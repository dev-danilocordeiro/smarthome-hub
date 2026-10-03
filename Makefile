SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE := docker compose --env-file .env -f infra/docker-compose.yml -f infra/docker-compose.observability.yml
PY_APPS := apps/api apps/ingestor apps/worker apps/simulator packages/device-protocol
API     := cd apps/api && poetry run
RUFF    := apps/api/.venv/bin/ruff

# Image of a compose service, read from the compose files so versions live in one place.
image = $(shell $(COMPOSE) config --format json | python3 -c "import json,sys; print(json.load(sys.stdin)['services']['$(1)']['image'])")

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

.PHONY: certs
certs: ## Generate the development CA and broker certificate (infra/mqtt/certs, gitignored)
	scripts/gen-dev-certs.sh

.PHONY: up
up: .env certs ## Build and start the stack, waiting until every service is healthy
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

.PHONY: demo-traffic
demo-traffic: ## Generate mixed traffic for the dashboards (SECONDS=120 RPS=10)
	python3 scripts/demo_traffic.py --seconds $${SECONDS:-120} --rps $${RPS:-10}

# --- Simulator -----------------------------------------------------------------

SIM_DIR   := $(CURDIR)/.simulator
SIM       := cd apps/simulator && poetry run python -m smarthome_simulator
HOMES     ?= 3
DEVICES   ?= 20
SPEED     ?= 1
FAULTS    ?= 1
OWNER     ?= alice
DEVTOOLS  := set -a; source .env; set +a; cd apps/api && \
	SMARTHOME_MQTT_CA_FILE=$(CURDIR)/infra/mqtt/certs/ca.crt \
	SMARTHOME_MQTT_PORT=$${MQTT_TLS_PORT:-8883} \
	SMARTHOME_DATABASE_URL=postgresql+asyncpg://$${POSTGRES_USER}:$${POSTGRES_PASSWORD}@localhost:$${POSTGRES_PORT}/$${POSTGRES_DB} \
	SMARTHOME_REDIS_URL=redis://:$${REDIS_PASSWORD}@localhost:$${REDIS_PORT}/0 \
	poetry run python -m smarthome.devtools.fleet

$(SIM_DIR)/fleet.json:
	@mkdir -p $(SIM_DIR)
	$(SIM) plan --homes $(HOMES) --devices-per-home $(DEVICES) > $(SIM_DIR)/plan.json
	$(DEVTOOLS) seed $(SIM_DIR)/plan.json $(SIM_DIR)/pairing.json --owner $(OWNER)
	$(SIM) claim $(SIM_DIR)/pairing.json $(SIM_DIR)/fleet.json \
	  --api http://localhost:$${API_PORT:-8000} --ca-file $(CURDIR)/infra/mqtt/certs/ca.crt

.PHONY: simulate
simulate: .env certs $(SIM_DIR)/fleet.json ## Pair (first run only) and run 3 homes x 20 devices (HOMES, DEVICES, SPEED, FAULTS); device spans go to the collector
	set -a; source .env; set +a; \
	  export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:$${OTEL_GRPC_PORT:-4317}; \
	  $(SIM) run $(SIM_DIR)/fleet.json --speed $(SPEED) --fault-rate $(FAULTS)

.PHONY: simulate-reset
simulate-reset: ## Forget the simulated fleet; the next `make simulate` pairs a new one
	rm -rf $(SIM_DIR)

# --- Load test (docs/load-test.md) ----------------------------------------------

LOAD_DIR     := $(CURDIR)/.loadtest
LOAD_HOMES   ?= 50
LOAD_DEVICES ?= 20
LOAD_RATE    ?= 5
LOAD_SECONDS ?= 300
VUS          ?= 20
K6_IMAGE     := grafana/k6:2.3.0

$(LOAD_DIR)/fleet.json:
	@mkdir -p $(LOAD_DIR)
	$(SIM) plan --homes $(LOAD_HOMES) --devices-per-home $(LOAD_DEVICES) --seed 7 > $(LOAD_DIR)/plan.json
	$(DEVTOOLS) seed $(LOAD_DIR)/plan.json $(LOAD_DIR)/pairing.json --owner dave
	$(SIM) claim $(LOAD_DIR)/pairing.json $(LOAD_DIR)/fleet.json \
	  --api http://localhost:$${API_PORT:-8000} --ca-file $(CURDIR)/infra/mqtt/certs/ca.crt

