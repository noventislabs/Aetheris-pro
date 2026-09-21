"""The route-surface invariant, in one place.

Phases 0-5 asserted something simple: no route uses a method other than GET.
That held because a read-only build had nothing to mutate, and it was worth
asserting because "we added a write endpoint" is exactly the kind of change
that slips through review.

Phase 6 makes it false on purpose. Submitting a paper order mutates server
state, and modelling that as a GET would make a state change cacheable,
prefetchable and repeatable by a browser doing ordinary browser things -- a
worse property than the one the invariant protected.

So the assertion becomes narrower and stronger rather than being deleted:

* a write may exist **only** under ``/paper``, where it acts on in-memory
  simulation state;
* the verbs are GET and POST and nothing else -- no PUT, PATCH or DELETE
  anywhere;
* no path may name an order, credential or withdrawal surface.

The companion guarantee -- that nothing in the package can reach a venue order
endpoint at all -- is asserted separately in ``tests/unit/test_architecture.py``
against the source, which is the check that actually keeps paper trading
incapable of becoming live trading.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

#: The only prefix under which a state-changing route may live.
WRITE_NAMESPACE = "/api/v1/paper/"

#: Verbs the system exposes at all.
ALLOWED_METHODS = {"get", "post"}

#: Words that must not appear in any path. "order" is permitted only inside the
#: paper namespace, which is handled separately below.
FORBIDDEN_PATH_TOKENS = ("credential", "api-key", "apikey", "withdraw", "transfer")


def assert_route_surface(client: TestClient) -> None:
    """Assert the whole published route surface, not just one router's slice."""
    paths = client.get("/openapi.json").json()["paths"]
    assert paths, "the OpenAPI document listed no paths at all"

    for path, operations in paths.items():
        methods = set(operations)
        assert methods <= ALLOWED_METHODS, (
            f"{path} exposes {sorted(methods - ALLOWED_METHODS)}; this build has no "
            "PUT, PATCH or DELETE route anywhere"
        )
        if "post" in methods:
            assert path.startswith(WRITE_NAMESPACE), (
                f"{path} is a write outside the paper namespace. State-changing routes "
                f"exist only under {WRITE_NAMESPACE}, against simulation state."
            )
        for token in FORBIDDEN_PATH_TOKENS:
            assert token not in path.lower(), f"{path} names a {token} surface"
        if "order" in path.lower():
            assert path.startswith(WRITE_NAMESPACE), (
                f"{path} names an order surface outside paper simulation"
            )
