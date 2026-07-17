# Discord Community TV Bot — dev tasks
# Run `make help` for the list.

.PHONY: help install install-provider test lint format run-bot run-dashboard run-provider clean

PY ?= python3
PIP ?= $(PY) -m pip

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN{FS=":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

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

run-bot:  ## Run the Discord bot
	$(PY) -m bot.main

run-dashboard:  ## Run the FastAPI dashboard (dev, auto-reload)
	$(PY) -m uvicorn dashboard.main:app --host 0.0.0.0 --port $${DASHBOARD_PORT:-8000} --reload

run-provider:  ## Run the file-provider service
	$(PY) -m uvicorn file_provider.api.main:app --host 0.0.0.0 --port $${FILE_PROVIDER_PORT:-8001} --reload

clean:  ## Remove caches, coverage artifacts
	rm -rf .pytest_cache .ruff_cache .coverage coverage.xml htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
