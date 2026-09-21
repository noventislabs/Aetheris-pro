"""Phase 8A recovery guarantees, against a real PostgreSQL.

Every defect these cover was invisible while order records lived in a
dictionary and became reachable the moment they became durable. That is why
none of them is tested against a substitute: a fake store agrees with whatever
the code believes, and what went wrong here was the code believing something
the database did not.

Skipped without a configured database, the same as the rest of the integration
suite. Each test works inside a throwaway owner and deletes it afterwards.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from aetheris.adapters.persistence.accounts import (
    BOOTSTRAP_OWNER_ID,
    PostgresAccountRepository,
)
from aetheris.adapters.persistence.engine import (
    build_engine,
    build_session_factory,
    check_connectivity,
    session_scope,
)
from aetheris.adapters.persistence.models import OrderFillRow, OrderRow
from aetheris.adapters.persistence.orders import PostgresOrderRepository
from aetheris.core.config import Settings
from aetheris.core.errors import RiskRejectionCode
from aetheris.domain.enums import OrderSide, OrderState, OrderType, TradingMode
from aetheris.domain.order import (
    OrderFill,
    OrderIntent,
    OrderOrigin,
    OrderRecord,
    ReconciliationAction,
    ReconciliationDecision,
)
from aetheris.engines.order.engine import OrderLifecycleEngine
from aetheris.main import create_app

pytestmark = pytest.mark.database

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
BACKEND = pathlib.Path(__file__).resolve().parents[2]
ENV_FILE = BACKEND / ".env"
SRC = BACKEND / "src"


def _settings() -> Settings:
    if not ENV_FILE.exists():
        pytest.skip("no backend/.env; database tests need a configured PostgreSQL")
    settings = Settings(_env_file=str(ENV_FILE))  # type: ignore[call-arg]
    if settings.database_url is None:
        pytest.skip("AETHERIS_DATABASE_URL is not set; database tests skipped")
    return settings


@pytest.fixture(scope="module")
def settings() -> Settings:
    return _settings()


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
    await accounts.ensure_owner(owner_id=owner_id, email=f"{owner_id}@test.invalid")
    account_id = await accounts.ensure_account(
        owner_id=owner_id, mode=TradingMode.PAPER, starting_balance=Decimal(100)
    )
    try:
        yield owner_id, account_id
    finally:
        async with session_scope(factory, str(owner_id)) as session:
            await session.execute(text("DELETE FROM users WHERE id = :o"), {"o": str(owner_id)})


def _intent(account_id: uuid.UUID, key: str) -> OrderIntent:
    return OrderIntent(
        account_id=str(account_id),
        symbol="ETHUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("1.0"),
        mode=TradingMode.PAPER,
        origin=OrderOrigin.MANUAL,
        intent_key=key,
        created_at=NOW,
    )


def _repo(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> PostgresOrderRepository:
    owner_id, account_id = tenant
    return PostgresOrderRepository(factory, owner_id=owner_id, account_id=account_id)


async def _in_flight(engine: OrderLifecycleEngine, account_id: uuid.UUID, key: str) -> OrderRecord:
    """An order taken to the far edge of the uncertainty window."""
    record = await engine.create(_intent(account_id, key))
    await engine.mark_validating(record.order_id, now=NOW)
    return await engine.mark_submitting(record.order_id, now=NOW)


# ----------------------------------------------------------------------
# G1 - durable order identity
# ----------------------------------------------------------------------

WRITER = """
import asyncio, sys, uuid
from datetime import UTC, datetime
from decimal import Decimal

sys.path.insert(0, sys.argv[1])
from aetheris.adapters.persistence.engine import build_engine, build_session_factory
from aetheris.adapters.persistence.orders import PostgresOrderRepository
from aetheris.core.config import Settings
from aetheris.domain.enums import OrderSide, OrderType, TradingMode
from aetheris.domain.order import OrderIntent, OrderOrigin
from aetheris.engines.order.engine import OrderLifecycleEngine

owner, account, key, mode, env = (
    uuid.UUID(sys.argv[2]), uuid.UUID(sys.argv[3]), sys.argv[4], sys.argv[5], sys.argv[6]
)

