# One-command tasks. Run `make` or `make help` to list them.
# Server targets use SSH: override with `make deploy SERVER=user@host`.

SERVER ?= ubuntu@15.235.92.201
SERVER_DIR ?= /opt/solecra
COMPOSE_PROD = docker compose --profile prod

.DEFAULT_GOAL := help
.PHONY: help dev up down restart logs ps db tunnel test lint fmt check fake-text fake-button \
        deploy server-logs server-ps server-health backup

help: ## List all commands
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# --- Local development ---------------------------------------------------

dev: ## Build and start the app + Postgres locally (foreground)
	docker compose up --build

up: ## Start locally in the background
	docker compose up -d --build

down: ## Stop local containers (data is kept)
	docker compose down

restart: ## Restart local containers (Gate 1 restart test)
	docker compose restart

logs: ## Follow app logs (health checks hidden)
	docker compose logs -f --tail 100 api | grep --line-buffered -v "/health"

ps: ## Show local container status
	docker compose ps

db: ## Open a psql shell on the local database
	docker compose exec postgres psql -U solecra

tunnel: ## Public HTTPS URL for Meta (paste <url>/webhook in Meta)
	cloudflared tunnel --url http://localhost:8000

fake-text: ## Send a signed fake text, e.g. make fake-text FROM=15145551234 TEXT="test draft"
	uv run python -m scripts.fake_webhook text $(FROM) "$(TEXT)"

fake-button: ## Send a signed fake button tap, e.g. make fake-button FROM=15145551234 ID=approve:1:1
	uv run python -m scripts.fake_webhook button $(FROM) $(ID)

# --- Quality -----------------------------------------------------------------

test: ## Run the test suite (needs Postgres; see RUNBOOK §5)
	uv run pytest -q

lint: ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Auto-format and fix lint issues
	uv run ruff format .
	uv run ruff check --fix .

check: lint test ## Everything CI runs (lint + tests + docs check)
	uv run python -m scripts.check_docs

# --- Server (over SSH) --------------------------------------------------------

deploy: ## Pull main on the server and rebuild/restart
	ssh $(SERVER) 'cd $(SERVER_DIR) && git pull --ff-only && $(COMPOSE_PROD) up -d --build && sleep 5 && curl -fsS localhost:8000/health'

server-logs: ## Follow app logs on the server
	ssh -t $(SERVER) 'cd $(SERVER_DIR) && docker compose logs -f --tail 100 api'

server-ps: ## Container status on the server
	ssh $(SERVER) 'cd $(SERVER_DIR) && $(COMPOSE_PROD) ps'

server-health: ## Check the server health endpoint
	ssh $(SERVER) 'curl -fsS localhost:8000/health'

backup: ## Run a database backup on the server now
	ssh $(SERVER) 'cd $(SERVER_DIR) && ./scripts/backup.sh'
