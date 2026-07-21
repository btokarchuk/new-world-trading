import json
from datetime import UTC, datetime, timedelta

import pytest

from nwt_risk.alerts import Alert, AlertOutbox, jsonl_sender, stderr_sender

START = datetime(2026, 1, 5, 9, 30, tzinfo=UTC)


class FakeClock:
    def __init__(self, start: datetime = START) -> None:
        self._t = start

    def now(self) -> datetime:
        return self._t

    def advance(self, delta: timedelta) -> None:
        self._t += delta


class RecordingSender:
    def __init__(self, ok: bool = True, raise_error: bool = False) -> None:
        self.ok = ok
        self.raise_error = raise_error
        self.seen: list[Alert] = []

    def __call__(self, alert: Alert) -> bool:
        self.seen.append(alert)
        if self.raise_error:
            raise RuntimeError("sender down")
        return self.ok


def _outbox(tmp_path, *senders):
    clock = FakeClock()
    outbox = AlertOutbox(tmp_path / "alerts.db", clock.now)
    for sender in senders:
        outbox.register_sender(sender)
    return outbox, clock


def test_raise_deliver_ack_lifecycle(tmp_path):
    sender = RecordingSender()
    outbox, clock = _outbox(tmp_path, sender)

    alert = outbox.raise_alert("WARN", "reconcile", "cash drift", {"diff": "6.00"})
    assert alert.alert_id == 1
    assert alert.severity == "WARN"
    assert alert.payload == {"diff": "6.00"}
    assert alert.created_at == START
    assert alert.delivered_at == START  # all senders succeeded inline
    assert alert.acked_at is None
    assert [a.alert_id for a in sender.seen] == [1]

    pending = outbox.unacked()
    assert [a.alert_id for a in pending] == [1]
    assert pending[0].delivered_at == START  # delivery persisted

    clock.advance(timedelta(minutes=5))
    outbox.ack(1)
    assert outbox.unacked() == []
    assert outbox.deliver_pending() == 0

    with pytest.raises(ValueError):
        outbox.ack(99)


def test_failed_sender_leaves_undelivered_then_retry_delivers(tmp_path):
    failing = RecordingSender(ok=False)
    good = RecordingSender()
    outbox, clock = _outbox(tmp_path, failing, good)

    alert = outbox.raise_alert("CRITICAL", "breaker", "daily loss", {})
    assert alert.delivered_at is None
    assert outbox.unacked()[0].delivered_at is None
    assert len(failing.seen) == 1 and len(good.seen) == 1  # both were attempted

    # Still failing: retry delivers nothing, alert stays pending.
    assert outbox.deliver_pending() == 0
    assert outbox.unacked()[0].delivered_at is None

    failing.ok = True
    clock.advance(timedelta(seconds=30))
    assert outbox.deliver_pending() == 1
    assert outbox.unacked()[0].delivered_at == START + timedelta(seconds=30)
    assert outbox.deliver_pending() == 0  # nothing left pending


def test_sender_exception_counts_as_failed_delivery(tmp_path):
    broken = RecordingSender(raise_error=True)
    outbox, _ = _outbox(tmp_path, broken)
    alert = outbox.raise_alert("EMERGENCY", "kill_switch", "boom", {})
    assert alert.delivered_at is None
    broken.raise_error = False
    assert outbox.deliver_pending() == 1


def test_unacked_severity_filter(tmp_path):
    outbox, _ = _outbox(tmp_path, RecordingSender())
    for severity in ("INFO", "WARN", "CRITICAL", "EMERGENCY"):
        outbox.raise_alert(severity, "cat", f"{severity} msg", {})

    assert [a.severity for a in outbox.unacked()] == [
        "INFO", "WARN", "CRITICAL", "EMERGENCY",
    ]
    assert [a.severity for a in outbox.unacked("WARN")] == [
        "WARN", "CRITICAL", "EMERGENCY",
    ]
    assert [a.severity for a in outbox.unacked("EMERGENCY")] == ["EMERGENCY"]

    # acked alerts drop out regardless of severity
    emergency = [a for a in outbox.unacked() if a.severity == "EMERGENCY"][0]
    outbox.ack(emergency.alert_id)
    assert [a.severity for a in outbox.unacked("CRITICAL")] == ["CRITICAL"]


def test_jsonl_sender_writes_valid_lines(tmp_path):
    path = tmp_path / "data" / "alerts.jsonl"
    outbox, _ = _outbox(tmp_path, jsonl_sender(path))

    first = outbox.raise_alert("INFO", "command", "nwt-risk status", {"argv": ["status"]})
    second = outbox.raise_alert("WARN", "reconcile", "drift", {"diff": "9.99"})
    assert first.delivered_at is not None and second.delivered_at is not None

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["alert_id"] == 1
    assert parsed[0]["category"] == "command"
    assert parsed[0]["payload"] == {"argv": ["status"]}
    assert parsed[1]["severity"] == "WARN"
    assert parsed[1]["payload"] == {"diff": "9.99"}


def test_stderr_sender_prints_and_succeeds(capsys):
    alert = Alert(
        alert_id=7,
        severity="EMERGENCY",
        category="kill_switch",
        message="halt now",
        payload={},
        created_at=START,
        delivered_at=None,
        acked_at=None,
    )
    assert stderr_sender(alert) is True
    err = capsys.readouterr().err
    assert "[EMERGENCY] kill_switch: halt now" in err
