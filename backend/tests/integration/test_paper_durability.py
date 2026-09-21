"""Durable paper state, verified against a real PostgreSQL.

The claim under test is the one the account response makes to a user: that a
balance, an open position and a day's session survive the process that created
them. A mocked store would agree with whatever this code believes, and the
things that actually break -- JSONB round-trips, exact ``Decimal``
preservation, row-level security -- are precisely what a substitute gets
subtly right for the wrong reasons.

Skipped, not failed, when no database is configured. Skipped is reported as
skipped: nothing here passes without a server behind it.

Every test works inside its own freshly-created owner and deletes it
afterwards, so a run leaves the database as it found it.
"""

from __future__ import annotations

import os
import pathlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from aetheris.adapters.persistence.accounts import PostgresAccountRepository
from aetheris.adapters.persistence.engine import (
    build_engine,
    build_session_factory,
    check_connectivity,
    session_scope,
)
from aetheris.adapters.persistence.paper import PostgresPaperSnapshotStore
from aetheris.analysis.leverage import resolve_leverage
from aetheris.core.config import Settings
from aetheris.core.freshness import DataStatus
from aetheris.domain.enums import OrderSide, TradingMode
from aetheris.domain.leverage import LeverageRequest
from aetheris.domain.paper import Durability
from aetheris.engines.paper.engine import (
    MarkPrice,
    PaperEngine,
    PaperEngineConfig,
    SubmitOrderRequest,
)
from aetheris.engines.paper.snapshot import SCHEMA_VERSION
from aetheris.engines.paper.store import InMemoryPaperRepository

pytestmark = pytest.mark.database

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def settings() -> Settings:
    env = pathlib.Path(__file__).resolve().parents[2] / ".env"
    if not env.exists():
        pytest.skip("no backend/.env; database tests need a configured PostgreSQL")
    loaded = Settings()
    if loaded.database_url is None and not os.environ.get("AETHERIS_DATABASE_URL"):
        pytest.skip("AETHERIS_DATABASE_URL is not set; database tests skipped")
    return loaded


@pytest_asyncio.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    eng = build_engine(settings)
    try:
        reachable, detail = await check_connectivity(eng)
        if not reachable:
            pytest.skip(f"database not reachable: {detail}")
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return build_session_factory(engine)


