# Negahban — developer and operator shortcuts. Run `make help`.
SHELL      := /bin/bash
PYTHON     ?= python3
VENV       ?= .venv
PY         := $(VENV)/bin/python
PORT       ?= 8080
IMAGE      ?= negahban:local
APP        ?= negahban-sre
MSG        ?= negahban deploy
TEST_DIRS  := $(sort $(dir $(wildcard tests/test_*.py demo-shop/tests/test_*.py)))

.DEFAULT_GOAL := help
.PHONY: help venv install test lint run docker-build docker-run deploy deploy-wait releases rollback clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}'

$(PY):
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install -q --upgrade pip

venv: $(PY) ## Create the virtualenv

install: $(PY) ## Install runtime + test dependencies
	@if [ -f requirements.txt ]; then $(PY) -m pip install -q -r requirements.txt; else echo "requirements.txt not synced yet"; fi
	@if [ -f demo-shop/requirements.txt ]; then $(PY) -m pip install -q -r demo-shop/requirements.txt; fi
	$(PY) -m pip install -q pytest

test: ## Run the test suites (tests/ and demo-shop/tests/)
	@if [ -z "$(TEST_DIRS)" ]; then echo "no test directories yet (code not synced)"; exit 0; fi
	@for d in $(TEST_DIRS); do \
	  if [ "$$d" = demo-shop/tests/ ]; then (cd demo-shop && ../$(PY) -m pytest -q tests) || exit 1; \
	  else ORCH_ENABLED=0 $(PY) -m pytest -q $$d || exit 1; fi; done

lint: ## Byte-compile every Python file (syntax check)
	$(PYTHON) -m compileall -q -x '(\.venv|node_modules)' .

run: ## Run the app locally on :$(PORT) with .env loaded
	@test -f .env || { echo "copy .env.example to .env first"; exit 1; }
	set -a; . ./.env; set +a; $(VENV)/bin/uvicorn negahban.main:app --host 127.0.0.1 --port $(PORT) --reload

docker-build: ## Build the container image
	docker build -t $(IMAGE) .

docker-run: ## Run the image locally with .env and ./data
	mkdir -p data
	docker run --rm -p $(PORT):8080 --env-file .env -e DATA_DIR=/data -v "$$PWD/data:/data" $(IMAGE)

deploy: ## Deploy to Liara via deploy/deploy_negahban.py (MSG="...")
	$(PYTHON) deploy/deploy_negahban.py "$(MSG)"

deploy-wait: ## Wait for the newest Liara release of $(APP)
	$(PYTHON) tools/liara_cli.py wait $(APP)

releases: ## List recent Liara releases of $(APP)
	$(PYTHON) tools/liara_cli.py releases $(APP) 10

rollback: ## Roll back $(APP) to RELEASE=<id>
	@test -n "$(RELEASE)" || { echo "usage: make rollback RELEASE=<releaseID>"; exit 1; }
	$(PYTHON) tools/liara_cli.py rollback $(RELEASE)

clean: ## Remove caches
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache demo-shop/.pytest_cache
