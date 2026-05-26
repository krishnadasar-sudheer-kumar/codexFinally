# Subagent Team Implementation Contract

This document records the coordination contract for the `subagent-teams` branch.

## Branch

- Integration branch: `subagent-teams`
- Merge target after acceptance: `main`

## Workstreams

| Role | Primary Ownership |
|---|---|
| Technical Lead / Integration Coordinator | Cross-team contracts, integration, final verification |
| Database Engineer | `backend/app/db/`, DB repository tests |
| Backend API Engineer | `backend/app/main.py`, API schemas/services/routes, API tests |
| LLM Engineer | `backend/app/llm.py`, LLM parsing/mock tests |
| Frontend Engineer | `frontend/` |
| DevOps / E2E Engineer | `Dockerfile`, `docker-compose.yml`, `.env.example`, `scripts/`, `test/` |

## Backend Contracts

The API layer should use the database layer through functions exported from `backend/app/db`.

Expected database responsibilities:

- Initialize schema idempotently.
- Seed the default user, default watchlist, and initial portfolio snapshot.
- Store UTC ISO 8601 timestamps.
- Expose profile, watchlist, positions, trades, portfolio snapshot, and chat-message repository helpers.

The API layer should use the LLM layer through:

```python
await generate_chat_plan(message, portfolio_context, history, mock=None)
```

The returned object should include:

- `message: str`
- `trades: list[{ticker, side, quantity}]`
- `watchlist_changes: list[{ticker, action}]`

## API Contract

The public API remains the one in `planning/PLAN.md` section 8:

- `GET /api/health`
- `GET /api/stream/prices`
- `GET /api/watchlist`
- `POST /api/watchlist`
- `DELETE /api/watchlist/{ticker}`
- `GET /api/portfolio`
- `GET /api/portfolio/history`
- `POST /api/portfolio/trade`
- `POST /api/chat`

All manual and LLM-generated trades use the same validation:

- Supported side: `buy` or `sell`
- Positive fractional quantity
- Sufficient cash for buys
- Sufficient shares for sells
- Cached market price must exist

## Verification Target

Before this branch is considered ready:

- Backend tests pass.
- Frontend build passes if dependencies are available.
- Docker build path is documented and smoke-testable.
- E2E scaffold exists and runs in mock LLM mode once the app container is available.
