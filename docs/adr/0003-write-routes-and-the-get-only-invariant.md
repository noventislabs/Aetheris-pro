# ADR 0003 — Replacing the GET-only route invariant

- Status: accepted
- Date: 2026-09-21
- Supersedes: the flat "no non-GET route exists" assertion of phases 0–5

## Context

Phases 0 through 5 asserted, in four test files and in the architecture test,
that no route in the system uses a method other than `GET`. It was a cheap,
blunt guarantee and it earned its place: while nothing had server state to
mutate, any non-GET route appearing was a signal worth failing a build over.

Phase 6 introduces paper trading. Submitting a paper order genuinely mutates
server state — it creates an order, opens a position, moves a balance.

Three options were considered:

1. **Keep the invariant, model order submission as a GET.** A state change
   behind a GET is cacheable by any intermediary, prefetchable by a browser,
   and repeatable by a reload or a link preview. The property being protected
   would have been traded for a worse one.
2. **Delete the invariant.** Honest, but it removes the check precisely as the
   system acquires the ability to change state — the moment it becomes most
   useful.
3. **Narrow it.** Keep an assertion, but about the thing that actually matters.

## Decision

Option 3. The single flat assertion is replaced by a narrower and stronger
pair, both enforced by tests:

1. **No route in the system can reach a venue order endpoint.** Nothing
   implements `TradingPort`, no venue order path is named anywhere in the
   package, and the `engines/` layer cannot import a transport, a framework or
   an adapter. This is asserted against the *source*, not only the served
   OpenAPI document, so a route behind a feature flag or a commented decorator
   is still caught.
2. **Writes exist only under `/paper`**, acting on in-memory simulation state.
   `GET` and `POST` are the only verbs the system exposes — no `PUT`, `PATCH`
   or `DELETE` anywhere — and CORS advertises exactly those two.

The assertion lives in one place, `tests/fixtures/invariants.py`, and each API
test file checks the *whole* published surface rather than its own slice. A
write added to the scanner router fails the scanner's own test.

## Consequences

- The guarantee a reader actually cares about — "this cannot place a real
  order" — is now asserted directly rather than being implied by a proxy.
- CORS gained `POST`. It is advertised because the routes exist, not in advance
  of them; the rule that a verb is offered only once it is real is unchanged.
- A refusal from the risk gate is a `200` carrying `accepted: false` and a
  `RISK_REJECTED_*` code, not an error status. Callers handle one response
  shape, and refused orders stay in the account history where they are visible.
- Cost: the invariant is now three assertions instead of one, and a future
  phase adding writes outside `/paper` must amend this ADR rather than quietly
  widening a constant.