.PHONY: loadtest-ingest
loadtest-ingest: .env certs $(LOAD_DIR)/fleet.json ## Telemetry load: LOAD_HOMES x LOAD_DEVICES devices at LOAD_RATE x their normal rate for LOAD_SECONDS, then a report
	@start=$$(date +%s); \
	  timeout --signal=INT --preserve-status $(LOAD_SECONDS) \
	    bash -c 'cd apps/simulator && poetry run python -m smarthome_simulator run $(LOAD_DIR)/fleet.json --rate $(LOAD_RATE) --fault-rate 0' \
	    > $(LOAD_DIR)/simulator.log 2>&1; \
	  end=$$(date +%s); sleep 20; \
	  python3 scripts/loadtest/report.py --start $$((start + 30)) --end $$end | tee $(LOAD_DIR)/ingest-report.md

.PHONY: loadtest-api
loadtest-api: .env ## HTTP read load with k6: VUS members for SECONDS against the BFF (needs `make web-dev` for the login)
	apps/api/.venv/bin/python scripts/loadtest/session.py --user dave > $(LOAD_DIR)/session.json
	docker run --rm --network host --user $$(id -u):$$(id -g) -v $(CURDIR)/scripts/loadtest:/scripts:ro -v $(LOAD_DIR):/loadtest \
	  -e VUS=$(VUS) -e SECONDS=$${SECONDS:-120} -e API=http://localhost:$${API_PORT:-8000} \
	  $(K6_IMAGE) run --summary-export=/loadtest/api-summary.json /scripts/api.js

.PHONY: loadtest-reset
loadtest-reset: ## Forget the load-test fleet (its homes stay in the database; `make nuke` clears all)
	rm -rf $(LOAD_DIR)

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

.PHONY: gen-client
gen-client: ## Export the OpenAPI document and regenerate the web app's TypeScript types
	apps/api/.venv/bin/python scripts/export_openapi.py
	npm run -w packages/contracts generate

.PHONY: contracts-check
contracts-check: ## Fail if packages/contracts is stale (run `make gen-client`)
	apps/api/.venv/bin/python scripts/export_openapi.py --check
	npm run -s -w packages/contracts generate
	git diff --exit-code -- packages/contracts/src

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
	npm run -w e2e typecheck

.PHONY: check-infra
check-infra: .env ## Validate compose, collector, Prometheus (and its alert rule tests), Alertmanager, Tempo, Loki, dashboards and CI workflows
	$(COMPOSE) config --quiet
	docker run --rm -v $(CURDIR)/infra/otel-collector/config.yaml:/c.yaml:ro \
	  $(call image,otel-collector) validate --config=/c.yaml
	docker run --rm --entrypoint promtool -v $(CURDIR)/infra/prometheus:/etc/prometheus:ro \
	  $(call image,prometheus) check config /etc/prometheus/prometheus.yml
	docker run --rm --entrypoint promtool -v $(CURDIR)/infra/prometheus:/etc/prometheus:ro \
	  $(call image,prometheus) test rules /etc/prometheus/rules/smarthome.test.yml
	docker run --rm --entrypoint amtool -v $(CURDIR)/infra/alertmanager:/a:ro \
	  $(call image,alertmanager) check-config /a/alertmanager.yml
	docker run --rm -v $(CURDIR)/infra/tempo/tempo.yaml:/t.yaml:ro \
	  $(call image,tempo) -config.file=/t.yaml -config.verify=true
	docker run --rm -v $(CURDIR)/infra/loki/loki.yaml:/l.yaml:ro \
	  $(call image,loki) -config.file=/l.yaml -verify-config
	python3 scripts/check_dashboards.py
	docker run --rm -v $(CURDIR):/repo -w /repo rhysd/actionlint:1.7.12 -color=false

.PHONY: check
check: lint typecheck contracts-check ## Every static gate CI runs (plus check-infra, which needs Docker)

# --- Tests -------------------------------------------------------------------

.PHONY: test-unit
test-unit: ## Fast tests: no containers
	$(API) pytest tests/unit tests/architecture
	cd packages/device-protocol && poetry run pytest
	cd apps/simulator && poetry run pytest
	npm run -w apps/web test

.PHONY: test-it
test-it: ## Integration tests: real Postgres/TimescaleDB, Redis, Keycloak, Mosquitto (needs Docker)
	$(API) pytest tests/integration

.PHONY: test
test: test-unit test-it ## All tests

.PHONY: e2e
e2e: ## Browser tests against the running stack (needs `make up`, `make simulate FAULTS=0`, `make web-dev`)
	npm run -w e2e test
