# Local setup

## Verified environment

This project was bootstrapped and tested on:

| Tool | Version | Notes |
|---|---|---|
| Windows | 11 Home SL 10.0.26200 | |
| Python | 3.14.6 | `C:\Python314` |
| Node | 24.21.0 | frontend, phase 3 |
| pnpm | 11.18.0 | preferred over npm |
| Git | 2.55.0 | |
| PostgreSQL | **not installed** | required from phase 1 |
| Docker | **not installed** | optional |

## Backend

```bash
cd backend
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"    # Windows
# source .venv/bin/activate && pip install -e ".[dev]"  # POSIX
```

Run the API:

```bash
.venv/Scripts/python.exe -m uvicorn aetheris.main:app --reload
```

- Interactive docs: http://127.0.0.1:8000/docs
- Liveness: http://127.0.0.1:8000/api/v1/health/live
- What this build can actually do: http://127.0.0.1:8000/api/v1/system/capabilities

## Configuration

Copy the template and edit:

```bash
cp .env.example .env
```

`.env` is gitignored. Never commit real credentials. Exchange keys stay
server-side and are never exposed to the frontend.

## Quality gates

All of these must pass before a phase is considered complete:

```bash
cd backend
.venv/Scripts/python.exe -m pytest -q          # tests
.venv/Scripts/python.exe -m ruff check .       # lint
.venv/Scripts/python.exe -m ruff format --check .
.venv/Scripts/python.exe -m mypy               # strict type check

cd ../frontend
pnpm test                                      # vitest
pnpm lint                                      # eslint
pnpm typecheck                                 # tsc --noEmit
pnpm build                                     # next build
```

From phase 3 onward a phase that touches the terminal also needs **actual
browser verification** — the page loaded in a real browser at several viewport
widths, with a real backend behind it. Three defects in this project were
invisible to every test and visible immediately in a browser: a CORS origin
mismatch between `localhost` and `127.0.0.1`, a stale build served by an
orphaned dev server, and a freshness rule that blanked every price on the
terminal.

## Paper trading settings

Paper trading is enabled by default and simulates only — no order is sent to
any exchange and no credential is required or accepted.

| Variable | Default | Meaning |
|---|---|---|
| `AETHERIS_PAPER_TRADING_ENABLED` | `true` | Whether paper mode may be entered |
| `AETHERIS_RISK_PAPER_STARTING_BALANCE` | `100` | Opening balance, USDT |
| `AETHERIS_RISK_DAILY_PROFIT_TARGET` | `20` | Locks new entries for the UTC day |
| `AETHERIS_RISK_DAILY_LOSS_LIMIT` | `-10` | Locks new entries for the UTC day |
| `AETHERIS_RISK_MAX_OPEN_POSITIONS` | `5` | |
| `AETHERIS_RISK_MAX_DATA_AGE_SECONDS` | `30` | Older than this cannot fill an order |
| `AETHERIS_PAPER_TAKER_FEE_BPS` | `5` | Per side, on notional |
| `AETHERIS_PAPER_SLIPPAGE_BPS` | `2` | Only when the venue publishes no book |

**Paper state is in-memory and is lost when the backend restarts.** That is
reported on every account response and in the capability registry; it is not a
bug to be worked around but the honest state of phase 6.

### Sizing on a 100 USDT account

Venue filters are real and are honoured rather than approximated, which has a
practical consequence worth knowing before the first order is refused:
BTCUSDT's step size is 0.001 BTC — roughly 80 USDT per increment — so a
BTCUSDT position at 1x needs about 82+ USDT of margin, and anything smaller
truncates to zero and is refused with `RISK_REJECTED_EXCHANGE_PRECISION`.
ETHUSDT publishes a 20 USDT minimum notional.

Either pick an instrument where `step_size × price` is small, or raise the
balance:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/paper/reset   -H "Content-Type: application/json" -d '{"starting_balance": "1000"}'
```

## Live market-data smoke test (opt-in)

CI never depends on Binance being reachable. To verify real connectivity by
hand:

```bash
cd backend
.venv/Scripts/python.exe -m aetheris.tools.binance_smoke
```

Public endpoints only, no credentials, no orders. It exits non-zero printing
`BINANCE_UNAVAILABLE` rather than inventing a result if the venue is
unreachable.

## Known environment constraints

- **~1 GB free RAM** on the development machine (7.7 GB total, i3-1215U).
  Avoid running the backend test suite, a Next.js dev server and a database
  simultaneously.
- **No PostgreSQL and no Docker.** Phase 1 cannot start until a database is
  available; see the options recorded in `docs/adr/0002-database-hosting.md`.
  Phase 2 needs no database: nothing is persisted, and market data is fetched
  live and cached in-process.
- **Outbound HTTPS to `fapi.binance.com`** is required for live market data.
  The mocked test suite does not need it.
