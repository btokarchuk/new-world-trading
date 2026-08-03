"""The scripted insanity drill against FakeAlpaca and temp dbs.

The drill loads the REAL config/risk.yaml on purpose: a limit change that stops
the governor rejecting hostile flow should fail here, not in the rehearsal.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

import pytest
from fake_alpaca import FakeAlpaca

from nwt_contracts import TradingState
from nwt_engine.broker.alpaca import AlpacaHttpBroker
from nwt_risk.alerts import AlertOutbox
from nwt_risk.checks import LongOnlyCheck, default_checks
from nwt_risk.config import RiskConfig
from nwt_risk.drill import LiveDrillRefused, run_insanity_drill
from nwt_risk.state import TradingStateMachine

_NOW = datetime(2026, 8, 3, 14, 30, tzinfo=UTC)
_SCENARIOS = (
    "hostile-intent-flood",
    "heartbeat-starvation",
    "kill-switch",
    "resume-requires-acks",
)


class Stack(NamedTuple):
    machine: TradingStateMachine
    outbox: AlertOutbox
    config: RiskConfig
    broker: AlpacaHttpBroker
    fake: FakeAlpaca
    db: Path


@pytest.fixture()
def stack(tmp_path: Path) -> Stack:
    fake = FakeAlpaca(cash="10000", base_ts=_NOW)
    db = tmp_path / "risk.db"
    return Stack(
        machine=TradingStateMachine(db, "paper", lambda: _NOW),
        outbox=AlertOutbox(db, lambda: _NOW),
        config=RiskConfig.load(Path(__file__).parents[2] / "config" / "risk.yaml"),
        broker=AlpacaHttpBroker(
            "https://paper-api.alpaca.markets", "key", "secret", client=fake.client()
        ),
        fake=fake,
        db=db,
    )


def _run(stack: Stack, *, env: str = "paper", **kwargs):
    return run_insanity_drill(
        machine=stack.machine,
        outbox=stack.outbox,
        config=stack.config,
        broker=stack.broker,
        env=env,
        now_fn=lambda: _NOW,
        **kwargs,
    )


def _drill_alerts(stack: Stack, severity: str = "INFO"):
    return [
        alert
        for alert in AlertOutbox(stack.db, lambda: _NOW).unacked(severity)
        if alert.category == "drill"
    ]


def _arm(stack: Stack) -> None:
    stack.machine.on_startup()
    latch = next(latch for latch in stack.machine.current().latches if not latch.acked)
    result = stack.machine.request_transition(
        TradingState.ACTIVE, "test", "RESUME paper", [latch.latch_id]
    )
    assert result.ok and stack.machine.armed()


def test_healthy_stack_passes_every_scenario(stack: Stack):
    result = _run(stack)

    assert result.failures == ()
    assert result.passed
    assert all(step.startswith("PASS") for step in result.steps), result.steps
    for scenario in _SCENARIOS:
        assert any(f"[{scenario}]" in step for step in result.steps), scenario
    assert result.scenario == "insanity"
    assert result.started_at == result.ended_at == _NOW


def test_kill_switch_scenario_really_cancels_at_the_broker(stack: Stack):
    stack.fake.create_external("SPY", side="buy", qty="1", limit_price="100")

    result = _run(stack)

    assert result.passed
    assert [order["status"] for order in stack.fake.orders] == ["canceled"]
    assert any(
        request.method == "DELETE" and request.url.path == "/v2/orders"
        for request in stack.fake.requests
    )


def test_drill_restores_state_and_arming_intent(stack: Stack):
    _arm(stack)

    result = _run(stack)

    assert result.passed, result.failures
    assert stack.machine.state() is TradingState.ACTIVE
    assert stack.machine.armed()
    assert [latch for latch in stack.machine.current().latches if not latch.acked] == []


def test_drill_from_halted_leaves_halted_and_disarmed(stack: Stack):
    result = _run(stack)

    assert result.passed, result.failures
    assert stack.machine.state() is TradingState.HALTED
    assert not stack.machine.armed()
    # Both drill latches exist for the audit trail, both acked by the restore.
    breakers = {latch.breaker for latch in stack.machine.current().latches}
    assert breakers == {"drill_kill_switch", "drill_resume_ack"}
    assert all(latch.acked for latch in stack.machine.current().latches)


def test_result_is_written_to_the_outbox(stack: Stack):
    result = _run(stack)

    alerts = _drill_alerts(stack)
    assert len(alerts) == 1
    assert alerts[0].severity == "INFO"
    assert alerts[0].payload["passed"] is True
    assert alerts[0].payload["scenario"] == "insanity"
    assert alerts[0].payload["steps"] == list(result.steps)
    assert "PASSED" in alerts[0].message


def test_broken_governor_fails_loudly(stack: Stack):
    crippled = [c for c in default_checks() if not isinstance(c, LongOnlyCheck)]

    result = _run(stack, checks=crippled)

    assert not result.passed
    assert any("PHANTOM_POSITION" in failure for failure in result.failures)
    assert any("1 approved (want 0)" in failure for failure in result.failures)
    assert any(step.startswith("FAIL") for step in result.steps)

    alerts = _drill_alerts(stack, "CRITICAL")
    assert len(alerts) == 1
    assert alerts[0].payload["passed"] is False
    assert "FAILED" in alerts[0].message


def test_live_env_is_refused_before_anything_is_touched(stack: Stack):
    with pytest.raises(LiveDrillRefused, match="paper only"):
        _run(stack, env="live")

    assert stack.fake.requests == []
    assert stack.machine.state() is TradingState.HALTED
    assert stack.machine.current().latches == ()
    alerts = _drill_alerts(stack, "CRITICAL")
    assert len(alerts) == 1
    assert "REFUSED" in alerts[0].message


def test_drill_refuses_to_ack_an_operators_outstanding_latches(stack: Stack):
    stack.machine.on_startup()

    result = _run(stack)

    assert not result.passed
    assert [f for f in result.failures if "kill-switch" in f]
    assert [f for f in result.failures if "resume-requires-acks" in f]
    assert all("un-acked latches outstanding" in f for f in result.failures)
    # The startup latch is untouched and nothing was cancelled at the broker.
    latches = stack.machine.current().latches
    assert [(latch.breaker, latch.acked) for latch in latches] == [("startup", False)]
    assert stack.fake.requests == []


def test_heartbeat_starvation_asserts_the_configured_grace(stack: Stack, tmp_path: Path):
    supervision = tmp_path / "explicit-supervision.db"

    result = _run(stack, supervision_db=supervision, heartbeat_grace_s=45)

    assert result.passed, result.failures
    assert any("grace 45s" in step for step in result.steps)
    assert supervision.exists()


def test_starved_heartbeat_never_lands_beside_the_live_dbs(stack: Stack, tmp_path: Path):
    result = _run(stack)

    assert result.passed, result.failures
    # A fake overdue beat in the live supervision store would make the real
    # watchdog cancel real orders, so the default must be a throwaway.
    assert list(tmp_path.glob("*supervision*")) == []
    breach = next(step for step in result.steps if "CRITICAL breach in" in step)
    assert str(tmp_path) not in breach
