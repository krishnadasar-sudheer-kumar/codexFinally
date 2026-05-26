# Subagent Teams Progress Checkpoint

Date: 2026-05-26
Branch: `subagent-teams`

## Current Status

The first integrated subagent pass has landed a working FinAlly application shell around the completed market data backend. The branch now includes backend API routes, SQLite persistence helpers, an LLM planning adapter, a Next.js frontend workstation, Docker packaging, and Playwright E2E coverage.

This file is intentionally a durable checkpoint so future work can resume cleanly if an agent session stops unexpectedly.

## Implemented

- Backend API:
  - FastAPI application factory and API router.
  - Health, watchlist, portfolio, portfolio history, trade execution, and chat endpoints.
  - SSE price streaming mounted from the existing market data package.
  - Unified service layer connecting DB, market cache/source, trade guardrails, and LLM-driven actions.

- Database:
  - SQLite repository helpers for user profile, watchlist, positions, trades, portfolio snapshots, and chat history.
  - Default user bootstrap with initial cash, default watchlist, and initial snapshot.
  - Idempotent watchlist mutations and fractional-share position math.

- LLM:
  - Deterministic mock mode for local, CI, and E2E use.
  - OpenAI SDK-backed structured response path when `OPENAI_API_KEY` is configured.
  - Structured JSON parsing and validation for chat plans.
  - Trade action quantities now support fractional shares to match the backend trading API.

- Frontend:
  - Static-exportable Next.js workstation served by the backend container.
  - Live watchlist, streaming price updates, portfolio metrics, trade ticket, positions, P&L history, and assistant chat.
  - Watchlist add/remove and buy/sell flows connected to the backend API.
  - Chat action errors are formatted safely instead of rendering structured error objects directly.

- DevOps and integration:
  - Dockerfile for combined frontend static build plus backend runtime.
  - `docker-compose.yml` for local app runs.
  - `test/docker-compose.test.yml` for Dockerized Playwright E2E runs.
  - Helper scripts for backend tests, frontend checks, and local container workflow.
  - Environment template in `.env.example`.

## Verification Passed

- Backend tests: `uv run --extra dev pytest -q`
  - Result: `134 passed`

- Backend lint: `uv run --extra dev ruff check app tests`
  - Result: `All checks passed`

- Frontend typecheck: `npm run typecheck`
  - Result: passed

- Frontend production/static build: `npm run build`
  - Result: passed

- Docker compose config:
  - `docker compose config --quiet`
  - `docker compose -f test/docker-compose.test.yml config --quiet`
  - Result: passed

- Dockerfile check:
  - `docker build --check .`
  - Result: passed

- Production image build:
  - `docker build -t finally:local .`
  - Result: passed

- Dockerized E2E:
  - `docker compose -f test/docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from playwright`
  - Result: `5 passed`

## Notes And Caveats

- The Playwright test container currently reports two high-severity npm audit findings from its small test dependency set. The E2E tests pass, but this should be reviewed before relying on that package set in CI.
- The service layer keeps a compatibility adapter around the DB helper module. This was useful for parallel-agent integration, but a later cleanup can remove duplication once repository ownership stabilizes.
- The app is still a local-first simulated trading product. Real brokerage execution, authentication, user management, and production secrets management are not implemented yet.
- Massive market data fallback behavior remains as designed in the market data documentation: use live/realtime when entitled, fall back to end-of-day where available, otherwise use the simulator.

## Recommended Next Work

- Add authentication and per-user session boundaries before any hosted deployment.
- Add CI workflow definitions to run backend tests, frontend checks, image build, and Dockerized Playwright E2E.
- Decide whether to keep the service-layer DB adapter or collapse onto the dedicated `backend/app/db` repository module.
- Add more E2E coverage for chart rendering, SSE reconnection behavior, and explicit API error banners.
