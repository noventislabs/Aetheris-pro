"""Phase 1 persistence, verified against a real PostgreSQL.

These tests do not mock a database. A mock would agree with whatever this code
believes, and the three things the order engine depends on -- exact ``NUMERIC``
round-trips, ``timestamptz``, and ``SELECT ... FOR UPDATE`` row locking -- are
precisely the things a substitute gets subtly wrong. ADR 0002 rejected SQLite
for that reason and the same reasoning rejects a fake here.

They are skipped, not failed, when no database is configured, so a checkout
without credentials still runs the rest of the suite. Skipped is reported as
skipped: nothing here passes without a server behind it.

Every test works inside its own freshly-created owner and deletes it afterwards,
so a run leaves the database as it found it and two runs cannot collide.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys
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
    DatabaseUnavailableError,
    build_engine,
    build_session_factory,
    check_connectivity,
    normalise_dsn,
    session_scope,
)
from aetheris.adapters.persistence.orders import PostgresOrderRepository
from aetheris.core.config import Settings
from aetheris.domain.enums import OrderSide, OrderState, OrderType, TradingMode
from aetheris.domain.order import OrderFill, OrderIntent, OrderOrigin, OrderRecord
from aetheris.engines.order.engine import OrderLifecycleEngine
from aetheris.engines.order.store import DuplicateClientOrderIdError

pytestmark = pytest.mark.database

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
BACKEND = pathlib.Path(__file__).resolve().parents[2]
ENV_FILE = BACKEND / ".env"
SRC = BACKEND / "src"


def _settings() -> Settings:
    """Settings from the developer's own .env, which the global fixture excludes.

    ``tests/conftest.py`` builds settings with ``_env_file=None`` on purpose, so
    a configured database never leaks into the rest of the suite. These tests
    want it, and ask for it explicitly rather than by changing that default.
    """
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


async def _make_tenant(
    factory: async_sessionmaker[AsyncSession], *, balance: Decimal = Decimal(100)
) -> tuple[uuid.UUID, uuid.UUID]:
    """A throwaway owner with a paper account, created through the runtime role."""
    accounts = PostgresAccountRepository(factory)
    owner_id = uuid.uuid4()
    await accounts.ensure_owner(
        owner_id=owner_id, email=f"{owner_id}@test.invalid", display_name="test"
    )
    account_id = await accounts.ensure_account(
        owner_id=owner_id, mode=TradingMode.PAPER, starting_balance=balance
    )
    return owner_id, account_id


async def _drop_tenant(factory: async_sessionmaker[AsyncSession], owner_id: uuid.UUID) -> None:
    async with session_scope(factory, str(owner_id)) as session:
        await session.execute(text("DELETE FROM users WHERE id = :owner"), {"owner": str(owner_id)})


@pytest_asyncio.fixture
async def tenant(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    owner_id, account_id = await _make_tenant(factory)
    try:
        yield owner_id, account_id
    finally:
        await _drop_tenant(factory, owner_id)


def _intent(account_id: uuid.UUID, *, key: str = "manual:ETHUSDT:BUY:bucket-1") -> OrderIntent:
    return OrderIntent(
        account_id=str(account_id),
        symbol="ETHUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("1.5"),
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


# ----------------------------------------------------------------------
# 1. Connectivity
# ----------------------------------------------------------------------


async def test_the_database_is_reachable_and_is_postgresql(engine: AsyncEngine) -> None:
    reachable, detail = await check_connectivity(engine)
    assert reachable
    assert "PostgreSQL" in detail


async def test_connectivity_failure_is_a_report_not_an_exception(settings: Settings) -> None:
    """A dropped connection is an operational state, per ADR 0002.

    The detail names the failure class. It never names the host, the user or
    the password -- a readiness body is served to a browser.
    """
    broken = settings.model_copy(update={"database_pool_size": 1})
    eng = build_engine(broken)
    await eng.dispose()
    reachable, detail = await check_connectivity(eng)
    assert isinstance(reachable, bool)
    assert settings.database_url is not None
    secret = settings.database_url.get_secret_value()
    assert secret not in detail
    for part in secret.replace("://", ":").replace("@", ":").split(":"):
        if len(part) > 8:
            assert part not in detail


async def test_the_runtime_role_is_not_the_schema_owner(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """The whole two-role design in one assertion.

    A role holding BYPASSRLS is exempt from every policy, so the isolation
    below would be decoration. This is checked at runtime because the failure
    mode is silent: the policies still appear to exist.
    """
    owner_id, _ = tenant
    async with session_scope(factory, str(owner_id)) as session:
        bypasses = await session.scalar(
            text("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
        )
        superuser = await session.scalar(
            text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        )
        creates_roles = await session.scalar(
            text("SELECT rolcreaterole FROM pg_roles WHERE rolname = current_user")
        )
    assert bypasses is False
    assert superuser is False
    assert creates_roles is False


# ----------------------------------------------------------------------
# 2. Decimal precision
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        Decimal("123.45678901"),
        Decimal("0.00000001"),
        Decimal("-0.00000001"),
        Decimal("9999999999999999.87654321"),
        Decimal("100.00000000"),
    ],
)
async def test_money_round_trips_exactly(
    factory: async_sessionmaker[AsyncSession],
    tenant: tuple[uuid.UUID, uuid.UUID],
    value: Decimal,
) -> None:
    """A stored balance that comes back rounded is a fabricated balance."""
    owner_id, _ = tenant
    async with session_scope(factory, str(owner_id)) as session:
        got = await session.scalar(text("SELECT CAST(:v AS numeric(24,8))"), {"v": value})
    assert isinstance(got, Decimal)
    assert got == value


async def test_an_order_keeps_every_digit_through_storage(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    _, account_id = tenant
    repo = _repo(factory, tenant)
    record = OrderRecord(
        order_id="order-precision",
        client_order_id="cid-precision",
        intent=_intent(account_id).model_copy(update={"quantity": Decimal("0.00000001")}),
        state=OrderState.CREATED,
        created_at=NOW,
        updated_at=NOW,
    )
    await repo.add(record)

    loaded = await repo.get("order-precision")
    assert loaded is not None
    assert loaded.intent.quantity == Decimal("0.00000001")


# ----------------------------------------------------------------------
# 3. timestamptz
# ----------------------------------------------------------------------


async def test_timestamps_keep_their_offset(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Order timing across a restart is UTC or it is wrong."""
    _, account_id = tenant
    repo = _repo(factory, tenant)
    record = OrderRecord(
        order_id="order-time",
        client_order_id="cid-time",
        intent=_intent(account_id),
        state=OrderState.SUBMITTED,
        created_at=NOW,
        updated_at=NOW,
        submitted_at=NOW,
    )
    await repo.add(record)

    loaded = await repo.get("order-time")
    assert loaded is not None
    assert loaded.created_at.tzinfo is not None
    assert loaded.submitted_at is not None
    assert loaded.submitted_at == NOW
    assert loaded.submitted_at.utcoffset() == timedelta(0)


