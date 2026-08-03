# Operator commands. Everything trading-related runs INSIDE the container so
# the code never holds the host user's permissions.
#
#   make up       start the box        make status   risk/account status
#   make shell    interactive shell    make cycle    one decision cycle
#   make logs     container logs       make poll     collect fills
#   make down     stop the box         make kill     PANIC: cancel all + HALT
#
# Host-side (deliberately outside the container): make test, make lint.

SHELL := /bin/bash
COMPOSE := docker compose -f deploy/docker-compose.yml
EXEC := $(COMPOSE) exec -T engine
EXEC_TTY := $(COMPOSE) exec engine

# Corporate CA plumbing: point NWT_CA_CERT at the host cert and it is mounted
# read-only at the same path inside. No-op on a clean network.
ifdef SSL_CERT_FILE
export NWT_CA_DIR := $(dir $(SSL_CERT_FILE))
export NWT_CA_CERT := /certs/$(notdir $(SSL_CERT_FILE))
endif

.PHONY: help build up down restart shell logs ps status cycle poll resume kill \
        flatten ingest-stocks ingest-crypto backtest test lint

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

build: ## Build the container image
	$(COMPOSE) build

up: ## Start the containerized stack
	$(COMPOSE) up -d
	@$(COMPOSE) ps

down: ## Stop the stack
	$(COMPOSE) down

restart: ## Rebuild and restart (use after code changes)
	$(COMPOSE) up -d --build
	@$(COMPOSE) ps

ps: ## Show container status
	@$(COMPOSE) ps

logs: ## Tail container logs
	$(COMPOSE) logs -f --tail=100

shell: ## Interactive shell inside the box
	$(EXEC_TTY) bash

# -- trading workflows (all inside the container) ---------------------------

status: ## Risk state, latches, alerts, broker account
	$(EXEC) nwt-risk status

cycle: ## Run one decision cycle (reconcile -> decide -> submit)
	$(EXEC) nwt-risk cycle

poll: ## Collect fills and re-verify the books
	$(EXEC) nwt-risk poll

resume: ## Arm the state machine (interactive: typed phrase required)
	$(EXEC_TTY) nwt-risk resume --to ACTIVE $(ACK)

kill: ## PANIC: cancel all orders + HALT (no confirmation, by design)
	$(EXEC) nwt-risk kill

flatten: ## Liquidate everything (interactive: typed challenge required)
	$(EXEC_TTY) nwt-risk flatten

ingest-stocks: ## Top up daily equity bars (START=YYYY-MM-DD)
	$(EXEC) nwt ingest-stocks --start $(or $(START),2026-07-01)

ingest-crypto: ## Top up daily crypto bars (START=YYYY-MM-DD)
	$(EXEC) nwt ingest-crypto --start $(or $(START),2026-07-01)

backtest: ## Run a backtest (CONFIG=path/to/experiment.yaml)
	$(EXEC) nwt backtest $(or $(CONFIG),config/experiments/exp_0003_paper_mirror.yaml)

# -- host-side development --------------------------------------------------

test: ## Run the test suite on the host
	uv run pytest -q

lint: ## Lint + import contracts on the host
	uv run ruff check . && uv run lint-imports