async def main() -> None:
    db = build_engine(Settings(_env_file=env))
    try:
        repo = PostgresOrderRepository(
            build_session_factory(db), owner_id=owner, account_id=account)
        engine = OrderLifecycleEngine(repo)
        intent = OrderIntent(
            account_id=str(account), symbol="ETHUSDT", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=Decimal("1.0"),
            mode=TradingMode.PAPER, origin=OrderOrigin.MANUAL,
            intent_key=key, created_at=datetime(2026, 9, 21, 12, 0, tzinfo=UTC),
        )
        if mode == "create":
            record = await engine.create(intent)
            print("ORDER_ID", record.order_id)
            print("CLIENT_ID", record.client_order_id)
        else:
            record, created = await engine.create_or_get(intent)
            print("ORDER_ID", record.order_id)
            print("CLIENT_ID", record.client_order_id)
            print("CREATED", created)
    finally:
        await db.dispose()

asyncio.run(main())
"""


async def _run_writer(
    tmp_path: pathlib.Path,
    owner_id: uuid.UUID,
    account_id: uuid.UUID,
    key: str,
    mode: str,
) -> dict[str, str]:
    script = tmp_path / f"writer_{mode}_{key.replace(':', '_')}.py"
    script.write_text(WRITER, encoding="utf-8")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        str(SRC),
        str(owner_id),
        str(account_id),
        key,
        mode,
        str(ENV_FILE),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
    assert process.returncode == 0, stderr.decode()[-2000:]
    out: dict[str, str] = {}
    for line in stdout.decode().splitlines():
        name, _, value = line.partition(" ")
        out[name] = value
    return out


async def test_a_restart_does_not_reuse_the_previous_runs_order_id(
    factory: async_sessionmaker[AsyncSession],
    tenant: tuple[uuid.UUID, uuid.UUID],
    tmp_path: pathlib.Path,
) -> None:
    """The defect that made the first order after every restart fail.

    A per-process counter reset to zero on boot, so the first order of each run
    was ``order-1`` and collided with the first order of the run before. The
    restart here is a real one: a separate interpreter writes and exits, so
    nothing in-process can be what keeps the ids apart.
    """
    owner_id, account_id = tenant

    first = await _run_writer(tmp_path, owner_id, account_id, "manual:one", "create")
    second = await _run_writer(tmp_path, owner_id, account_id, "manual:two", "create")

    assert first["ORDER_ID"] != second["ORDER_ID"]
    assert first["CLIENT_ID"] != second["CLIENT_ID"]

    repo = _repo(factory, tenant)
    stored = await repo.all_records()
    assert len(stored) == 2
    assert len({r.order_id for r in stored}) == 2


async def test_many_orders_in_one_process_still_have_distinct_ids(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Uniqueness must not depend on the ids being sequential."""
    _, account_id = tenant
    engine = OrderLifecycleEngine(_repo(factory, tenant))
    made = [await engine.create(_intent(account_id, f"manual:k{n}")) for n in range(5)]
    assert len({r.order_id for r in made}) == 5


# ----------------------------------------------------------------------
# G2 - adopting a venue total
# ----------------------------------------------------------------------