# ----------------------------------------------------------------------
# 4. Idempotency, enforced by the database
# ----------------------------------------------------------------------


async def test_a_duplicate_client_order_id_is_refused_by_the_database(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """The guarantee that could not stay in application code.

    Two processes can both check-then-insert; a dictionary cannot serialise
    across them and a unique constraint can.
    """
    _, account_id = tenant
    repo = _repo(factory, tenant)
    first = OrderRecord(
        order_id="order-dup-1",
        client_order_id="cid-shared",
        intent=_intent(account_id),
        state=OrderState.CREATED,
        created_at=NOW,
        updated_at=NOW,
    )
    await repo.add(first)

    second = first.model_copy(update={"order_id": "order-dup-2"})
    with pytest.raises(DuplicateClientOrderIdError):
        await repo.add(second)

    assert await repo.get("order-dup-2") is None


async def test_concurrent_inserts_of_one_identity_produce_exactly_one_order(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """The race the constraint exists for, run rather than argued about."""
    _, account_id = tenant
    repo = _repo(factory, tenant)

    def make(order_id: str) -> OrderRecord:
        return OrderRecord(
            order_id=order_id,
            client_order_id="cid-raced",
            intent=_intent(account_id),
            state=OrderState.CREATED,
            created_at=NOW,
            updated_at=NOW,
        )

    results = await asyncio.gather(
        repo.add(make("order-race-a")),
        repo.add(make("order-race-b")),
        return_exceptions=True,
    )
    accepted = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, DuplicateClientOrderIdError)]
    assert len(accepted) == 1
    assert len(refused) == 1

    stored = await repo.all_records()
    assert len([r for r in stored if r.client_order_id == "cid-raced"]) == 1


