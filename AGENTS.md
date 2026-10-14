# AGENTS.md

Instructions for coding agents (including the Sentinela execution line) working in this
repository. The commands below are the ones CI runs (`.github/workflows/backend.yml` and
`web.yml`). Every one of them was run on `main` in a clean clone and passes.

## Setup

- Python ≥ 3.12, < 3.15 (CI uses 3.12) and Poetry ≥ 2.2. Each Python app has its own in-project
  virtualenv (`.venv`, from `poetry.toml`).
- Node ≥ 22 (CI uses 24) and npm workspaces (`apps/web`, `packages/contracts`, `e2e`).
- Docker, for the integration tests (Testcontainers) and the full stack.

```bash
make install       # poetry install in every Python app + npm ci
```

## Build, tests and lint

```bash
make lint            # ruff format --check, ruff check, import-linter, ESLint
make typecheck       # mypy --strict in every Python app, tsc in apps/web and e2e
make contracts-check # OpenAPI document and generated TS types are current
make test-unit       # no containers: api unit + architecture, device-protocol, simulator, web (Vitest)
make test-it         # integration: real TimescaleDB, Redis, Keycloak, Mosquitto via Testcontainers (Docker, ~2.5 min)
npm run -w apps/web build
```

- `make check` runs `lint`, `typecheck` and `contracts-check` together.
- `make fmt` formats Python and applies the autofixable lint.
- After changing the API, run `make gen-client` and commit `packages/contracts` (the
  `contracts-check` gate fails otherwise).
- After adding or renaming a module in `apps/api/src/smarthome/modules/`, run
  `make import-contracts` and commit `apps/api/.importlinter`.
- `make e2e` and `make check-infra` need the running stack or Docker images; run them only when
  the issue touches the web flows or `infra/`.

## Module rules

- `apps/api/src/smarthome` is a modular monolith with three entrypoints (api, ingestor, worker).
  A module imports only another module's `public.py`, never its layers. import-linter
  enforces it.
- Each module owns one Postgres schema, with no foreign keys across schemas.
- The domain layer does not import frameworks (FastAPI, SQLAlchemy, Pydantic, Redis).

## Database

Alembic migrations live in `apps/api/migrations/versions/` (`NNNN_*.py`). Forward only: add a
new revision and never edit one that is already on `main`.

## Don't touch

Unless the issue explicitly asks for it:

- `.github/` (CI), the Dockerfiles, `infra/` (compose, Keycloak realm, MQTT ACLs and certs,
  observability config);
- migrations already on `main`;
- device credentials, MQTT ACLs, authentication and authorization (see
  `docs/security/threat-model.md`);
- Python and Node versions and dependencies (`pyproject.toml`, `poetry.lock`, `package.json`,
  `package-lock.json`): a new dependency is a person's decision.