@pytest_asyncio.fixture
async def tenant(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    accounts = PostgresAccountRepository(factory)
    owner_id = uuid.uuid4()
    await accounts.ensure_owner(
        owner_id=owner_id, email=f"{owner_id}@test.invalid", display_name="test"
    )
    account_id = await accounts.ensure_account(
        owner_id=owner_id, mode=TradingMode.PAPER, starting_balance=Decimal(100)
    )
    try:
        yield owner_id, account_id
    finally:
        async with session_scope(factory, str(owner_id)) as session:
            await session.execute(
                text("DELETE FROM users WHERE id = :owner"), {"owner": str(owner_id)}
            )


# ----------------------------------------------------------------------
# Builders
# ----------------------------------------------------------------------


def make_engine() -> PaperEngine:
    config = PaperEngineConfig(
        starting_balance=Decimal(100),
        daily_profit_target=Decimal(20),
        daily_loss_limit=Decimal(-10),
        max_open_positions=5,
        max_position_notional=Decimal(100),
        max_portfolio_exposure=Decimal(300),
        max_data_age_seconds=30.0,
    )
    return PaperEngine(
        InMemoryPaperRepository(starting_balance=config.starting_balance, now=NOW), config
    )


def mark(price: str, *, symbol: str = "BTCUSDT") -> MarkPrice:
    return MarkPrice(
        symbol=symbol,
        status=DataStatus.OK,
        source="venue-adapter:rest",
        last_price=Decimal(price),
        bid_price=None,
        ask_price=None,
        age_seconds=1.0,
    )


def one_x():  # type: ignore[no-untyped-def]
    return resolve_leverage(
        LeverageRequest(requested_leverage=Decimal(1), basis="test"),
        exchange_max_leverage=None,
        risk_max_leverage=None,
        risk_engine_available=False,
    )


def trade_into(engine: PaperEngine) -> None:
    """Open one position and close another, so there is real history."""
    for symbol, price, margin in (("BTCUSDT", "80000", "20"), ("ETHUSDT", "3000", "15")):
        engine.submit_order(
            SubmitOrderRequest(
                symbol=symbol,
                side=OrderSide.BUY,
                margin=Decimal(margin),
                client_order_id=f"durability-{symbol}",
            ),
            now=NOW,
            mark=mark(price, symbol=symbol),
            filters=None,
            leverage=one_x(),
            paper_enabled=True,
            marks={symbol: mark(price, symbol=symbol)},
        )
    engine.close_position(
        "ETHUSDT",
        now=NOW + timedelta(minutes=30),
        mark=mark("3300", symbol="ETHUSDT"),
        marks={"ETHUSDT": mark("3300", symbol="ETHUSDT")},
    )


# ----------------------------------------------------------------------
# The guarantee
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_account_survives_the_process_that_created_it(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """The headline claim, tested the only way that means anything.

    A second engine and a second store -- no shared object at all -- stand in
    for the restart. Anything reused between the two would be testing memory.
    """
    owner_id, account_id = tenant
    first = make_engine()
    trade_into(first)
    before = first.state()
    assert before.positions, "the fixture must leave an open position"
    assert before.trades, "the fixture must leave a closed trade"

    writer = PostgresPaperSnapshotStore(factory, owner_id=owner_id, account_id=account_id)
    assert await writer.save(before) is True

    reader = PostgresPaperSnapshotStore(factory, owner_id=owner_id, account_id=account_id)
    restored = await reader.load()
    assert restored is not None

    second = make_engine()
    second.restore(restored)
    after = second.state()

    assert after.balance == before.balance
    assert after.realized_pnl == before.realized_pnl
    assert after.total_fees == before.total_fees
    assert set(after.positions) == set(before.positions)
    assert len(after.trades) == len(before.trades)
    assert len(after.order_log) == len(before.order_log)


@pytest.mark.asyncio
async def test_money_survives_jsonb_exactly(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """JSONB is the place a Decimal quietly becomes a float."""
    owner_id, account_id = tenant
    engine = make_engine()
    state = engine.state()
    state.balance = Decimal("0.1") + Decimal("0.2")
    state.total_fees = Decimal("12345.678901234567")
    state.realized_pnl = Decimal("-0.000000010000000")

    store = PostgresPaperSnapshotStore(factory, owner_id=owner_id, account_id=account_id)
    await store.save(state)
    restored = await PostgresPaperSnapshotStore(
        factory, owner_id=owner_id, account_id=account_id
    ).load()

    assert restored is not None
    assert restored.balance == Decimal("0.3")
    assert str(restored.balance) == "0.3"
    assert restored.total_fees == Decimal("12345.678901234567")
    assert restored.realized_pnl == Decimal("-0.000000010000000")


@pytest.mark.asyncio
async def test_saving_twice_replaces_rather_than_accumulates(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """One row per account. A second save must not leave two truths."""
    owner_id, account_id = tenant
    store = PostgresPaperSnapshotStore(factory, owner_id=owner_id, account_id=account_id)
    engine = make_engine()

    state = engine.state()
    state.balance = Decimal(111)
    await store.save(state)
    state.balance = Decimal(222)
    await store.save(state)

    async with session_scope(factory, str(owner_id)) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT count(*) FROM paper_state_snapshots "
                    "WHERE owner_id = :o AND account_id = :a"
                ),
                {"o": str(owner_id), "a": str(account_id)},
            )
        ).scalar_one()
    assert rows == 1

    restored = await store.load()
    assert restored is not None
    assert restored.balance == Decimal(222)


@pytest.mark.asyncio
async def test_a_first_run_finds_nothing_and_says_so(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """None means start fresh. It is the normal answer, not a failure."""
    owner_id, account_id = tenant
    store = PostgresPaperSnapshotStore(factory, owner_id=owner_id, account_id=account_id)
    assert await store.load() is None


@pytest.mark.asyncio
async def test_the_schema_version_is_stored_as_a_column(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A mismatch should be visible to a query, not only to the loader."""
    owner_id, account_id = tenant
    store = PostgresPaperSnapshotStore(factory, owner_id=owner_id, account_id=account_id)
    await store.save(make_engine().state())

    async with session_scope(factory, str(owner_id)) as session:
        version = (
            await session.execute(
                text("SELECT schema_version FROM paper_state_snapshots WHERE owner_id = :o"),
                {"o": str(owner_id)},
            )
        ).scalar_one()
    assert version == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_an_unreadable_snapshot_starts_fresh_rather_than_half_loading(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """An account whose balance restored but whose positions vanished is worse
    than one that plainly started over."""
    owner_id, account_id = tenant
    store = PostgresPaperSnapshotStore(factory, owner_id=owner_id, account_id=account_id)
    await store.save(make_engine().state())

    async with session_scope(factory, str(owner_id)) as session:
        await session.execute(
            text(
                "UPDATE paper_state_snapshots SET state = jsonb_set("
                "state, '{schema_version}', '999') WHERE owner_id = :o"
            ),
            {"o": str(owner_id)},
        )

    assert await store.load() is None
    # The database itself is fine, so the store is still considered healthy.
    assert store.durability is Durability.DURABLE


# ----------------------------------------------------------------------
# Tenancy
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_tenant_cannot_read_another_tenants_account(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A new table outside RLS is how tenant isolation stops being true."""
    owner_id, account_id = tenant
    mine = PostgresPaperSnapshotStore(factory, owner_id=owner_id, account_id=account_id)
    state = make_engine().state()
    state.balance = Decimal("4242.42")
    await mine.save(state)

    accounts = PostgresAccountRepository(factory)
    other_owner = uuid.uuid4()
    await accounts.ensure_owner(
        owner_id=other_owner, email=f"{other_owner}@test.invalid", display_name="other"
    )
    try:
        # Same account id, different owner. RLS scopes on owner, so this must
        # find nothing rather than another tenant's balance.
        intruder = PostgresPaperSnapshotStore(factory, owner_id=other_owner, account_id=account_id)
        assert await intruder.load() is None
    finally:
        async with session_scope(factory, str(other_owner)) as session:
            await session.execute(
                text("DELETE FROM users WHERE id = :owner"), {"owner": str(other_owner)}
            )


# ----------------------------------------------------------------------
# Degradation
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_write_degrades_durability_without_raising(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """The mutation already happened. Reporting failure would be the lie.

    A write that cannot land must not tell the caller their trade failed --
    it did not. What must change is the promise about surviving a restart.
    """
    owner_id, _ = tenant
    # An account id that does not exist violates the foreign key, which is a
    # real database failure rather than a simulated one.
    store = PostgresPaperSnapshotStore(factory, owner_id=owner_id, account_id=uuid.uuid4())
    assert store.durability is Durability.DURABLE

    landed = await store.save(make_engine().state())

    assert landed is False
    assert store.durability is Durability.IN_MEMORY
    assert store.last_error is not None
