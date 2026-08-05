"""Daily report: the machine's account of its day, provable from its own rows.

The narrative law (anti-confabulation): every sentence in "What happened and
why" is generated from a row that was actually read, and when the evidence
stops the sentence says "cannot explain ... from evidence" and stops with it.
An observer that guesses is worse than no observer — it teaches the operator
to trust prose over the book, which is exactly backwards.

Everything here is derived from one read-only Snapshot (observe.py). Marks are
the last STORED daily closes; nothing in this module touches a network, a
broker, or a writable database handle.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .observe import (
    FillView,
    ObserverConfig,
    RiskDbUnavailable,
    Snapshot,
    gather,
    open_risk_db,
)

__all__ = ["RiskDbUnavailable", "build_snapshot", "render_markdown", "generate"]

# How many individual fills the narrative spells out before it summarizes.
_NARRATE_FILLS_MAX = 6


def build_snapshot(
    db_path: Path | str, config_path: Path | str, now: datetime | None = None
) -> Snapshot:
    now = now or datetime.now(UTC)
    cfg = ObserverConfig.load(config_path)
    conn = open_risk_db(db_path)
    try:
        return gather(conn, cfg, now, db_path)
    finally:
        conn.close()


def generate(
    db_path: Path | str,
    config_path: Path | str,
    out_dir: Path | str,
    now: datetime | None = None,
) -> tuple[Path, str]:
    """Build today's report, write data/reports/YYYY-MM-DD.md, return both."""
    now = now or datetime.now(UTC)
    snapshot = build_snapshot(db_path, config_path, now)
    markdown = render_markdown(snapshot)
    out = Path(out_dir) / f"{now.date().isoformat()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(markdown)
    return out, markdown


# -- formatting helpers ------------------------------------------------------


