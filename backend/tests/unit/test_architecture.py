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