async def test_adopting_a_venue_total_leaves_no_stale_fill_rows(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """The record must still be readable afterwards, which it was not.

    Adopting a venue total deliberately drops the local fills: per-fill detail
    cannot be reconstructed from a summary, so keeping it beside an adopted
    total would give the record an implied notional nothing observed. The child
    rows were only ever appended, so the old fill survived beside the new total
    and the record's own validator rejected it -- on every later *read*,
    including the recovery sweep.
    """
    owner_id, account_id = tenant
    repo = _repo(factory, tenant)
    engine = OrderLifecycleEngine(repo)

    record = await _in_flight(engine, account_id, "manual:adopt")
    await engine.apply_venue_ack(
        record.order_id, venue_order_id="v-1", state=OrderState.ACCEPTED, now=NOW
    )
    await engine.apply_fill(
        record.order_id,
        OrderFill(fill_id="f-1", price=Decimal(100), quantity=Decimal("0.4"), filled_at=NOW),
        now=NOW,
    )
    await engine.mark_unknown(record.order_id, reason="no response", now=NOW)
    await engine.begin_reconciliation(record.order_id, now=NOW)
    await engine.apply_reconciliation(
        record.order_id,
        ReconciliationDecision(
            action=ReconciliationAction.ADOPT_VENUE_STATE,
            target_state=OrderState.FILLED,
            detail="the venue says it filled completely",
            filled_quantity=Decimal("1.0"),
            average_fill_price=Decimal(105),
        ),
        now=NOW,
    )

    async with session_scope(factory, str(owner_id)) as session:
        rows = await session.scalar(
            text(
                "SELECT count(*) FROM order_fills f JOIN orders o ON o.id = f.order_id "
                "WHERE o.order_id = :o"
            ),
            {"o": record.order_id},
        )
    assert rows == 0

    # The assertion that actually failed before: it can be read back at all.
    reloaded = await repo.get(record.order_id)
    assert reloaded is not None
    assert reloaded.state is OrderState.FILLED
    assert reloaded.filled_quantity == Decimal("1.0")
    assert reloaded.average_fill_price == Decimal(105)
    assert reloaded.fills == ()

    # And it does not break the sweep either.
    scan = await repo.scan_open()
    assert scan.unreadable == ()


async def test_a_fill_that_is_still_ours_is_not_deleted(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Synchronising the fill set must not become "delete everything"."""
    owner_id, account_id = tenant
    repo = _repo(factory, tenant)
    engine = OrderLifecycleEngine(repo)

    record = await _in_flight(engine, account_id, "manual:keep")
    await engine.apply_venue_ack(
        record.order_id, venue_order_id="v-2", state=OrderState.ACCEPTED, now=NOW
    )
    for n, qty in ((1, "0.3"), (2, "0.2")):
        await engine.apply_fill(
            record.order_id,
            OrderFill(fill_id=f"f-{n}", price=Decimal(100), quantity=Decimal(qty), filled_at=NOW),
            now=NOW,
        )

    async with session_scope(factory, str(owner_id)) as session:
        rows = await session.scalar(
            text(
                "SELECT count(*) FROM order_fills f JOIN orders o ON o.id = f.order_id "
                "WHERE o.order_id = :o"
            ),
            {"o": record.order_id},
        )
    assert rows == 2
    reloaded = await repo.get(record.order_id)
    assert reloaded is not None
    assert reloaded.filled_quantity == Decimal("0.5")
    assert len(reloaded.fills) == 2


# ----------------------------------------------------------------------
# G3 - concurrent fills
# ----------------------------------------------------------------------


async def test_two_workers_filling_one_order_keep_both_fills_and_the_right_total(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Unserialised, both fills landed and the total kept only one of them.

    Worse than a lost update: the rows and the total then disagreed, and the
    record could no longer be loaded at all. Both fills must survive *and* the
    aggregate must equal their sum.
    """
    owner_id, account_id = tenant
    repo = _repo(factory, tenant)
    engine = OrderLifecycleEngine(repo)

    record = await _in_flight(engine, account_id, "manual:concurrent")
    await engine.apply_venue_ack(
        record.order_id, venue_order_id="v-3", state=OrderState.ACCEPTED, now=NOW
    )

    async def fill(fill_id: str) -> None:
        await engine.apply_fill(
            record.order_id,
            OrderFill(fill_id=fill_id, price=Decimal(100), quantity=Decimal("0.5"), filled_at=NOW),
            now=NOW,
        )

    await asyncio.gather(fill("f-a"), fill("f-b"))

    async with session_scope(factory, str(owner_id)) as session:
        rows = await session.scalar(
            text(
                "SELECT count(*) FROM order_fills f JOIN orders o ON o.id = f.order_id "
                "WHERE o.order_id = :o"
            ),
            {"o": record.order_id},
        )
        total = await session.scalar(
            text("SELECT filled_quantity FROM orders WHERE order_id = :o"),
            {"o": record.order_id},
        )
    assert rows == 2
    assert total == Decimal("1.0")

    reloaded = await repo.get(record.order_id)
    assert reloaded is not None
    assert reloaded.filled_quantity == Decimal("1.0")
    assert len(reloaded.fills) == 2
    assert reloaded.state is OrderState.FILLED


async def test_a_nested_lock_on_the_same_order_does_not_deadlock(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """``reconcile`` locks, then calls a mutator that locks the same row.

    A second ``SELECT ... FOR UPDATE`` from another connection would wait on
    the transaction that is waiting for it. The re-entrant path is what keeps
    the recovery route from hanging until a statement timeout.
    """
    _, account_id = tenant
    engine = OrderLifecycleEngine(_repo(factory, tenant))
    record = await _in_flight(engine, account_id, "manual:nested")
    await engine.mark_unknown(record.order_id, reason="silence", now=NOW)

    resolved, decision = await asyncio.wait_for(
        engine.reconcile(record.order_id, None, venue_reachable=False, now=NOW),
        timeout=30,
    )
    assert decision.action is ReconciliationAction.VENUE_UNREACHABLE
    assert resolved.state is OrderState.UNKNOWN


# ----------------------------------------------------------------------
# Recovery pass
# ----------------------------------------------------------------------


async def test_recovery_moves_interrupted_orders_to_unknown_and_leaves_the_rest(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """The three populations a restart finds, each handled differently.

    An order that reached the venue is unknown -- we were interrupted and never
    learned its fate. An order that never left is not: its absence at the venue
    is certain rather than inferred, and manufacturing doubt about it would
    create a state the machine then refuses to resolve without evidence that
    cannot exist.
    """
    _, account_id = tenant
    engine = OrderLifecycleEngine(_repo(factory, tenant))

    in_flight = await _in_flight(engine, account_id, "manual:inflight")
    never_left = await engine.create(_intent(account_id, "manual:neverleft"))
    settled = await _in_flight(engine, account_id, "manual:settled")
    await engine.apply_venue_ack(
        settled.order_id, venue_order_id="v-9", state=OrderState.FILLED, now=NOW
    )

    report = await engine.recover(now=NOW)

    assert report.durable is True
    assert in_flight.order_id in report.interrupted
    assert never_left.order_id in report.never_submitted
    assert settled.order_id not in report.interrupted

    after = await engine.repository.get(in_flight.order_id)
    assert after is not None
    assert after.state is OrderState.UNKNOWN
    assert after.is_unreconciled

    untouched = await engine.repository.get(never_left.order_id)
    assert untouched is not None
    assert untouched.state is OrderState.CREATED

    terminal = await engine.repository.get(settled.order_id)
    assert terminal is not None
    assert terminal.state is OrderState.FILLED


async def test_recovery_is_idempotent(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Safe to run twice, which is what makes a crash during recovery survivable."""
    _, account_id = tenant
    engine = OrderLifecycleEngine(_repo(factory, tenant))
    record = await _in_flight(engine, account_id, "manual:twice")

    first = await engine.recover(now=NOW)
    second = await engine.recover(now=NOW)

    assert record.order_id in first.interrupted
    assert second.interrupted == ()
    assert record.order_id in second.already_unresolved
    assert first.blocking == second.blocking


async def test_recovery_never_guesses_a_venue_answer(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """UNKNOWN is where recovery stops. Resolving it needs evidence."""
    _, account_id = tenant
    engine = OrderLifecycleEngine(_repo(factory, tenant))
    record = await _in_flight(engine, account_id, "manual:noguess")
    await engine.recover(now=NOW)

    after = await engine.repository.get(record.order_id)
    assert after is not None
    assert after.state is OrderState.UNKNOWN
    assert after.state is not OrderState.FILLED
    assert after.state is not OrderState.CANCELLED
    assert after.venue_order_id is None


async def test_an_in_memory_store_reports_that_it_recovered_nothing(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A clean report from a store that forgets everything is not a clean restart."""
    from aetheris.engines.order.store import InMemoryOrderRepository

    report = await OrderLifecycleEngine(InMemoryOrderRepository()).recover(now=NOW)
    assert report.durable is False
    assert report.scanned == 0


# ----------------------------------------------------------------------
# Recovery isolation
# ----------------------------------------------------------------------


async def _corrupt(
    factory: async_sessionmaker[AsyncSession],
    tenant: tuple[uuid.UUID, uuid.UUID],
    order_id: str,
) -> None:
    """Store an order whose fill rows disagree with its own total.

    Exactly the damage the two fixed defects used to cause, written directly so
    the isolation can be tested without reintroducing them.
    """
    owner_id, account_id = tenant
    async with session_scope(factory, str(owner_id)) as session:
        row = OrderRow(
            owner_id=owner_id,
            account_id=account_id,
            order_id=order_id,
            client_order_id=f"cid-{order_id}",
            state=OrderState.ACCEPTED.value,
            symbol="ETHUSDT",
            side=OrderSide.BUY.value,
            order_type=OrderType.MARKET.value,
            mode=TradingMode.PAPER.value,
            origin=OrderOrigin.MANUAL.value,
            intent_key="manual:corrupt",
            quantity=Decimal("1.0"),
            filled_quantity=Decimal("0.5"),
            created_at=NOW,
            updated_at=NOW,
            submitted_at=NOW,
        )
        session.add(row)
        await session.flush()
        session.add(
            OrderFillRow(
                owner_id=owner_id,
                order_id=row.id,
                fill_id="f-x",
                price=Decimal(100),
                quantity=Decimal("0.9"),  # disagrees with filled_quantity
                filled_at=NOW,
            )
        )
        await session.flush()


async def test_one_unreadable_order_does_not_hide_every_other_one(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A record is validated on load, so a whole-table read raises on one bad row.

    The read that raises is the recovery sweep, which would mean a single
    damaged order concealing every healthy unreconciled one behind it -- the
    opposite of failing closed.
    """
    _, account_id = tenant
    repo = _repo(factory, tenant)
    engine = OrderLifecycleEngine(repo)

    healthy = await _in_flight(engine, account_id, "manual:healthy")
    await _corrupt(factory, tenant, "order-corrupt")

    # The strict read still refuses, which is correct for a caller that asked
    # for records rather than for a report.
    with pytest.raises(ValidationError):
        await repo.open_records()

    scan = await repo.scan_open()
    assert [r.order_id for r in scan.records] == [healthy.order_id]
    assert [u.order_id for u in scan.unreadable] == ["order-corrupt"]
    assert scan.blocking >= 1


async def test_an_unreadable_order_is_recorded_and_blocks_rather_than_being_skipped(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Reported as durable evidence. An order nobody can read may still be a position."""
    owner_id, _ = tenant
    repo = _repo(factory, tenant)
    engine = OrderLifecycleEngine(repo)
    await _corrupt(factory, tenant, "order-corrupt-2")

    report = await engine.recover(now=NOW)
    assert "order-corrupt-2" in report.unreadable
    assert report.blocking >= 1

    async with session_scope(factory, str(owner_id)) as session:
        recorded = await session.scalar(
            text(
                "SELECT count(*) FROM order_discrepancies d JOIN orders o ON o.id = d.order_id "
                "WHERE o.order_id = :o"
            ),
            {"o": "order-corrupt-2"},
        )
    assert recorded == 1

    pending = await engine.reconciliation_pending()
    assert pending.count >= 1
    assert "could not be read" in pending.detail


# ----------------------------------------------------------------------
# Duplicate retry / replay
# ----------------------------------------------------------------------


async def test_a_retry_after_a_restart_replays_the_persisted_order(
    factory: async_sessionmaker[AsyncSession],
    tenant: tuple[uuid.UUID, uuid.UUID],
    tmp_path: pathlib.Path,
) -> None:
    """The caller's real question is "what happened to my order", and raising does not answer it.

    Identity is derived from intent, so the retry re-derives the same
    ``client_order_id``. What must come back is the order that already exists,
    not an exception and not a second order.
    """
    owner_id, account_id = tenant

    original = await _run_writer(tmp_path, owner_id, account_id, "manual:retry", "create")
    replayed = await _run_writer(tmp_path, owner_id, account_id, "manual:retry", "get")

    assert replayed["CREATED"] == "False"
    assert replayed["ORDER_ID"] == original["ORDER_ID"]
    assert replayed["CLIENT_ID"] == original["CLIENT_ID"]

    repo = _repo(factory, tenant)
    stored = await repo.all_records()
    assert len(stored) == 1


async def test_concurrent_duplicate_requests_produce_one_order(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Both callers get the same order back; the constraint decides which one won."""
    _, account_id = tenant
    repo = _repo(factory, tenant)
    engine = OrderLifecycleEngine(repo)
    intent = _intent(account_id, "manual:raced-replay")

    results = await asyncio.gather(
        engine.create_or_get(intent),
        engine.create_or_get(intent),
    )

    order_ids = {record.order_id for record, _ in results}
    assert len(order_ids) == 1
    assert sum(1 for _, created in results if created) <= 1
    assert len(await repo.all_records()) == 1


# ----------------------------------------------------------------------
# Reconciliation safety, end to end through the risk engine
# ----------------------------------------------------------------------


@pytest.fixture
def api(settings: Settings) -> Iterator[TestClient]:
    """The real app over the real database, with the venue mocked.

    The exchange is the one thing that must not be real here: these tests are
    about what the order store tells the risk engine, and a live venue would
    make them depend on the market.
    """
    from tests.integration.test_paper_api import PriceBook, routes

    handler = routes(PriceBook())
    live = settings.model_copy(update={"environment": "test"})
    live.binance.max_retries = 0
    live.binance.backoff_seconds = 0.0
    live.binance.ticker_ttl_seconds = 0.001
    with TestClient(create_app(live, exchange_transport=handler.transport())) as client:
        yield client


async def test_a_pending_order_refuses_a_new_entry_through_the_real_risk_path(
    api: TestClient, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The check that was unreachable, reached.

    ``unreconciled_orders`` was hardcoded to zero in the account view, so the
    risk engine's RECONCILIATION_PENDING branch could never fire however many
    orders were outstanding. This drives it from a real order in PostgreSQL.
    """
    # Driven through a pool of our own. The app's pool belongs to the
    # TestClient's event loop, and reaching into it from this one is a
    # cross-loop error rather than a test of anything.
    account_id = api.app.state.order_account_id
    assert account_id is not None
    engine = OrderLifecycleEngine(
        PostgresOrderRepository(factory, owner_id=BOOTSTRAP_OWNER_ID, account_id=account_id)
    )
    await engine.repository.clear()

    try:
        record = await _in_flight(engine, account_id, "manual:blocking-entry")
        await engine.mark_unknown(record.order_id, reason="no response", now=NOW)

        pending = await engine.reconciliation_pending()
        assert pending.count == 1

        response = api.post(
            "/api/v1/paper/orders",
            json={"symbol": "BTCUSDT", "side": "BUY", "margin": "20"},
        )
        body = response.json()
        assert body["order"]["rejection_code"] == (RiskRejectionCode.RECONCILIATION_PENDING.value)
        assert "awaiting reconciliation" in body["order"]["rejection_detail"]
    finally:
        await engine.repository.clear()


async def test_no_pending_orders_does_not_block(
    api: TestClient, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The complement, so the refusal above is known to be caused by the order."""
    account_id = api.app.state.order_account_id
    engine = OrderLifecycleEngine(
        PostgresOrderRepository(factory, owner_id=BOOTSTRAP_OWNER_ID, account_id=account_id)
    )
    await engine.repository.clear()

    pending = await engine.reconciliation_pending()
    assert pending.count == 0

    response = api.post(
        "/api/v1/paper/orders",
        json={"symbol": "BTCUSDT", "side": "BUY", "margin": "20"},
    )
    body = response.json()
    assert body["order"]["rejection_code"] != RiskRejectionCode.RECONCILIATION_PENDING.value


async def test_the_application_runs_recovery_at_startup(api: TestClient) -> None:
    """Recovery is not a method somebody could call; it runs on boot."""
    report = api.app.state.order_recovery
    assert report is not None
    assert report.durable is True
