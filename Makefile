# Discord Community TV Bot — dev + prod tasks
# Run `make help` for the full list.
#
# All docker-compose actions go through this Makefile so ops stays uniform.

.PHONY: help \
        install install-provider install-all \
        test test-cov lint format \
        run-bot run-dashboard run-provider \
        up up-build up-bot up-dashboard up-provider down restart \
        logs logs-bot logs-dashboard logs-provider \
        ps status rebuild pull \
        shell-bot shell-dashboard shell-provider \
        db-shell db-shell-provider refresh-playlist telegram-login \
        health backup clean

PY ?= python3
PIP ?= $(PY) -m pip
COMPOSE ?= docker compose

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN{FS=":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ============================================================ dev / testing
install:  ## Install bot + dashboard requirements
	$(PIP) install -r requirements.txt

install-provider:  ## Install file-provider requirements
	$(PIP) install -r file_provider/requirements.txt

install-all: install install-provider  ## Install everything

test:  ## Run the full test suite
	$(PY) -m pytest

test-cov:  ## Run tests with coverage report
	$(PY) -m coverage run -m pytest && $(PY) -m coverage report

lint:  ## Ruff lint + format check
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

format:  ## Apply ruff format + autofix
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

# ============================================================= run locally
run-bot:  ## Run the Discord bot (local Python, not Docker)
	$(PY) -m bot.main

run-dashboard:  ## Run the FastAPI dashboard (dev, auto-reload)
	$(PY) -m uvicorn dashboard.main:app --host 0.0.0.0 --port $${DASHBOARD_PORT:-8000} --reload

run-provider:  ## Run the file-provider service (dev, auto-reload)
	$(PY) -m uvicorn file_provider.api.main:app --host 0.0.0.0 --port $${FILE_PROVIDER_PORT:-8001} --reload

# ============================================================ docker-compose
up:  ## Start all services in the background
	$(COMPOSE) up -d

up-build:  ## Rebuild images then start all services
	$(COMPOSE) up -d --build

up-bot:  ## Start just the bot (+ its deps)
	$(COMPOSE) up -d bot

up-dashboard:  ## Start just the dashboard
	$(COMPOSE) up -d dashboard

up-provider:  ## Start just the file-provider
	$(COMPOSE) up -d file-provider

down:  ## Stop and remove all containers (keeps volumes)
	$(COMPOSE) down

restart:  ## Restart all services
	$(COMPOSE) restart

rebuild:  ## Force-rebuild images from scratch (no cache)
	$(COMPOSE) build --no-cache
	$(COMPOSE) up -d

pull:  ## Pull latest base images
	$(COMPOSE) pull

# ============================================================ observability
logs:  ## Tail logs from all services
	$(COMPOSE) logs -f --tail=100

logs-bot:  ## Tail bot logs
	$(COMPOSE) logs -f --tail=200 bot

logs-dashboard:  ## Tail dashboard logs
	$(COMPOSE) logs -f --tail=200 dashboard

logs-provider:  ## Tail file-provider logs
	$(COMPOSE) logs -f --tail=200 file-provider

ps status:  ## Show container status
	$(COMPOSE) ps

health:  ## Show healthcheck status
	@$(COMPOSE) ps --format 'table {{.Service}}\t{{.Status}}'

# =============================================================== shells
shell-bot:  ## Interactive shell in the bot container
	$(COMPOSE) exec bot /bin/bash

shell-dashboard:  ## Interactive shell in the dashboard container
	$(COMPOSE) exec dashboard /bin/bash

shell-provider:  ## Interactive shell in the file-provider container
	$(COMPOSE) exec file-provider /bin/bash

db-shell:  ## Open sqlite3 on the shared bot DB
	$(COMPOSE) exec bot sqlite3 /data/tv.db

db-shell-provider:  ## Open sqlite3 on the provider DB
	$(COMPOSE) exec file-provider sqlite3 /data/provider.db

# ============================================================= operations
refresh-playlist:  ## Force the file-provider to rescan its backends
	$(COMPOSE) exec file-provider \
		python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8001/refresh', data=b'', timeout=60).read().decode())"

telegram-login:  ## Interactive Telethon first-run auth (phone + code)
	$(COMPOSE) run --rm file-provider python -c "\
from file_provider.config import load; \
from file_provider.providers.telegram import TelegramProvider; \
c = load(); \
p = TelegramProvider(c.telegram_api_id, c.telegram_api_hash, c.telegram_channel_id, c.telethon_session_path()); \
print(f'scanned {len(p.list_tracks())} audio messages')"

backup:  ## tar.gz the data/ and cache/ directories with a timestamp
	@mkdir -p backups
	tar -czf backups/tvbot-$$(date +%Y%m%d-%H%M%S).tar.gz data/ cache/
	@ls -lh backups/ | tail -3

# ================================================================== hygiene
clean:  ## Remove Python + test caches
	rm -rf .pytest_cache .ruff_cache .coverage coverage.xml htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
