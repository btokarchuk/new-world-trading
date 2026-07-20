# New World Trading

A personal automated-trading research platform: backtest / paper / live behind one broker
interface, wrapped in defense-in-depth failsafes, with an agentic research layer and an iOS
dashboard. Built as a two-person quant shop: Claude runs research and operations; Brent owns
capital decisions.

**The honest premise:** the edges retail automation can actually own are behavioral
discipline, trend/momentum premia, and cost/tax efficiency — measured against an always-on
buy-and-hold SPY control sleeve. Anything claiming alpha must survive walk-forward
validation, deflated-Sharpe scrutiny, and a paper track record before it touches money.

## Layout

| Path | What |
|---|---|
| `contracts/` | Seam types shared by engine and risk layer (`OrderIntent`, `TradingState`, …) |
| `engine/` | Core: domain models, event loop, sleeves, SimBroker/AlpacaBroker, data, strategies, experiments |
| `risk/` | RiskGovernor, pre-trade checks, circuit breakers, state machine, reconciliation *(Phase 3)* |
| `watchdog/` | Independent safety process — own keys, no `nwt_*` imports *(Phase 4)* |
| `backend/` | FastAPI + agent jobs (reports, chat, Edge Lab) *(Phase 5+)* |
| `ios/` | SwiftUI app *(Phase 5+)* |
| `config/` | Universes, experiments, risk limits |
| `docs/` | Research snapshot, decision log, runbooks |
| `data/` | (gitignored) Parquet market data, SQLite results |

## Quick start

```bash
uv sync --all-packages
uv run pytest
uv run nwt backtest config/experiments/exp_0001_buyhold_synth.yaml
```

Plan of record: see `docs/` and the approved build plan. Never commit `secrets/` or `data/`.
