from datetime import UTC, datetime
from decimal import Decimal

from typer.testing import CliRunner

from nwt_contracts import TradingState
from nwt_engine.broker import AccountState, BrokerPosition
from nwt_risk import cli
from nwt_risk.alerts import AlertOutbox
from nwt_risk.state import TradingStateMachine

NOW = datetime(2026, 1, 5, 9, 30, tzinfo=UTC)

runner = CliRunner()


class FakeBroker:
    def __init__(self) -> None:
        self.cancel_all_calls = 0
        self.close_calls: list[bool] = []
        self.positions = [
            BrokerPosition(symbol="AAPL", qty=Decimal("5"), avg_cost=Decimal("100")),
            BrokerPosition(symbol="BTC/USD", qty=Decimal("0.1"), avg_cost=Decimal("50000")),
        ]

    def cancel_all(self) -> None:
        self.cancel_all_calls += 1

    def get_positions(self) -> list[BrokerPosition]:
        return self.positions

    def get_open_orders(self) -> list:
        return []

    def get_account(self) -> AccountState:
        return AccountState(ts=NOW, cash=Decimal("9123.45"), equity=Decimal("10456.78"))

    def close_all_positions(self, cancel_orders: bool) -> list[dict]:
        self.close_calls.append(cancel_orders)
        return [
            {"symbol": "AAPL", "status": 200, "body": {}},
            {"symbol": "BTCUSD", "status": 500, "body": {"message": "failed"}},
        ]


def _setup(tmp_path, monkeypatch, env: str = "paper"):
    fake = FakeBroker()
    monkeypatch.setattr(cli, "_make_broker", lambda _env: fake)
    db = tmp_path / "risk.db"
    config = tmp_path / "risk.yaml"
    config.write_text('equity_reference_usd: "10000"\n', encoding="utf-8")
    args = ["--db", str(db), "--config", str(config), "--env", env]
    return fake, db, config, args


def _machine(db, env: str = "paper") -> TradingStateMachine:
    return TradingStateMachine(db, env, lambda: NOW)


def test_status_renders(tmp_path, monkeypatch):
    fake, db, config, args = _setup(tmp_path, monkeypatch)
    machine = _machine(db)
    machine.on_startup()

    result = runner.invoke(cli.app, ["status", *args])
    assert result.exit_code == 0, result.output
    assert "HALTED" in result.output
    assert "config_hash" in result.output
    assert "startup" in result.output          # un-acked startup latch listed
    assert "9123.45" in result.output          # broker cash
    assert "10456.78" in result.output         # broker equity
    assert "open orders" in result.output


def test_kill_cancels_trips_halted_and_emits_emergency(tmp_path, monkeypatch):
    fake, db, config, args = _setup(tmp_path, monkeypatch)

    result = runner.invoke(cli.app, ["kill", *args])
    assert result.exit_code == 0, result.output
    assert fake.cancel_all_calls == 1

    machine = _machine(db)
    assert machine.state() is TradingState.HALTED
    latches = [
        latch for latch in machine.current().latches
        if latch.breaker == "kill_switch" and not latch.acked
    ]
    assert len(latches) == 1
    assert latches[0].detail == "operator kill"

    emergencies = AlertOutbox(db, lambda: NOW).unacked("EMERGENCY")
    assert len(emergencies) == 1
    assert emergencies[0].message == (
        "POSITIONS UNPROTECTED — brackets cancelled with all orders;"
        " re-protect or flatten"
    )


def test_flatten_aborts_on_wrong_phrase(tmp_path, monkeypatch):
    fake, db, config, args = _setup(tmp_path, monkeypatch)

    result = runner.invoke(cli.app, ["flatten", *args], input="FLATTEN paper 99\n")
    assert result.exit_code == 1
    assert fake.close_calls == []
    assert AlertOutbox(db, lambda: NOW).unacked("CRITICAL") == []


def test_flatten_executes_on_exact_phrase(tmp_path, monkeypatch):
    fake, db, config, args = _setup(tmp_path, monkeypatch)

    result = runner.invoke(cli.app, ["flatten", *args], input="FLATTEN paper 2\n")
    assert result.exit_code == 0, result.output
    assert fake.close_calls == [True]          # cancel_orders=True
    assert "open positions: 2" in result.output
    assert "AAPL: 200" in result.output        # per-item 207 results printed
    assert "BTCUSD: 500" in result.output

    criticals = AlertOutbox(db, lambda: NOW).unacked("CRITICAL")
    assert len(criticals) == 1
    assert criticals[0].category == "flatten"
    assert criticals[0].payload["results"][0]["symbol"] == "AAPL"


