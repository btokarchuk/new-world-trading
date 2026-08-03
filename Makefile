# Operator commands. Everything trading-related runs INSIDE the container so
# the code never holds the host user's permissions.
#
#   make up       start both services  make status   risk/account status
#   make shell    interactive shell    make beat     is the engine alive?
#   make logs     all container logs   make cycle    one decision cycle
#   make down     stop the stack       make drill    rehearse the safety nets
#                                      make kill     PANIC: cancel all + HALT
#
# Host-side (deliberately outside the container): make test, make lint.

SHELL := /bin/bash
COMPOSE := docker compose -f deploy/docker-compose.yml
EXEC := $(COMPOSE) exec -T engine
EXEC_TTY := $(COMPOSE) exec engine
# Heartbeat reads run in the WATCHDOG container: `make beat` earns its keep
# exactly when the engine is dead or crash-looping, and exec into a dead
# container answers nothing.
EXEC_WATCHDOG := $(COMPOSE) exec -T watchdog
# The scheduler beats into the risk db; the watchdog reads the same file.
BEAT_DB ?= data/risk.db
# The drill must assert the grace the DEPLOYED watchdog actually uses, and the
# risk package deliberately cannot read the watchdog's config — so read it here.
WATCHDOG_GRACE_S ?= $(shell awk '/^heartbeat_grace_s:/ {print $$2}' config/watchdog.yaml)

# Corporate CA plumbing: point NWT_CA_CERT at the host cert and it is mounted
# read-only at the same path inside. No-op on a clean network.
ifdef SSL_CERT_FILE
export NWT_CA_DIR := $(dir $(SSL_CERT_FILE))
export NWT_CA_CERT := /certs/$(notdir $(SSL_CERT_FILE))
endif

.PHONY: help build up down restart shell logs watchdog-logs ps beat status cycle \
        poll resume kill flatten drill ingest-stocks ingest-crypto backtest test lint

# Heartbeats are a PROMISE (next_due), so "alive" is decidable without knowing
# the trading calendar: a beat is late or it is not.
define BEAT_PY
import sys
from datetime import UTC, datetime
from pathlib import Path
from nwt_risk.supervision import SupervisionStore

now = datetime.now(UTC)
for candidate in sys.argv[1:]:
    if not Path(candidate).exists():
        continue
    store = SupervisionStore(candidate)
    beat = store.last_beat()
    if beat is None:
        continue
    late = beat.overdue_by(now).total_seconds()
    print(f'{candidate}: seq {beat.seq} phase {beat.phase} at {beat.ts.isoformat()}')
    print(f'  promised back by {beat.next_due.isoformat()}')
    print(f'  {"OVERDUE" if late > 0 else "on time"} by {abs(late):.0f}s -- {beat.detail}')
    pending = store.pending_commands()
    print(f'  pending control commands: {len(pending)}')
    for command in pending:
        print(f'    [{command.command_id}] {command.command} from {command.issuer}: {command.reason}')
    break
else:
    print('no heartbeat in: ' + ', '.join(sys.argv[1:]))
    raise SystemExit(1)
endef
export BEAT_PY

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

build: ## Build the container image
	$(COMPOSE) build

up: ## Start the stack: scheduler + watchdog
	$(COMPOSE) up -d
	@$(COMPOSE) ps

down: ## Stop the stack
	$(COMPOSE) down

restart: ## Rebuild and restart (use after code changes)
	$(COMPOSE) up -d --build
	@$(COMPOSE) ps

ps: ## Show container status
	@$(COMPOSE) ps

logs: ## Tail logs from every service
	$(COMPOSE) logs -f --tail=100

watchdog-logs: ## Tail the watchdog only (supervisor's point of view)
	$(COMPOSE) logs -f --tail=100 watchdog

shell: ## Interactive shell inside the box
	$(EXEC_TTY) bash

# -- trading workflows (all inside the container) ---------------------------

beat: ## Last heartbeat + pending control commands (is the engine alive?)
	@$(EXEC_WATCHDOG) python -c "$$BEAT_PY" $(BEAT_DB)

status: ## Risk state, latches, alerts, broker account
	$(EXEC) nwt-risk status

cycle: ## Attended cycle now (the scheduler runs these on its own)
	$(EXEC) nwt-risk cycle

poll: ## Attended fill collection + re-verify the books
	$(EXEC) nwt-risk poll

resume: ## Arm the state machine (interactive: typed phrase required)
	$(EXEC_TTY) nwt-risk resume --to ACTIVE $(ACK)

kill: ## PANIC: cancel all orders + HALT (no confirmation, by design)
	$(EXEC) nwt-risk kill

flatten: ## Liquidate everything (interactive: typed challenge required)
	$(EXEC_TTY) nwt-risk flatten

drill: ## Rehearse the safety nets; nonzero exit == a net has a hole
	$(EXEC) nwt-risk drill --scenario insanity --grace-s $(or $(WATCHDOG_GRACE_S),120)

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
