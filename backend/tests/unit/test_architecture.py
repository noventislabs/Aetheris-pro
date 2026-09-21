"""Architectural invariants, enforced rather than documented.

Layering and the read-only guarantee are the two properties most likely to
erode quietly under delivery pressure, so both are asserted here. A violation
fails the suite in the commit that introduces it, not in review six weeks
later.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

import aetheris
from aetheris.adapters.exchange.binance.adapter import BinanceFuturesMarketDataAdapter
from aetheris.adapters.exchange.ports import MarketDataPort, TradingPort

PACKAGE_ROOT = pathlib.Path(aetheris.__file__).parent

#: The pure foundation may not know about transport, frameworks or venues.
FORBIDDEN_PREFIXES = (
    "httpx",
    "fastapi",
    "starlette",
    "binance",
    "aetheris.adapters",
    "aetheris.api",
    "aetheris.services",
    "aetheris.tools",
)

#: Layers that must stay free of I/O, frameworks and venue knowledge.
#: `analysis` joins them in phase 3: scanner statistics are arithmetic over
#: candles, and keeping them pure is what makes them testable without a
#: network and reusable by the phase 5 backtester.
PURE_PACKAGES = ("core", "domain", "analysis")


def imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def pure_source_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for package in PURE_PACKAGES:
        files.extend(sorted((PACKAGE_ROOT / package).rglob("*.py")))
    return files


def test_there_are_pure_modules_to_check() -> None:
    # Guards against the suite passing because the glob silently matched nothing.
    assert len(pure_source_files()) >= 6


@pytest.mark.parametrize("path", pure_source_files(), ids=lambda p: p.name)
def test_core_and_domain_stay_free_of_exchange_and_framework_imports(
    path: pathlib.Path,
) -> None:
    offenders = {
        module for module in imported_modules(path) if module.startswith(FORBIDDEN_PREFIXES)
    }
    assert not offenders, (
        f"{path.relative_to(PACKAGE_ROOT)} imports {sorted(offenders)}; "
        "core/ and domain/ must stay independent of transport, frameworks and venues"
    )


def test_venue_names_do_not_leak_into_the_pure_layers() -> None:
    """No venue vocabulary in the pure layers.

    Two files are exempt, both of which describe deployments rather than
    implement venue behaviour: ``core/config.py`` is the composition surface
    where an operator names a venue and its URL, and ``core/capabilities.py``
    must name the venue whose adapter it reports on -- a registry that could
    not say which exchange is wired up would not be a disclosure at all.

    Both remain covered by the import test above, which is what actually
    guarantees no venue *code* reaches the pure layers.
    """
    exempt = {
        PACKAGE_ROOT / "core" / "config.py",
        PACKAGE_ROOT / "core" / "capabilities.py",
    }
    for path in pure_source_files():
        if path in exempt:
            continue
        source = path.read_text(encoding="utf-8").lower()
        assert "binance" not in source, (
            f"{path.relative_to(PACKAGE_ROOT)} mentions a specific venue; "
            "the pure layers must stay venue-agnostic"
        )


# ----------------------------------------------------------------------
# Read-only guarantee
# ----------------------------------------------------------------------


def test_market_data_adapter_implements_only_the_read_only_port() -> None:
    assert issubclass(BinanceFuturesMarketDataAdapter, MarketDataPort)


def test_no_adapter_satisfies_the_trading_port() -> None:
    """Nothing in this build can place, cancel or inspect an order.

    TradingPort is a declared type with no implementation. If an adapter ever
    starts satisfying it outside the execution phases, this fails.
    """
    adapter = BinanceFuturesMarketDataAdapter.__new__(BinanceFuturesMarketDataAdapter)
    assert not isinstance(adapter, TradingPort)


@pytest.mark.parametrize(
    "method",
    ["create_order", "cancel_order", "get_order", "get_balance", "get_positions"],
)
def test_adapter_has_no_execution_methods(method: str) -> None:
    assert not hasattr(BinanceFuturesMarketDataAdapter, method)


#: The one module allowed to name a venue order path. Phase 8b narrowed a
#: package-wide ban to a single file rather than deleting it: "no order path
#: anywhere" was the right rule while nothing executed, and "order paths live
#: in exactly one place" is the strongest rule still available once something
#: does. A path appearing anywhere else is the regression worth catching.
VENUE_PATH_MODULE = "adapters/exchange/binance/testnet_endpoints.py"


def test_venue_order_paths_appear_in_exactly_one_module() -> None:
    """A Binance order path may be named in the testnet endpoint map, nowhere else.

    Confinement, not absence. Every other module -- the paper engine, the risk
    engine, the market-data adapter, every route -- must still be incapable of
    naming an order endpoint, which is what keeps the execution surface a
    single reviewable file.
    """
    order_paths = (
        "/fapi/v1/order",
        "/fapi/v1/batchOrders",
        "/fapi/v1/leverage",
        "/fapi/v1/marginType",
        "/fapi/v1/leverageBracket",
        "/fapi/v3/account",
    )
    for path in PACKAGE_ROOT.rglob("*.py"):
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        if relative == VENUE_PATH_MODULE:
            continue
        source = path.read_text(encoding="utf-8")
        for order_path in order_paths:
            assert order_path not in source, (
                f"{relative} references {order_path}; venue order paths belong only "
                f"in {VENUE_PATH_MODULE}"
            )


# ----------------------------------------------------------------------
# Phase 4: the analysis engine must stay reusable and inert
# ----------------------------------------------------------------------

ANALYSIS_ROOT = PACKAGE_ROOT / "analysis"

#: The engine is the phase 5 backtester's entry point. It must run over
#: historical bars with none of this standing up, so it may not even import
#: configuration -- settings are a deployment concern, not an arithmetic one.
ANALYSIS_FORBIDDEN = (*FORBIDDEN_PREFIXES, "aetheris.core.config")


def analysis_source_files() -> list[pathlib.Path]:
    return sorted(ANALYSIS_ROOT.rglob("*.py"))


def test_there_are_analysis_modules_to_check() -> None:
    assert len(analysis_source_files()) >= 8


@pytest.mark.parametrize("path", analysis_source_files(), ids=lambda p: p.name)
def test_analysis_layer_is_pure(path: pathlib.Path) -> None:
    offenders = {
        module for module in imported_modules(path) if module.startswith(ANALYSIS_FORBIDDEN)
    }
    assert not offenders, (
        f"{path.relative_to(PACKAGE_ROOT)} imports {sorted(offenders)}; the analysis "
        "engine must stay callable from a backtester with no HTTP, framework, venue "
        "or settings machinery"
    )


def test_indicator_engine_imports_without_a_framework() -> None:
    """The §21 contract, asserted by actually doing it."""
    from aetheris.analysis.indicators.engine import calculate_indicators
    from aetheris.analysis.strategies.registry import evaluate_from_candles

    assert callable(calculate_indicators)
    assert callable(evaluate_from_candles)


def test_no_dynamic_execution_anywhere_in_the_package() -> None:
    """No eval, exec, compile or __import__ reachable from a request.

    Indicator and strategy selection are whitelists of registered callables;
    a parameter can choose *which* registered maths runs, never *what* runs.
    """
    banned = ("eval(", "exec(", "compile(", "__import__(", "os.system", "subprocess")
    for path in PACKAGE_ROOT.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for token in banned:
            assert token not in source, f"{path.relative_to(PACKAGE_ROOT)} contains {token!r}"


def test_indicator_registry_cannot_resolve_an_arbitrary_name() -> None:
    from aetheris.analysis.indicators.registry import get_spec

    for hostile in ("__import__", "builtins.eval", "os.system", "../../etc/passwd"):
        assert get_spec(hostile) is None


def test_strategy_registry_is_a_fixed_tuple() -> None:
    from aetheris.analysis.strategies.registry import STRATEGY_KEYS

    assert STRATEGY_KEYS == ("trend_momentum",)


# ----------------------------------------------------------------------
# Phase 6: the paper engine must stay incapable of becoming live trading
# ----------------------------------------------------------------------

ENGINES_ROOT = PACKAGE_ROOT / "engines"

#: The engine is handed prices and does arithmetic. It may use core, domain
#: and analysis, and nothing that could reach a network or a venue. That is
#: what makes it impossible for a bug in simulated execution to become a real
#: order, rather than merely unlikely.
ENGINE_FORBIDDEN = FORBIDDEN_PREFIXES


def engine_source_files() -> list[pathlib.Path]:
    return sorted(ENGINES_ROOT.rglob("*.py"))


def test_there_are_engine_modules_to_check() -> None:
    assert len(engine_source_files()) >= 5


@pytest.mark.parametrize("path", engine_source_files(), ids=lambda p: p.name)
def test_engines_layer_is_pure(path: pathlib.Path) -> None:
    offenders = {module for module in imported_modules(path) if module.startswith(ENGINE_FORBIDDEN)}
    assert not offenders, (
        f"{path.relative_to(PACKAGE_ROOT)} imports {sorted(offenders)}; the paper "
        "engine must have no route to a transport, a framework or a venue"
    )


def test_venue_names_do_not_leak_into_the_engine() -> None:
    for path in engine_source_files():
        source = path.read_text(encoding="utf-8").lower()
        assert "binance" not in source, (
            f"{path.relative_to(PACKAGE_ROOT)} mentions a specific venue; the paper "
            "engine simulates against prices, not against a named exchange"
        )


@pytest.mark.parametrize(
    "token",
    [
        "api_key",
        "apikey",
        "api_secret",
        "secret_key",
        "signature",
        "hmac",
        "withdraw",
        "X-MBX-APIKEY",
    ],
)
def test_paper_engine_names_no_credential_or_withdrawal_concept(token: str) -> None:
    """Paper trading needs no credential, so none may appear even as a name.

    A field called ``api_key`` on a simulated order would be the first step to
    one being read, and withdrawal permissions must never be required by
    anything in this system, in any mode.
    """
    for path in engine_source_files():
        source = path.read_text(encoding="utf-8").lower()
        assert token.lower() not in source, f"{path.relative_to(PACKAGE_ROOT)} names {token}"


def test_no_live_execution_path_exists() -> None:
    """Live execution must be unreachable, and the legacy testnet host unused.

    The production host may still be named -- it is the public market-data base
    URL -- but never together with an order path, and the superseded testnet
    host may not appear at all: a fallback that "still works" is how a
    configuration slip becomes real money.
    """
    banned = (
        "testnet.binancefuture",
        "fapi.binance.com/fapi/v1/order",
    )
    # The composition surface is exempt, as it is for the venue-name test, and
    # for a sharper reason here: config.py names the superseded host and the
    # production host precisely in order to **refuse** them. Honest code has to
    # mention a thing to deny it, so the denial is asserted separately below
    # rather than the mention being banned.
    exempt = {PACKAGE_ROOT / "core" / "config.py"}
    for path in PACKAGE_ROOT.rglob("*.py"):
        if path in exempt:
            continue
        source = path.read_text(encoding="utf-8").lower()
        for token in banned:
            assert token not in source, f"{path.relative_to(PACKAGE_ROOT)} references {token}"


def test_only_the_paper_router_declares_a_write_route() -> None:
    """Asserted against the source, not just the served OpenAPI document.

    An endpoint added to another router with the decorator commented out, or
    behind a feature flag, would not appear in the served document but is
    exactly the change worth catching at review time.
    """
    for path in sorted((PACKAGE_ROOT / "api").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        writes = [
            verb
            for verb in ("router.post", "router.put", "router.patch", "router.delete")
            if verb in source
        ]
        if path.name in ("paper.py", "testnet.py"):
            assert writes == ["router.post"], (
                f"{path.name} may declare POST routes and no other verb. Cancellation "
                "is a POST to a sub-path: the venue's own cancel is an HTTP DELETE, "
                "but that is the adapter's business and does not belong in this API's "
                "verb surface."
            )
            continue
        assert not writes, (
            f"{path.relative_to(PACKAGE_ROOT)} declares {writes}; state-changing "
            "routes exist only under the paper and testnet namespaces"
        )


def test_the_trading_port_still_has_no_implementation_after_paper_shipped() -> None:
    """The guarantee that makes paper trading structurally safe.

    A paper order is a record in memory because there is nothing to send it
    to -- not because a flag says not to send it.
    """
    from aetheris.engines.paper.engine import PaperEngine

    engine = PaperEngine.__new__(PaperEngine)
    assert not isinstance(engine, TradingPort)
    for method in ("create_order", "cancel_order", "get_balance", "get_positions"):
        assert not hasattr(BinanceFuturesMarketDataAdapter, method)


# ----------------------------------------------------------------------
# Phase 7: the risk engine and the autonomous loop
# ----------------------------------------------------------------------

RISK_ROOT = PACKAGE_ROOT / "engines" / "risk"


def risk_source_files() -> list[pathlib.Path]:
    return sorted(RISK_ROOT.rglob("*.py"))


def test_there_are_risk_engine_modules_to_check() -> None:
    assert len(risk_source_files()) >= 4


@pytest.mark.parametrize("path", risk_source_files(), ids=lambda p: p.name)
def test_the_risk_engine_is_pure(path: pathlib.Path) -> None:
    """The final authority must be callable with no network and no framework.

    That is what makes the whole envelope testable in-process, and it is why
    the autonomous loop cannot reach an exchange through it.
    """
    offenders = {
        module for module in imported_modules(path) if module.startswith(FORBIDDEN_PREFIXES)
    }
    assert not offenders, (
        f"{path.relative_to(PACKAGE_ROOT)} imports {sorted(offenders)}; the risk "
        "engine must stay free of transport, frameworks and venues"
    )


def test_venue_names_do_not_leak_into_the_risk_engine() -> None:
    for path in risk_source_files():
        source = path.read_text(encoding="utf-8").lower()
        assert "binance" not in source, (
            f"{path.relative_to(PACKAGE_ROOT)} mentions a specific venue"
        )


#: Words that would misrepresent what this system produces. A deterministic
#: reading of named conditions is not a confidence, and a risk ceiling derived
#: from stop distance is not a prediction. Phase 7 is where that pressure is
#: highest, because an autonomous loop invites being described as if it knew
#: something.
FORBIDDEN_VOCABULARY = (
    "confidence",
    "probability of profit",
    "win probability",
    "win rate",
    "expected return",
    "prediction",
    "predicted",
    "forecast",
    "ai-selected",
    "guaranteed",
)

PHASE_7_MODULES = (
    PACKAGE_ROOT / "engines" / "risk",
    PACKAGE_ROOT / "services" / "autonomous.py",
    PACKAGE_ROOT / "domain" / "autonomous.py",
)


def phase_7_source_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for target in PHASE_7_MODULES:
        files.extend(sorted(target.rglob("*.py")) if target.is_dir() else [target])
    return files


@pytest.mark.parametrize("term", FORBIDDEN_VOCABULARY)
def test_phase_7_modules_never_claim_to_predict(term: str) -> None:
    """Asserted against the source, including comments and docstrings.

    A docstring that describes a condition count as a confidence is how the
    misreading spreads: it gets copied into an API description, then into a UI
    string, then into a screenshot. The words are banned at the source.

    Denials are permitted -- a module may say it is *not* a prediction -- so the
    check looks for the term outside a negating context. The previous line is
    included in that context because these denials are prose and wrap.
    """
    negations = ("not", "never", "no ", "nor ", "rather than", "cannot", "without")
    for path in phase_7_source_files():
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, 1):
            lowered = line.lower()
            if term not in lowered:
                continue
            context = (lines[number - 2].lower() if number >= 2 else "") + " " + lowered
            assert any(marker in context for marker in negations), (
                f"{path.relative_to(PACKAGE_ROOT)}:{number} uses {term!r} without "
                f"denying it: {line.strip()!r}"
            )


def test_the_autonomous_loop_cannot_reach_a_venue_order_endpoint() -> None:
    """The loop acts on its own, so this guarantee matters more here than anywhere."""
    from aetheris.services.autonomous import AutonomousLoop

    loop = AutonomousLoop.__new__(AutonomousLoop)
    assert not isinstance(loop, TradingPort)
    for method in ("create_order", "cancel_order", "get_balance", "get_positions"):
        assert not hasattr(AutonomousLoop, method)


def test_the_autonomous_loop_names_no_credential_or_withdrawal_concept() -> None:
    source = (PACKAGE_ROOT / "services" / "autonomous.py").read_text(encoding="utf-8").lower()
    for token in ("api_key", "apikey", "api_secret", "signature", "hmac", "withdraw"):
        assert token not in source, f"services/autonomous.py names {token}"


def test_the_autonomous_loop_depends_on_the_paper_engine_not_an_execution_port() -> None:
    """The structural barrier between paper autonomy and future live execution.

    There is deliberately no ``ExecutionPort`` a testnet adapter could later
    satisfy. Pointing this loop at a venue requires writing new code and
    changing its type, not flipping a config value -- which is the difference
    between a deliberate phase and an accident.
    """
    import inspect

    from aetheris.services.autonomous import AutonomousLoop
    from aetheris.services.paper import PaperTradingService

    signature = inspect.signature(AutonomousLoop.__init__)
    annotation = signature.parameters["paper"].annotation
    assert annotation in (PaperTradingService, "PaperTradingService")

    source = (PACKAGE_ROOT / "services" / "autonomous.py").read_text(encoding="utf-8")
    assert "ExecutionPort" not in source
    # TradingPort may be *named* -- the module docstring says none exists to
    # inject, which is the point -- but it must never be imported.
    for line in source.splitlines():
        if line.startswith(("import ", "from ")):
            assert "TradingPort" not in line, "the loop must not import an execution port"


def test_the_autonomous_loop_takes_no_trading_mode_parameter() -> None:
    """The mode is not a parameter, so there is no ``mode=LIVE`` to pass."""
    import inspect

    from aetheris.services.autonomous import AutonomousLoop

    for name, method in inspect.getmembers(AutonomousLoop, inspect.isfunction):
        if name.startswith("__"):
            continue
        for parameter in inspect.signature(method).parameters.values():
            assert "TradingMode" not in str(parameter.annotation), (
                f"AutonomousLoop.{name} takes a trading mode; autonomy is a paper "
                "component and must not be pointed at another mode by argument"
            )


# ----------------------------------------------------------------------
# Phase 8a: the order engine, and the boundary it must not cross yet
# ----------------------------------------------------------------------

ORDER_ROOT = PACKAGE_ROOT / "engines" / "order"


def order_source_files() -> list[pathlib.Path]:
    return sorted(ORDER_ROOT.rglob("*.py"))


def test_there_are_order_engine_modules_to_check() -> None:
    assert len(order_source_files()) >= 5


@pytest.mark.parametrize("path", order_source_files(), ids=lambda p: p.name)
def test_the_order_engine_is_pure(path: pathlib.Path) -> None:
    """It drives a lifecycle; it does not reach a venue.

    Phase 8c attaches an adapter to this. That the lifecycle itself cannot
    import a transport is what keeps the attachment a deliberate act rather
    than something that could happen by accident.
    """
    offenders = {
        module for module in imported_modules(path) if module.startswith(FORBIDDEN_PREFIXES)
    }
    assert not offenders, (
        f"{path.relative_to(PACKAGE_ROOT)} imports {sorted(offenders)}; the order "
        "lifecycle must stay free of transport, frameworks and venues"
    )


def test_venue_names_do_not_leak_into_the_order_engine() -> None:
    """No exemption. The client-order-id cap is described, not attributed.

    The concrete value belongs with the adapter in phase 8c, where venue facts
    live -- the same resolution phase 4 reached for the leverage brackets.
    """
    for path in order_source_files():
        source = path.read_text(encoding="utf-8").lower()
        assert "binance" not in source, (
            f"{path.relative_to(PACKAGE_ROOT)} mentions a specific venue"
        )


def test_the_order_engine_names_no_credential_or_withdrawal_concept() -> None:
    for path in order_source_files():
        source = path.read_text(encoding="utf-8").lower()
        for token in ("api_key", "apikey", "api_secret", "signature", "hmac", "withdraw"):
            assert token not in source, f"{path.relative_to(PACKAGE_ROOT)} names {token}"


def test_the_only_execution_adapter_is_the_testnet_one() -> None:
    """8b gives the lifecycle somewhere to submit -- exactly one somewhere.

    The statement 8a could make, that nothing executes at all, is no longer
    true. What replaces it is narrower and still worth asserting: the testnet
    adapter reports TESTNET, the market-data adapter still satisfies nothing,
    and no object anywhere claims to execute against LIVE.
    """
    from aetheris.adapters.exchange.binance.testnet_adapter import (
        BinanceTestnetTradingAdapter,
    )
    from aetheris.domain.enums import TradingMode

    adapter = BinanceTestnetTradingAdapter.__new__(BinanceTestnetTradingAdapter)
    assert adapter.venue_mode is TradingMode.TESTNET

    market_data = BinanceFuturesMarketDataAdapter.__new__(BinanceFuturesMarketDataAdapter)
    assert not isinstance(market_data, TradingPort)

    # No execution module may name the live mode at all. config.py and
    # enums.py name it because a mode has to exist to be switched off, and
    # capabilities.py must report on it; none of them can execute.
    exempt_names = {"config.py", "enums.py", "capabilities.py", "models.py"}
    for path in PACKAGE_ROOT.rglob("*.py"):
        if path.name in exempt_names:
            continue
        source = path.read_text(encoding="utf-8")
        assert "TradingMode.LIVE" not in source, (
            f"{path.relative_to(PACKAGE_ROOT)} names TradingMode.LIVE; live "
            "execution is not implemented and nothing may reference its mode"
        )


def test_the_superseded_and_production_hosts_are_named_only_to_refuse_them() -> None:
    """The exemption above is paid for here.

    config.py may mention both hosts, and must do so only inside a rejection.
    Asserting the refusal exists is stronger than asserting the string does
    not: a file that had quietly started *using* one of them would pass a
    substring ban by simply not spelling it the same way.
    """
    from aetheris.core.config import (
        LEGACY_TESTNET_HOST,
        PRODUCTION_HOST,
        TESTNET_ALLOWED_HOST,
        TestnetSettings,
    )

    assert TESTNET_ALLOWED_HOST == "demo-fapi.binance.com"
    for refused in (PRODUCTION_HOST, LEGACY_TESTNET_HOST):
        with pytest.raises(ValueError):
            TestnetSettings(_env_file=None, rest_base_url=f"https://{refused}")  # type: ignore[call-arg]


def test_unknown_cannot_reach_a_resolved_state_without_reconciling() -> None:
    """The safety property of the phase, asserted at the architecture level.

    Duplicated from the state-machine suite deliberately: this is the invariant
    the rest of phase 8 is built on, and it should fail loudly in the file that
    exists to catch erosion.
    """
    from aetheris.domain.enums import OrderState
    from aetheris.engines.order.machine import LEGAL_TRANSITIONS

    assert LEGAL_TRANSITIONS[OrderState.UNKNOWN] == frozenset({OrderState.RECONCILING})


def test_the_order_engine_does_not_claim_crash_recovery_without_a_durable_store() -> None:
    """The protocol is built; the guarantee needs phase 8b's database."""
    from aetheris.engines.order.engine import OrderLifecycleEngine
    from aetheris.engines.order.store import InMemoryOrderRepository

    engine = OrderLifecycleEngine(InMemoryOrderRepository())
    assert engine.supports_crash_recovery is False