def test_resume_refuses_without_acks(tmp_path, monkeypatch):
    fake, db, config, args = _setup(tmp_path, monkeypatch)
    machine = _machine(db)
    machine.on_startup()

    result = runner.invoke(cli.app, ["resume", "--to", "ACTIVE", *args])
    assert result.exit_code == 1
    assert machine.state() is TradingState.HALTED


def test_resume_succeeds_with_acks_and_phrase(tmp_path, monkeypatch):
    fake, db, config, args = _setup(tmp_path, monkeypatch)
    machine = _machine(db)
    machine.on_startup()
    unacked = [latch for latch in machine.current().latches if not latch.acked]
    assert len(unacked) == 1

    result = runner.invoke(
        cli.app,
        ["resume", "--to", "ACTIVE", "--ack", str(unacked[0].latch_id), *args],
        input="RESUME paper\n",
    )
    assert result.exit_code == 0, result.output
    assert "state: ACTIVE" in result.output
    assert machine.state() is TradingState.ACTIVE
    assert all(latch.acked for latch in machine.current().latches)


def test_resume_aborts_on_wrong_phrase(tmp_path, monkeypatch):
    fake, db, config, args = _setup(tmp_path, monkeypatch)
    machine = _machine(db)
    machine.on_startup()
    unacked = [latch for latch in machine.current().latches if not latch.acked]

    result = runner.invoke(
        cli.app,
        ["resume", "--to", "ACTIVE", "--ack", str(unacked[0].latch_id), *args],
        input="RESUME live\n",
    )
    assert result.exit_code == 1
    assert machine.state() is TradingState.HALTED


def test_live_resume_requires_review_flag(tmp_path, monkeypatch):
    fake, db, config, args = _setup(tmp_path, monkeypatch, env="live")
    machine = _machine(db, env="live")
    machine.on_startup()
    unacked = [latch for latch in machine.current().latches if not latch.acked]

    result = runner.invoke(
        cli.app,
        ["resume", "--to", "ACTIVE", "--ack", str(unacked[0].latch_id), *args],
        input="RESUME live\n",
    )
    assert result.exit_code == 1
    assert machine.state() is TradingState.HALTED

    reviewed = runner.invoke(
        cli.app,
        [
            "resume", "--to", "ACTIVE", "--ack", str(unacked[0].latch_id),
            "--i-have-reviewed", *args,
        ],
        input="RESUME live\n",
    )
    assert reviewed.exit_code == 0, reviewed.output
    assert machine.state() is TradingState.ACTIVE


def test_drill_exits_nonzero(tmp_path, monkeypatch):
    fake, db, config, args = _setup(tmp_path, monkeypatch)

    result = runner.invoke(cli.app, ["drill", "--scenario", "insanity", *args])
    assert result.exit_code == 1
    assert "not yet implemented (Phase 4)" in result.output


def test_every_command_writes_audit_alert(tmp_path, monkeypatch):
    fake, db, config, args = _setup(tmp_path, monkeypatch)

    runner.invoke(cli.app, ["status", *args])
    runner.invoke(cli.app, ["drill", *args])
    commands = [
        alert for alert in AlertOutbox(db, lambda: NOW).unacked("INFO")
        if alert.category == "command"
    ]
    assert len(commands) == 2
    assert commands[0].message == "nwt-risk status"
    assert commands[1].message == "nwt-risk drill"
    # jsonl sender wired: one line per alert in the sibling alerts.jsonl
    assert (tmp_path / "alerts.jsonl").exists()


def test_redacted_argv_hides_key_material():
    argv = [
        "nwt-risk", "status", "--env", "paper",
        "--api-key=abc123", "--secret", "hunter2", "--db", "data/risk.db",
    ]
    assert cli._redacted_argv(argv) == [
        "nwt-risk", "status", "--env", "paper",
        "--api-key=***", "--secret", "***", "--db", "data/risk.db",
    ]


def test_env_file_loading(tmp_path, monkeypatch):
    from nwt_risk.cli import _load_env_file

    monkeypatch.chdir(tmp_path)
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "paper.env").write_text(
        "# comment\nALPACA_PAPER_KEY_ID=from_file\nALPACA_PAPER_SECRET=filesecret\n"
    )
    monkeypatch.delenv("ALPACA_PAPER_KEY_ID", raising=False)
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "from_real_env")
    _load_env_file("paper")
    import os

    assert os.environ["ALPACA_PAPER_KEY_ID"] == "from_file"
    assert os.environ["ALPACA_PAPER_SECRET"] == "from_real_env"  # real env wins
    monkeypatch.delenv("ALPACA_PAPER_KEY_ID")
