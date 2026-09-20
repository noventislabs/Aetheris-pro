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

All four must pass before a phase is considered complete:

```bash
cd backend
.venv/Scripts/python.exe -m pytest -q          # tests
.venv/Scripts/python.exe -m ruff check .       # lint
.venv/Scripts/python.exe -m ruff format --check .
.venv/Scripts/python.exe -m mypy               # strict type check
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