def _age(seconds: float) -> str:
    seconds = int(abs(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _money(value: Decimal | None) -> str:
    # :f keeps the row's stored precision and never falls into 1E+4 notation.
    return "—" if value is None else f"{value:f}"


def _signed(value: Decimal) -> str:
    text = _money(value)
    return text if value < 0 else f"+{text}"


def _fill_table(fills: tuple[FillView, ...]) -> list[str]:
    if not fills:
        return ["(none)"]
    lines = [
        "| sleeve | ts (UTC) | symbol | side | qty | price | fees |",
        "|---|---|---|---|---|---|---|",
    ]
    for f in fills:
        lines.append(
            f"| {f.sleeve_id} | {f.ts.strftime('%H:%M:%S')} | {f.symbol} | {f.side}"
            f" | {_money(f.qty)} | {_money(f.price)} | {_money(f.fees)} |"
        )
    return lines


# -- markdown ----------------------------------------------------------------


def render_markdown(snap: Snapshot) -> str:
    day = snap.generated_at.date().isoformat()
    yesterday = (snap.generated_at.date() - timedelta(days=1)).isoformat()
    lines: list[str] = [
        f"# Daily report — {day}",
        "",
        f"Generated {snap.generated_at.isoformat()} from `{snap.db_path}` (read-only)."
        " Marks are the last stored daily closes; nothing here touched a network"
        " or a broker.",
        "",
    ]

    lines.append("## Trading state")
    if snap.state is None:
        lines.append("- No system state row exists.")
    else:
        lines.append(
            f"- State: **{snap.state.state.value}** (mode {snap.state.mode}),"
            f" since {snap.state.updated_at.isoformat()}"
        )
        lines.append(f"- Arming intent: {'armed' if snap.state.armed else 'absent'}")
    lines.append(f"- Un-acked latches: {len(snap.latches)}")
    for latch in snap.latches:
        lines.append(
            f"  - [{latch.latch_id}] {latch.breaker} / {latch.reason} —"
            f" {latch.detail} (age {_age(latch.age_s)})"
        )
    # Every CLI invocation writes an INFO "command" alert and nothing acks
    # them, so itemizing INFO here would bury the EMERGENCY rows under audit
    # noise. The count stays honest; the full list is one /status call away.
    itemized = [a for a in snap.alerts if a.severity != "INFO"]
    lines.append(f"- Un-acked alerts: {len(snap.alerts)}")
    for alert in itemized:
        lines.append(
            f"  - [{alert.alert_id}] {alert.severity} {alert.category}:"
            f" {alert.message} (age {_age(alert.age_s)})"
        )
    if len(itemized) < len(snap.alerts):
        lines.append(
            f"  - … plus {len(snap.alerts) - len(itemized)} un-acked INFO alert(s),"
            " omitted here (full list via GET /status)"
        )
    lines.append("")

    lines.append("## Heartbeat")
    if snap.heartbeat is None:
        lines.append("- No heartbeat recorded.")
    else:
        hb = snap.heartbeat
        lines.append(
            f"- Last beat: seq {hb.seq}, phase {hb.phase}, at {hb.ts.isoformat()} — {hb.detail}"
        )
        lines.append(f"- Promised back by: {hb.next_due.isoformat()}")
        if hb.overdue_s > 0:
            lines.append(f"- Promise: **OVERDUE by {_age(hb.overdue_s)}**")
        else:
            lines.append(f"- Promise: on time ({_age(hb.overdue_s)} of slack)")
    lines.append(f"- Pending watchdog commands: {snap.pending_commands}")
    lines.append("")

    act = snap.activity
    lines.append(f"## Scheduler activity (since {act.since.isoformat()})")
    lines.append(
        f"- Reconcile passes: {act.reconcile_events} (cycles and polls are not"
        " distinguished in the audit trail)"
    )
    lines.append(
        f"- Orders submitted: {act.orders_submitted}"
        f" (rejected at broker: {act.orders_rejected})"
    )
    if act.rejections_by_reason:
        reasons = ", ".join(
            f"{reason} ×{count}" for reason, count in sorted(act.rejections_by_reason.items())
        )
        lines.append(f"- Governor rejections by reason: {reasons}")
    else:
        lines.append("- Governor rejections by reason: none")
    lines.append(f"- Fills applied: {act.fills_applied}")
    if act.audit_counts:
        kinds = ", ".join(f"{k} {v}" for k, v in sorted(act.audit_counts.items()))
        lines.append(f"- Audit rows by kind: {kinds}")
    else:
        lines.append("- Audit rows by kind: none")
    lines.append("")

    lines.append("## Fills")
    lines.append(f"### Today ({day})")
    lines.extend(_fill_table(snap.fills_today))
    lines.append(f"### Yesterday ({yesterday})")
    lines.extend(_fill_table(snap.fills_yesterday))
    lines.append("")

    lines.append("## Positions & open P&L")
    if not snap.sleeves:
        lines.append("(no sleeves configured)")
    for sleeve in snap.sleeves:
        lines.append(
            f"### {sleeve.sleeve_id} — capital {_money(sleeve.capital)},"
            f" cash {_money(sleeve.cash)}"
        )
        if not sleeve.positions:
            lines.append("(no positions)")
        else:
            lines.append("| symbol | qty | avg cost | mark | value | open P&L |")
            lines.append("|---|---|---|---|---|---|")
            for pos in sleeve.positions:
                pnl = _signed(pos.open_pnl) if pos.open_pnl is not None else "—"
                lines.append(
                    f"| {pos.symbol} | {_money(pos.qty)} | {_money(pos.avg_cost)}"
                    f" | {_money(pos.mark)} | {_money(pos.market_value)} | {pnl} |"
                )
            lines.append(f"- Sleeve equity at stored closes: {_money(sleeve.equity)}")
        if not sleeve.fold_complete:
            lines.append(
                "- **Ledger fold broke on this sleeve**: the numbers above stop"
                " at the last entry the fold could apply, and no mark, value,"
                " or equity is asserted (see 'What happened and why')."
            )
    lines.append("")

    lines.append("## Equity (day-open)")
    if not snap.equity:
        lines.append("(no equity rows recorded)")
    else:
        lines.append("| day | equity |")
        lines.append("|---|---|")
        for point in snap.equity:
            lines.append(f"| {point.day} | {_money(point.equity)} |")
        delta = snap.equity[-1].equity - snap.equity[0].equity
        lines.append(
            f"- Change over recorded days ({snap.equity[0].day} →"
            f" {snap.equity[-1].day}): {_signed(delta)}"
        )
    lines.append("")

    lines.append("## What happened and why")
    lines.append(_narrative(snap))
    lines.append("")
    return "\n".join(lines)


# -- the narrative -----------------------------------------------------------


def _narrative(snap: Snapshot) -> str:
    """One paragraph, every clause traceable to a row already rendered above."""
    if snap.empty:
        return (
            "The risk db contains no recorded state, ledger entries, audit rows,"
            " heartbeats, alerts, or equity marks. There is no evidence of any"
            " activity to narrate, and this report deliberately invents none."
        )
    sentences: list[str] = []

    if snap.state is None:
        sentences.append(
            "No system state row exists, so the trading posture cannot be"
            " explained from evidence."
        )
    else:
        sentences.append(
            f"The system is {snap.state.state.value} in {snap.state.mode} mode"
            f" (last transition {snap.state.updated_at.isoformat()})."
        )
    if snap.latches:
        oldest = max(snap.latches, key=lambda latch: latch.age_s)
        sentences.append(
            f"{len(snap.latches)} un-acked latch(es) gate a return to ACTIVE;"
            f" the oldest is '{oldest.breaker}' ({oldest.reason}),"
            f" {_age(oldest.age_s)} old."
        )
    if snap.alerts:
        # Same ranking alerts.py uses, restated here as data about the rows.
        rank = {"INFO": 0, "WARN": 1, "CRITICAL": 2, "EMERGENCY": 3}
        worst = max(snap.alerts, key=lambda a: rank.get(a.severity, 0))
        sentences.append(
            f"{len(snap.alerts)} alert(s) remain un-acked, the most severe at"
            f" {worst.severity} ('{worst.category}')."
        )

    if snap.heartbeat is None:
        sentences.append(
            "No heartbeat has ever been recorded, so engine liveness cannot be"
            " explained from evidence."
        )
    elif snap.heartbeat.overdue_s > 0:
        sentences.append(
            f"The engine's last heartbeat (seq {snap.heartbeat.seq}, phase"
            f" {snap.heartbeat.phase}) promised a return by"
            f" {snap.heartbeat.next_due.isoformat()} and that promise is broken"
            f" by {_age(snap.heartbeat.overdue_s)}."
        )
    else:
        sentences.append(
            f"The engine's last heartbeat (seq {snap.heartbeat.seq}, phase"
            f" {snap.heartbeat.phase}) is keeping its promise to return by"
            f" {snap.heartbeat.next_due.isoformat()}."
        )
    if snap.pending_commands:
        sentences.append(
            f"{snap.pending_commands} watchdog command(s) are waiting to be"
            " consumed by the engine."
        )

    act = snap.activity
    sentences.append(
        f"Since UTC midnight the audit trail records {act.reconcile_events}"
        " reconcile pass(es) (the audit schema does not distinguish decision"
        f" cycles from polls), {act.orders_submitted} order submission(s), and"
        f" {act.fills_applied} fill(s)."
    )
    if act.rejections_by_reason:
        reasons = ", ".join(
            f"{reason} ×{count}" for reason, count in sorted(act.rejections_by_reason.items())
        )
        sentences.append(
            f"The governor rejected intents for: {reasons} — each traces to a"
            " stored verdict row."
        )

    if snap.fills_today:
        if len(snap.fills_today) <= _NARRATE_FILLS_MAX:
            clauses = []
            for f in snap.fills_today:
                verb = {"buy": "bought", "sell": "sold"}.get(f.side)
                if verb is None:
                    # A fill row without a recorded side gets no invented one.
                    clauses.append(
                        f"{f.sleeve_id} recorded a fill with no recorded side:"
                        f" {_money(f.qty)} {f.symbol} @ {_money(f.price)}"
                    )
                else:
                    clauses.append(
                        f"{f.sleeve_id} {verb} {_money(f.qty)} {f.symbol}"
                        f" @ {_money(f.price)}"
                    )
            sentences.append(f"Fills settled today: {'; '.join(clauses)}.")
        else:
            sentences.append(
                f"{len(snap.fills_today)} fills settled into sleeve ledgers today."
            )
    else:
        sentences.append("No fills reached any sleeve ledger today.")

    marked = [
        pos for sleeve in snap.sleeves for pos in sleeve.positions if pos.open_pnl is not None
    ]
    unmarked = [
        pos for sleeve in snap.sleeves for pos in sleeve.positions if pos.open_pnl is None
    ]
    if marked or unmarked:
        clause = (
            f"The book holds {len(marked) + len(unmarked)} open position(s)"
        )
        if marked:
            total = sum((pos.open_pnl for pos in marked), Decimal("0"))
            clause += (
                f"; open P&L at the last stored closes is {_signed(total)}"
            )
            if unmarked:
                clause += f" across the {len(marked)} position(s) that have a stored close"
        clause += "."
        sentences.append(clause)

    if snap.equity:
        first, last = snap.equity[0], snap.equity[-1]
        sentences.append(
            f"Recorded day-open equity ran {_money(first.equity)} ({first.day})"
            f" to {_money(last.equity)} ({last.day}),"
            f" {_signed(last.equity - first.equity)}."
        )

    for gap in snap.gaps:
        # Verbatim and unsoftened: the gap IS the finding.
        sentences.append(gap + ".")

    return " ".join(sentences)
