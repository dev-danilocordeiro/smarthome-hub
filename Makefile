SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE := docker compose --env-file .env -f infra/docker-compose.yml
PY_APPS := apps/api apps/ingestor apps/worker apps/simulator
API     := cd apps/api && poetry run
RUFF    := apps/api/.venv/bin/ruff

.PHONY: help
help: ## List targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- Setup -------------------------------------------------------------------

.env:
	cp .env.example .env
	@echo "created .env from .env.example"

.PHONY: install
install: ## Install Python (Poetry) and Node (npm) dependencies
	@for app in $(PY_APPS); do echo "==> $$app"; (cd $$app && poetry install --quiet) || exit 1; done
	npm ci

# --- Stack -------------------------------------------------------------------

.PHONY: up
up: .env ## Build and start the stack, waiting until every service is healthy
	$(COMPOSE) up -d --build --wait

.PHONY: down
down: ## Stop the stack (keeps volumes)
	$(COMPOSE) down

.PHONY: nuke
nuke: ## Stop the stack and delete its volumes
	$(COMPOSE) down -v

.PHONY: ps
ps: ## Show stack status
	$(COMPOSE) ps

.PHONY: logs
logs: ## Follow logs (S=<service> to filter)
	$(COMPOSE) logs -f $(S)

.PHONY: migrate
migrate: .env ## Apply database migrations through the stack
	$(COMPOSE) run --rm migrate

.PHONY: api-dev
api-dev: .env ## Run the API on the host with reload (needs postgres + redis up)
	set -a; source .env; set +a; cd apps/api && poetry run alembic upgrade head && \
	  poetry run uvicorn smarthome.main:create_app --factory --reload --port $${API_PORT:-8000}

.PHONY: web-dev
web-dev: ## Run the Vite dev server
	npm run -w apps/web dev

# --- Quality gates -----------------------------------------------------------

.PHONY: import-contracts
import-contracts: ## Regenerate apps/api/.importlinter from the module list
	python3 scripts/gen_import_contracts.py

.PHONY: lint-imports
lint-imports: ## Check module boundaries (import-linter)
	python3 scripts/gen_import_contracts.py --check
	$(API) lint-imports

.PHONY: fmt
fmt: ## Format Python and fix autofixable lint
	$(RUFF) format .
	$(RUFF) check --fix .

.PHONY: lint
lint: lint-imports ## Ruff, import-linter, ESLint
	$(RUFF) format --check .
	$(RUFF) check .
	npm run -w apps/web lint

.PHONY: typecheck
typecheck: ## mypy --strict on every Python app, tsc on the web app
	@for app in $(PY_APPS); do echo "==> mypy $$app"; (cd $$app && poetry run mypy) || exit 1; done
	npm run -w apps/web typecheck

.PHONY: check
check: lint typecheck ## Every static gate CI runs

# --- Tests -------------------------------------------------------------------

.PHONY: test-unit
test-unit: ## Fast tests: no containers
	$(API) pytest tests/unit tests/architecture
	npm run -w apps/web test

.PHONY: test-it
test-it: ## Integration tests against real Postgres/TimescaleDB and Redis (needs Docker)
	$(API) pytest tests/integration

.PHONY: test
test: test-unit test-it ## All tests