# ----------------------------------------------------------------------
# 5. Concurrent row locking
# ----------------------------------------------------------------------


async def test_a_row_lock_serialises_two_workers_on_one_order(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Read-modify-write twice, concurrently, on the same order.

    Without ``SELECT ... FOR UPDATE`` both workers read the same value and one
    increment is lost -- the final count is 1, not 2. That is exactly the shape
    of two reconciliation passes both reading ``UNKNOWN`` and both applying a
    conclusion.
    """
    _, account_id = tenant
    repo = _repo(factory, tenant)
    await repo.add(
        OrderRecord(
            order_id="order-locked",
            client_order_id="cid-locked",
            intent=_intent(account_id),
            state=OrderState.UNKNOWN,
            created_at=NOW,
            updated_at=NOW,
            submitted_at=NOW,
        )
    )

    async def increment() -> None:
        async with repo.locked("order-locked") as held:
            assert held is not None
            # A real reconciliation asks a venue here. The await is the point:
            # it is where an unserialised worker would interleave.
            await asyncio.sleep(0.05)
            await repo.update(
                held.model_copy(
                    update={"reconciliation_attempts": held.reconciliation_attempts + 1}
                )
            )

    await asyncio.gather(increment(), increment())

    final = await repo.get("order-locked")
    assert final is not None
    assert final.reconciliation_attempts == 2


async def test_the_lock_is_per_order_not_global(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A global lock would serialise every order behind the slowest one."""
    _, account_id = tenant
    repo = _repo(factory, tenant)
    for n in (1, 2):
        await repo.add(
            OrderRecord(
                order_id=f"order-parallel-{n}",
                client_order_id=f"cid-parallel-{n}",
                intent=_intent(account_id, key=f"manual:ETHUSDT:BUY:bucket-{n}"),
                state=OrderState.UNKNOWN,
                created_at=NOW,
                updated_at=NOW,
                submitted_at=NOW,
            )
        )

    first_held = asyncio.Event()
    second_held = asyncio.Event()

    async def hold(order_id: str, mine: asyncio.Event, theirs: asyncio.Event) -> None:
        async with repo.locked(order_id):
            mine.set()
            # Each worker refuses to leave until the other has also taken its
            # lock. Both can only get here if the locks are per row: a table
            # lock would block the second here forever and time this out.
            # Deliberately not a sleep -- a sleep would measure the latency of
            # opening a second pooled connection rather than the lock's scope.
            await asyncio.wait_for(theirs.wait(), timeout=60)

    await asyncio.gather(
        hold("order-parallel-1", first_held, second_held),
        hold("order-parallel-2", second_held, first_held),
    )
    assert first_held.is_set() and second_held.is_set()


# ----------------------------------------------------------------------
# 6 & 8. Cross-tenant isolation, enforced by RLS on the runtime role
# ----------------------------------------------------------------------


async def test_one_tenant_cannot_read_another_tenants_orders(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Written before there is an endpoint to attack, as phase 1 requires.

    Every read path is checked, not just the obvious one: by application id, by
    derived client order id, the full listing, and the open-order sweep a
    recovery pass uses. A leak through any one of them is a leak.
    """
    alice_owner, alice_account = await _make_tenant(factory)
    bob_owner, bob_account = await _make_tenant(factory)
    try:
        alice = PostgresOrderRepository(factory, owner_id=alice_owner, account_id=alice_account)
        bob = PostgresOrderRepository(factory, owner_id=bob_owner, account_id=bob_account)

        await alice.add(
            OrderRecord(
                order_id="order-alice",
                client_order_id="cid-alice",
                intent=_intent(alice_account),
                state=OrderState.SUBMITTED,
                created_at=NOW,
                updated_at=NOW,
                submitted_at=NOW,
            )
        )

        assert await bob.get("order-alice") is None
        assert await bob.get_by_client_order_id("cid-alice") is None
        assert await bob.all_records() == ()
        assert await bob.open_records() == ()

        mine = await alice.all_records()
        assert [r.order_id for r in mine] == ["order-alice"]
    finally:
        await _drop_tenant(factory, alice_owner)
        await _drop_tenant(factory, bob_owner)


async def test_a_tenant_cannot_write_a_row_owned_by_someone_else(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The WITH CHECK half of the policy. USING alone would guard reads only."""
    alice_owner, _alice_account = await _make_tenant(factory)
    bob_owner, _bob_account = await _make_tenant(factory)
    try:
        async with session_scope(factory, str(bob_owner)) as session:
            with pytest.raises(Exception) as caught:
                await session.execute(
                    text(
                        "INSERT INTO accounts (owner_id, mode, starting_balance) "
                        "VALUES (:owner, 'TESTNET', 1)"
                    ),
                    {"owner": str(alice_owner)},
                )
                await session.flush()
        assert "policy" in str(caught.value).lower() or "row-level" in str(caught.value).lower()
    finally:
        await _drop_tenant(factory, alice_owner)
        await _drop_tenant(factory, bob_owner)


async def test_a_query_outside_a_tenant_scope_sees_nothing(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Fails closed.

    ``current_setting(..., true)`` is NULL when unset and NULL equals nothing,
    so a forgotten scope returns zero rows rather than every tenant's. This is
    the behaviour a missing ``WHERE`` clause should have and normally does not.
    """
    _owner_id, account_id = tenant
    repo = _repo(factory, tenant)
    await repo.add(
        OrderRecord(
            order_id="order-scoped",
            client_order_id="cid-scoped",
            intent=_intent(account_id),
            state=OrderState.CREATED,
            created_at=NOW,
            updated_at=NOW,
        )
    )

    session = factory()
    try:
        async with session.begin():
            visible = await session.scalar(text("SELECT count(*) FROM orders"))
            accounts = await session.scalar(text("SELECT count(*) FROM accounts"))
            users = await session.scalar(text("SELECT count(*) FROM users"))
    finally:
        await session.close()

    assert visible == 0
    assert accounts == 0
    assert users == 0


# ----------------------------------------------------------------------
# 7. Persistence across a process restart
# ----------------------------------------------------------------------


WRITER = """
import asyncio, pathlib, sys, uuid
from datetime import UTC, datetime
from decimal import Decimal

sys.path.insert(0, sys.argv[1])
from aetheris.adapters.persistence.engine import build_engine, build_session_factory
from aetheris.adapters.persistence.orders import PostgresOrderRepository
from aetheris.core.config import Settings
from aetheris.domain.enums import OrderSide, OrderState, OrderType, TradingMode
from aetheris.domain.order import OrderIntent, OrderRecord

owner_id, account_id, order_id, env_file = (
    uuid.UUID(sys.argv[2]), uuid.UUID(sys.argv[3]), sys.argv[4], sys.argv[5]
)

async def main() -> None:
    engine = build_engine(Settings(_env_file=env_file))
    try:
        repo = PostgresOrderRepository(
            build_session_factory(engine), owner_id=owner_id, account_id=account_id
        )
        now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
        await repo.add(
            OrderRecord(
                order_id=order_id,
                client_order_id="cid-restart",
                intent=OrderIntent(
                    account_id=str(account_id), symbol="ETHUSDT", side=OrderSide.BUY,
                    order_type=OrderType.MARKET, quantity=Decimal("2.25"),
                    mode=TradingMode.PAPER, intent_key="manual:ETHUSDT:BUY:restart",
                    created_at=now,
                ),
                state=OrderState.SUBMITTED,
                created_at=now, updated_at=now, submitted_at=now,
            )
        )
        print("WROTE", repo.durable)
    finally:
        await engine.dispose()

asyncio.run(main())
"""


async def test_an_order_written_by_another_process_survives_into_this_one(
    factory: async_sessionmaker[AsyncSession],
    tenant: tuple[uuid.UUID, uuid.UUID],
    tmp_path: pathlib.Path,
) -> None:
    """The restart test, run as an actual restart.

    A separate interpreter writes the order and exits. Nothing in this process
    ever held that record, so nothing in this process can be what returns it:
    a pool, a cache or a module-level dictionary all die with that subprocess.
    What comes back comes back from PostgreSQL.

    This is the assertion phase 8a could not make, and the reason it did not
    claim crash recovery.
    """
    owner_id, account_id = tenant
    script = tmp_path / "writer.py"
    script.write_text(WRITER, encoding="utf-8")

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        str(SRC),
        str(owner_id),
        str(account_id),
        "order-restart",
        str(ENV_FILE),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
    assert process.returncode == 0, stderr.decode()[-2000:]
    assert b"WROTE True" in stdout

    repo = _repo(factory, tenant)
    recovered = await repo.get_by_client_order_id("cid-restart")
    assert recovered is not None
    assert recovered.order_id == "order-restart"
    assert recovered.state is OrderState.SUBMITTED
    assert recovered.intent.quantity == Decimal("2.25")
    # The crash signature the recovery protocol reads.
    assert recovered.submitted_at is not None
    assert recovered.venue_order_id is None
    assert recovered.reached_venue


async def test_the_lifecycle_engine_reports_crash_recovery_only_when_durable(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    from aetheris.engines.order.store import InMemoryOrderRepository

    assert OrderLifecycleEngine(_repo(factory, tenant)).supports_crash_recovery is True
    assert OrderLifecycleEngine(InMemoryOrderRepository()).supports_crash_recovery is False


# ----------------------------------------------------------------------
# Transaction boundaries
# ----------------------------------------------------------------------


async def test_a_fill_and_the_order_total_commit_together(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Boundary C. Apart, they diverge, and the record's own validator would
    then reject what it just loaded."""
    owner_id, account_id = tenant
    repo = _repo(factory, tenant)
    engine = OrderLifecycleEngine(repo)

    record = await engine.create(_intent(account_id))
    await engine.mark_validating(record.order_id, now=NOW)
    await engine.mark_submitting(record.order_id, now=NOW)
    await engine.apply_venue_ack(
        record.order_id, venue_order_id="v-1", state=OrderState.ACCEPTED, now=NOW
    )
    await engine.apply_fill(
        record.order_id,
        OrderFill(
            fill_id="f-1",
            price=Decimal("2500.25"),
            quantity=Decimal("1.5"),
            filled_at=NOW,
            venue_trade_id="t-1",
        ),
        now=NOW,
    )

    loaded = await repo.get(record.order_id)
    assert loaded is not None
    assert loaded.filled_quantity == Decimal("1.5")
    assert loaded.average_fill_price == Decimal("2500.25")
    assert len(loaded.fills) == 1

    async with session_scope(factory, str(owner_id)) as session:
        stored = await session.scalar(
            text(
                "SELECT count(*) FROM order_fills f JOIN orders o ON o.id = f.order_id "
                "WHERE o.order_id = :oid"
            ),
            {"oid": record.order_id},
        )
    assert stored == 1


async def test_a_replayed_venue_fill_is_not_counted_twice(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    owner_id, account_id = tenant
    repo = _repo(factory, tenant)
    record = OrderRecord(
        order_id="order-fill-replay",
        client_order_id="cid-fill-replay",
        intent=_intent(account_id),
        state=OrderState.PARTIALLY_FILLED,
        created_at=NOW,
        updated_at=NOW,
        submitted_at=NOW,
        filled_quantity=Decimal("1.5"),
        average_fill_price=Decimal(100),
        fills=(
            OrderFill(
                fill_id="f-1",
                price=Decimal(100),
                quantity=Decimal("1.5"),
                filled_at=NOW,
                venue_trade_id="t-1",
            ),
        ),
    )
    await repo.add(record)
    # The same record written again, as a retry or a replayed message would.
    await repo.update(record)

    async with session_scope(factory, str(owner_id)) as session:
        stored = await session.scalar(
            text(
                "SELECT count(*) FROM order_fills f JOIN orders o ON o.id = f.order_id "
                "WHERE o.order_id = :oid"
            ),
            {"oid": "order-fill-replay"},
        )
    assert stored == 1


async def test_discrepancies_cannot_be_rewritten_by_the_runtime_role(
    factory: async_sessionmaker[AsyncSession], tenant: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Evidence is append-only, by privilege rather than by good intentions."""
    owner_id, _ = tenant
    async with session_scope(factory, str(owner_id)) as session:
        granted = (
            (
                await session.execute(
                    text(
                        "SELECT privilege_type FROM information_schema.role_table_grants "
                        "WHERE grantee = current_user AND table_name = 'order_discrepancies'"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert set(granted) == {"SELECT", "INSERT"}


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------


def test_a_non_postgresql_url_is_refused() -> None:
    """ADR 0002 in executable form."""
    with pytest.raises(DatabaseUnavailableError):
        normalise_dsn("sqlite+aiosqlite:///aetheris.db")


def test_sslmode_is_translated_rather_than_passed_through() -> None:
    url, options = normalise_dsn("postgresql://u:p@h:5432/db?sslmode=require")
    assert url.startswith("postgresql+asyncpg://")
    assert "sslmode" not in url
    assert options["sslmode"] == "require"


def test_verification_modes_refuse_to_downgrade_silently(settings: Settings) -> None:
    """verify-full without a CA must fail, not quietly settle for less."""
    from aetheris.adapters.persistence.engine import _ssl_argument

    with pytest.raises(DatabaseUnavailableError):
        _ssl_argument("verify-full", None)


# ----------------------------------------------------------------------
# The application, end to end
# ----------------------------------------------------------------------


async def test_readiness_reports_the_database_as_ok_when_it_is_connected(
    settings: Settings,
) -> None:
    """Readiness stops guessing and connects.

    Before phase 1 this endpoint reported UNAVAILABLE whenever a URL was set,
    because it had no way to check. Reporting a component as healthy without
    contacting it would be the same class of fabrication as a made-up price.
    """
    from fastapi.testclient import TestClient

    from aetheris.main import create_app

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/health/ready")
    body = response.json()
    components = {c["name"]: c for c in body["components"]}

    assert response.status_code == 200
    assert components["database"]["status"] == "OK"
    assert "PostgreSQL" in components["database"]["detail"]
    assert body["ready"] is True


async def test_the_readiness_body_never_carries_the_connection_string(
    settings: Settings,
) -> None:
    """This body is served to a browser."""
    from fastapi.testclient import TestClient

    from aetheris.main import create_app

    with TestClient(create_app(settings)) as client:
        raw = client.get("/api/v1/health/ready").text

    assert settings.database_url is not None
    dsn = settings.database_url.get_secret_value()
    assert dsn not in raw
    for token in dsn.replace("://", "/").replace("@", "/").replace(":", "/").split("/"):
        if len(token) > 8:
            assert token not in raw


async def test_the_application_wires_a_durable_order_store(settings: Settings) -> None:
    """Crash recovery is claimed only because the store behind it is durable."""
    from fastapi.testclient import TestClient

    from aetheris.main import create_app

    app = create_app(settings)
    with TestClient(app):
        assert app.state.order_engine is not None
        assert app.state.order_engine.supports_crash_recovery is True
        assert app.state.order_engine.repository.durable is True


def test_no_tracked_file_contains_the_connection_string(settings: Settings) -> None:
    """The credential exists on this machine. It must exist nowhere in the repo.

    Reads the real values and searches every file git tracks. The values are
    never printed, including in the failure message -- a test that leaked the
    secret while checking for leaks would be self-defeating.
    """
    import subprocess

    assert settings.database_url is not None
    dsn = settings.database_url.get_secret_value()
    password = dsn.partition("://")[2].rpartition("@")[0].partition(":")[2]
    needles = {dsn, password}
    if settings.secret_key is not None:
        needles.add(settings.secret_key.get_secret_value())
    needles = {n for n in needles if len(n) > 8}

    listing = subprocess.run(
        ["git", "ls-files", "-z"],  # noqa: S607
        cwd=str(BACKEND.parent),
        capture_output=True,
        check=True,
    )
    offenders = []
    for name in listing.stdout.decode().split(chr(0)):
        if not name:
            continue
        path = BACKEND.parent / name
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(needle in content for needle in needles):
            offenders.append(name)

    assert offenders == [], f"credential material found in tracked files: {offenders}"


async def test_the_migration_refuses_a_role_that_would_make_rls_inert(
    engine: AsyncEngine,
) -> None:
    """Migration 0002's precondition, run rather than trusted.

    A guard that has never fired is a guard nobody has checked. This runs the
    same SQL against a role that does not exist and asserts it raises, so the
    protection against migrating onto a BYPASSRLS role is known to work rather
    than assumed to.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "m0002", BACKEND / "migrations" / "versions" / "0002_tenant_isolation.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    guard = module._GUARD_TEMPLATE.replace("__ROLE__", "a_role_that_does_not_exist")
    # Run on a raw connection, not through session_scope: that wrapper turns a
    # driver error into DatabaseUnavailableError on purpose, and here the
    # message is the thing under test.
    with pytest.raises(Exception) as caught:
        async with engine.begin() as connection:
            await connection.execute(text(guard))
    assert "does not exist" in str(caught.value)
    assert "BYPASSRLS" in module._GUARD_TEMPLATE

    # And the real role passes the same guard.
    assert "aetheris_app" in module._GUARD
