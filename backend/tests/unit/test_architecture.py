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


def test_no_order_endpoints_are_referenced_anywhere() -> None:
    """Phase 2 must not even name a Binance order path."""
    order_paths = ("/fapi/v1/order", "/fapi/v1/batchOrders", "/fapi/v2/account")
    for path in PACKAGE_ROOT.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for order_path in order_paths:
            assert order_path not in source, (
                f"{path.relative_to(PACKAGE_ROOT)} references {order_path}"
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


def test_no_testnet_or_live_execution_path_exists() -> None:
    """Phase 6 is paper only. Nothing may reference a venue trading host."""
    banned = ("testnet.binance", "testnet.binancefuture", "fapi.binance.com/fapi/v1/order")
    for path in PACKAGE_ROOT.rglob("*.py"):
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
        if path.name == "paper.py":
            assert writes == ["router.post"], (
                "the paper router may declare POST routes and no other verb"
            )
            continue
        assert not writes, (
            f"{path.relative_to(PACKAGE_ROOT)} declares {writes}; state-changing "
            "routes exist only under the paper namespace"
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
