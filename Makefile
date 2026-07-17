# Discord Community TV Bot — all ops via docker compose.
#
# The host only needs Docker + `make`. Nothing runs on the host directly.
# Run `make help` for the full list.

.PHONY: help \
        build rebuild pull \
        up up-build down restart \
        up-bot up-dashboard up-provider \
        logs logs-bot logs-dashboard logs-provider \
        ps status health \
        test test-cov lint format \
        dev shell-bot shell-dashboard shell-provider \
        db-shell db-shell-provider \
        refresh-playlist telegram-login volume \
        backup clean env

# ---------------------------------------------------------------- config
COMPOSE ?= docker compose
DEV_PROFILE := --profile dev

# Match host uid/gid so mounted-file writes (from `format`, tests writing
# .pytest_cache, etc.) don't end up root-owned on Linux.
TEST_UID := $(shell id -u 2>/dev/null || echo 1000)
TEST_GID := $(shell id -g 2>/dev/null || echo 1000)
export TEST_UID
export TEST_GID

# Make sure `.env` exists — docker-compose refuses to load `env_file: .env`
# if the file is missing. First run copies `.env.example` as a placeholder.
env:  ## Create `.env` from `.env.example` if missing
	@if [ ! -f .env ]; then \
	  echo "creating .env from .env.example (fill in real values before starting the bot)"; \
	  cp .env.example .env; \
	fi

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN{FS=":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ================================================================= build
build: env  ## Build all images
	$(COMPOSE) $(DEV_PROFILE) build

rebuild: env  ## Force-rebuild all images from scratch (no cache)
	$(COMPOSE) $(DEV_PROFILE) build --no-cache

pull:  ## Pull latest base images
	$(COMPOSE) pull

# ============================================================== lifecycle
up: env  ## Start all long-running services in the background
	$(COMPOSE) up -d

up-build: env  ## Rebuild then start all services
	$(COMPOSE) up -d --build

up-bot: env  ## Start just the bot (+ its deps)
	$(COMPOSE) up -d bot

up-dashboard: env  ## Start just the dashboard
	$(COMPOSE) up -d dashboard

up-provider: env  ## Start just the file-provider
	$(COMPOSE) up -d file-provider

down:  ## Stop and remove containers (keeps volumes)
	$(COMPOSE) $(DEV_PROFILE) down --remove-orphans

restart:  ## Restart all services
	$(COMPOSE) restart

# =========================================================== observability
logs:  ## Tail logs from all services
	$(COMPOSE) logs -f --tail=100

logs-bot:  ## Tail bot logs
	$(COMPOSE) logs -f --tail=200 bot

logs-dashboard:  ## Tail dashboard logs
	$(COMPOSE) logs -f --tail=200 dashboard

logs-provider:  ## Tail file-provider logs
	$(COMPOSE) logs -f --tail=200 file-provider

ps status:  ## Show container status
	$(COMPOSE) $(DEV_PROFILE) ps

health:  ## Show healthcheck status
	@$(COMPOSE) ps --format 'table {{.Service}}\t{{.Status}}'

# ================================================================= tests
# Everything test-related runs inside the `test` service (see docker-compose.yml).
# The source tree is mounted so edits on the host are visible immediately.
test: env build  ## Run the full pytest suite inside a container
	$(COMPOSE) $(DEV_PROFILE) run --rm test

test-cov: env build  ## Run pytest with coverage report inside a container
	$(COMPOSE) $(DEV_PROFILE) run --rm test \
	  sh -c "python -m coverage run -m pytest -q && python -m coverage report"

lint: env build  ## Ruff check + format-check inside a container
	$(COMPOSE) $(DEV_PROFILE) run --rm test \
	  sh -c "python -m ruff check . && python -m ruff format --check ."

format: env build  ## Ruff format + autofix inside a container
	$(COMPOSE) $(DEV_PROFILE) run --rm test \
	  sh -c "python -m ruff format . && python -m ruff check --fix ."

# ================================================================= shells
dev: env build  ## Interactive bash inside a dev container
	$(COMPOSE) $(DEV_PROFILE) run --rm dev

shell-bot:  ## Interactive shell in the (running) bot container
	$(COMPOSE) exec bot /bin/bash

shell-dashboard:  ## Interactive shell in the (running) dashboard container
	$(COMPOSE) exec dashboard /bin/bash

shell-provider:  ## Interactive shell in the (running) file-provider container
	$(COMPOSE) exec file-provider /bin/bash

db-shell:  ## Open sqlite3 on the shared bot DB (needs `up` first)
	$(COMPOSE) exec bot sqlite3 /data/tv.db

db-shell-provider:  ## Open sqlite3 on the provider DB (needs `up` first)
	$(COMPOSE) exec file-provider sqlite3 /data/provider.db

# ============================================================== operations
refresh-playlist:  ## Tell the file-provider to rescan its backends
	$(COMPOSE) exec file-provider \
	  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8001/refresh', data=b'', timeout=60).read().decode())"

skip:  ## Skip the currently playing track
	$(COMPOSE) exec bot \
	  python -c "from db.database import Database; from dashboard.commands import enqueue; enqueue(Database('/data/tv.db'), command='skip', requested_by='CLI'); print('Queued skip command')"

pause:  ## Pause playback
	$(COMPOSE) exec bot \
	  python -c "from db.database import Database; from dashboard.commands import enqueue; enqueue(Database('/data/tv.db'), command='pause', requested_by='CLI'); print('Queued pause command')"

resume:  ## Resume/play playback
	$(COMPOSE) exec bot \
	  python -c "from db.database import Database; from dashboard.commands import enqueue; enqueue(Database('/data/tv.db'), command='resume', requested_by='CLI'); print('Queued resume command')"

volume:  ## Set global stream gain: make volume VOLUME=125 (50-250)
	@test -n "$(VOLUME)" || (echo "Usage: make volume VOLUME=125 (50-250)"; exit 2)
	@case "$(VOLUME)" in *[!0-9]*|"") echo "VOLUME must be an integer"; exit 2;; esac
	@[ "$(VOLUME)" -ge 50 ] && [ "$(VOLUME)" -le 250 ] || (echo "VOLUME must be 50-250"; exit 2)
	$(COMPOSE) exec bot \
	  python -c "from db.database import Database; from dashboard.commands import enqueue; enqueue(Database('/data/tv.db'), command='set_volume', requested_by='CLI', payload={'volume_percent': '$(VOLUME)'}); print('Queued volume command')"

telegram-login: env build  ## Interactive Telethon first-run auth
	$(COMPOSE) run --rm file-provider python -c "\
from file_provider.config import load; \
from file_provider.providers.telegram import TelegramProvider; \
c = load(); \
p = TelegramProvider(c.telegram_api_id, c.telegram_api_hash, c.telegram_channel_id, c.telethon_session_path()); \
print(f'scanned {len(p.list_tracks())} audio messages')"

backup:  ## tar.gz `data/` + `cache/` into `backups/` (uses busybox image)
	@mkdir -p backups
	docker run --rm \
	  -v $$(pwd)/data:/src/data:ro \
	  -v $$(pwd)/cache:/src/cache:ro \
	  -v $$(pwd)/backups:/backups \
	  busybox sh -c "tar -czf /backups/tvbot-$$(date +%Y%m%d-%H%M%S).tar.gz -C /src data cache"
	@ls -lh backups/ | tail -3

# ================================================================== hygiene
clean:  ## Stop everything, remove images, prune build cache
	$(COMPOSE) $(DEV_PROFILE) down --rmi local --remove-orphans
	docker builder prune -f
