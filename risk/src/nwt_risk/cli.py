"""nwt-risk: the operator control CLI — status / kill / flatten / resume / drill.

Command friction is deliberately asymmetric: anything that moves TOWARD
safety (kill) runs with zero confirmation, anything that moves AWAY from it
(resume) or destroys positions (flatten) demands a typed challenge phrase.
Every invocation writes an INFO "command" alert to the outbox (argv with
key/secret material redacted) so the audit trail exists even for aborted
runs.
"""

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer

from nwt_contracts import TradingState

from .alerts import AlertOutbox, jsonl_sender, stderr_sender
from .config import RiskConfig
from .drill import DEFAULT_HEARTBEAT_GRACE_S, LIVE_REFUSED, run_insanity_drill
from .reasons import ReasonCode
from .state import TradingStateMachine

app = typer.Typer(no_args_is_help=True, add_completion=False)

_PAPER_URL = "https://paper-api.alpaca.markets"
_LIVE_URL = "https://api.alpaca.markets"

_KILL_MESSAGE = (
    "POSITIONS UNPROTECTED — brackets cancelled with all orders; re-protect or flatten"
)

# Bar top-ups re-request a window rather than a single day: ingest merges on
# write, and a long weekend or a missed run must not leave a hole in the tape.
_INGEST_LOOKBACK_DAYS = 10

_DB_OPT = typer.Option(Path("data/risk.db"), "--db", help="Risk state/alerts SQLite db.")
_CONFIG_OPT = typer.Option(Path("config/risk.yaml"), "--config", help="Risk config YAML.")
_ENV_OPT = typer.Option("paper", "--env", help="Broker environment: paper|live.")


class MissingCredentialsError(RuntimeError):
    pass


def _now() -> datetime:
    # Wall clock only at the operator boundary — the CLI is human-attended.
    return datetime.now(UTC)


def _check_env(env: str) -> str:
    if env not in ("paper", "live"):
        raise typer.BadParameter("--env must be 'paper' or 'live'")
    return env


def _load_env_file(env: str) -> None:
    """Load secrets/{env}.env if present. Explicit path, no cwd-walking dotenv
    magic; real environment variables always win over file values."""
    path = Path("secrets") / f"{env}.env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _make_broker(env: str):
    _load_env_file(env)
    prefix = "ALPACA_PAPER" if env == "paper" else "ALPACA_LIVE"
    key_id = os.environ.get(f"{prefix}_KEY_ID", "")
    secret = os.environ.get(f"{prefix}_SECRET", "")
    if not key_id or not secret:
        raise MissingCredentialsError(
            f"missing broker credentials: set {prefix}_KEY_ID and {prefix}_SECRET "
            f"(or create secrets/{env}.env — see secrets/paper.env.example)"
        )
    from nwt_engine.broker.alpaca import AlpacaHttpBroker

    return AlpacaHttpBroker(_PAPER_URL if env == "paper" else _LIVE_URL, key_id, secret)


def _universe_symbols(paper_cfg) -> tuple[list[str], list[str]]:
    """(equities, cryptos) from the deployment's universe files."""
    import yaml

    equities: list[str] = []
    cryptos: list[str] = []
    for file in paper_cfg.universe_files:
        for entry in yaml.safe_load(Path(file).read_text())["instruments"]:
            target = cryptos if entry["asset_class"] == "crypto" else equities
            target.append(entry["symbol"])
    return equities, cryptos


def _require_broker(env: str):
    try:
        return _make_broker(env)
    except MissingCredentialsError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2)


def _open(db: Path, env: str) -> tuple[TradingStateMachine, AlertOutbox]:
    db.parent.mkdir(parents=True, exist_ok=True)
    machine = TradingStateMachine(db, env, _now)
    outbox = AlertOutbox(db, _now)
    outbox.register_sender(stderr_sender)
    outbox.register_sender(jsonl_sender(db.parent / "alerts.jsonl"))
    return machine, outbox


_SENSITIVE_TOKENS = ("key", "secret", "token")


def _redacted_argv(argv: list[str]) -> list[str]:
    redacted: list[str] = []
    hide_next = False
    for arg in argv:
        name = arg.split("=", 1)[0].lower()
        if hide_next:
            redacted.append("***")
            hide_next = False
        elif arg.startswith("-") and any(t in name for t in _SENSITIVE_TOKENS):
            if "=" in arg:
                redacted.append(f"{arg.split('=', 1)[0]}=***")
            else:
                redacted.append(arg)
                hide_next = True
        else:
            redacted.append(arg)
    return redacted


def _audit_command(outbox: AlertOutbox, command: str) -> None:
    outbox.raise_alert(
        "INFO", "command", f"nwt-risk {command}", {"argv": _redacted_argv(sys.argv)}
    )


def _echo_latches(latches: list) -> None:
    typer.echo(f"un-acked latches ({len(latches)}):")
    for latch in latches:
        typer.echo(
            f"  [{latch.latch_id:>3}] {latch.breaker:<20} {latch.reason.value:<20}"
            f" {latch.detail}"
        )


@app.command()
def status(
    db: Path = _DB_OPT,
    config: Path = _CONFIG_OPT,
    env: str = _ENV_OPT,
) -> None:
    """Trading state, un-acked latches/alerts, config hash, broker account."""
    env = _check_env(env)
    machine, outbox = _open(db, env)
    _audit_command(outbox, "status")

    record = machine.current()
    typer.echo(f"{'env':>13}: {env}")
    typer.echo(f"{'state':>13}: {record.state.value}")
    typer.echo(f"{'updated_at':>13}: {record.updated_at.isoformat()}")
    typer.echo(f"{'config_hash':>13}: {RiskConfig.load(config).config_hash}")

    typer.echo("")
    _echo_latches([latch for latch in record.latches if not latch.acked])

    alerts = outbox.unacked("WARN")
    typer.echo(f"\nun-acked alerts ({len(alerts)}):")
    for alert in alerts:
        typer.echo(
            f"  [{alert.alert_id:>3}] {alert.severity:<9} {alert.category:<12} {alert.message}"
        )

    try:
        broker = _make_broker(env)
    except MissingCredentialsError as exc:
        typer.echo(f"\nbroker: unavailable ({exc})")
        return
    account = broker.get_account()
    typer.echo("\nbroker account:")
    typer.echo(f"{'cash':>13}: {account.cash}")
    typer.echo(f"{'equity':>13}: {account.equity}")
    typer.echo(f"{'open orders':>13}: {len(broker.get_open_orders())}")


@app.command()
def kill(
    db: Path = _DB_OPT,
    config: Path = _CONFIG_OPT,
    env: str = _ENV_OPT,
) -> None:
    """EMERGENCY stop: cancel every open order and HALT. No confirmation."""
    env = _check_env(env)
    machine, outbox = _open(db, env)
    _audit_command(outbox, "kill")

    cancel_error: str | None = None
    try:
        _make_broker(env).cancel_all()
    except MissingCredentialsError as exc:
        cancel_error = f"{exc} — orders NOT cancelled at broker"
    except Exception as exc:
        cancel_error = f"cancel_all failed: {exc}"

    # HALT locally no matter what the broker call did.
    machine.trip(
        "kill_switch", TradingState.HALTED, ReasonCode.KILL_SWITCH, "operator kill"
    )
    outbox.raise_alert(
        "EMERGENCY",
        "kill_switch",
        _KILL_MESSAGE,
        {"env": env, "cancel_error": cancel_error},
    )
    typer.echo(f"state: {machine.state().value}")
    if cancel_error is not None:
        typer.echo(f"error: {cancel_error}", err=True)
        raise typer.Exit(1)
    typer.echo("all orders cancelled; kill_switch latch armed")


@app.command()
def flatten(
    db: Path = _DB_OPT,
    config: Path = _CONFIG_OPT,
    env: str = _ENV_OPT,
) -> None:
    """Close ALL positions (challenge-response: type FLATTEN <env> <count>)."""
    env = _check_env(env)
    machine, outbox = _open(db, env)
    _audit_command(outbox, "flatten")
    broker = _require_broker(env)

    positions = broker.get_positions()
    count = len(positions)
    typer.echo(f"open positions: {count}")
    for position in positions:
        typer.echo(f"  {position.symbol:<12} qty {position.qty}")

    expected = f"FLATTEN {env} {count}"
    answer = typer.prompt(f'type "{expected}" to close ALL positions')
    if answer != expected:
        typer.echo("aborted: confirmation mismatch", err=True)
        raise typer.Exit(1)

    results = broker.close_all_positions(cancel_orders=True)
    for item in results:
        typer.echo(f"  {item['symbol']}: {item['status']}")
    outbox.raise_alert(
        "CRITICAL",
        "flatten",
        f"flatten executed on {env}: {count} positions, {len(results)} close results",
        {"env": env, "count": count, "results": results},
    )


@app.command()
def resume(
    to: str = typer.Option(..., "--to", help="Target state: ACTIVE|REDUCING."),
    ack: list[int] = typer.Option(
        [], "--ack", help="Latch id to acknowledge (repeat per latch)."
    ),
    i_have_reviewed: bool = typer.Option(
        False,
        "--i-have-reviewed",
        help="Live only: attest the latch causes were reviewed.",
    ),
    db: Path = _DB_OPT,
    config: Path = _CONFIG_OPT,
    env: str = _ENV_OPT,
) -> None:
    """Move toward trading: acks every latch, then a typed RESUME phrase."""
    env = _check_env(env)
    if to not in ("ACTIVE", "REDUCING"):
        raise typer.BadParameter("--to must be ACTIVE or REDUCING")
    machine, outbox = _open(db, env)
    _audit_command(outbox, "resume")

    unacked = [latch for latch in machine.current().latches if not latch.acked]
    _echo_latches(unacked)

    if env == "live" and not i_have_reviewed:
        typer.echo("refused: live resume requires --i-have-reviewed", err=True)
        raise typer.Exit(1)
    missing = [latch.latch_id for latch in unacked if latch.latch_id not in ack]
    if missing:
        typer.echo(
            f"refused: un-acked latches not acknowledged: {missing}"
            " (pass --ack <id> for each)",
            err=True,
        )
        raise typer.Exit(1)

    expected = f"RESUME {env}"
    answer = typer.prompt(f'type "{expected}" to confirm')
    if answer != expected:
        typer.echo("aborted: confirmation mismatch", err=True)
        raise typer.Exit(1)

    result = machine.request_transition(TradingState(to), "cli", answer, list(ack))
    if not result.ok:
        typer.echo(f"refused: {result.error}", err=True)
        raise typer.Exit(1)
    typer.echo(f"state: {result.state.value}")


@app.command()
def drill(
    scenario: str = typer.Option("insanity", "--scenario", help="Drill scenario."),
    grace_s: int = typer.Option(
        DEFAULT_HEARTBEAT_GRACE_S,
        "--grace-s",
        help="Heartbeat grace asserted against; mirror config/watchdog.yaml.",
    ),
    db: Path = _DB_OPT,
    config: Path = _CONFIG_OPT,
    env: str = _ENV_OPT,
) -> None:
    """Run the scripted insanity drill: hostile intents, starved heartbeat, kill switch."""
    env = _check_env(env)
    machine, outbox = _open(db, env)
    _audit_command(outbox, "drill")
    if scenario != "insanity":
        raise typer.BadParameter("only --scenario insanity exists in v1")
    # Refuse before a live broker is even constructed; run_insanity_drill repeats
    # the guard for programmatic callers.
    if env != "paper":
        outbox.raise_alert(
            "CRITICAL", "drill", f"insanity drill REFUSED on env {env!r}", {"env": env}
        )
        typer.echo(f"refused: {LIVE_REFUSED}", err=True)
        raise typer.Exit(2)

    result = run_insanity_drill(
        machine=machine,
        outbox=outbox,
        config=RiskConfig.load(config),
        broker=_require_broker(env),
        env=env,
        now_fn=_now,
        heartbeat_grace_s=grace_s,
    )
    for step in result.steps:
        typer.echo(f"  {step}")
    typer.echo(
        f"drill {'PASSED' if result.passed else 'FAILED'}: {len(result.steps)} steps,"
        f" {len(result.failures)} failures (logged to the outbox as category 'drill')"
    )
    if not result.passed:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()


@app.command()
def cycle(
    db: Path = _DB_OPT,
    config: Path = _CONFIG_OPT,
    env: str = _ENV_OPT,
    paper_config: Path = typer.Option(Path("config/paper.yaml"), "--paper-config"),
) -> None:
    """Run one attended paper decision cycle (reconcile -> decide -> submit)."""
    env = _check_env(env)
    if env != "paper":
        typer.echo("error: cycle is paper-only in Phase 3", err=True)
        raise typer.Exit(2)
    machine, outbox = _open(db, env)
    _audit_command(outbox, "cycle")
    broker = _require_broker(env)

    from .paper import PaperConfig, build_paper_cycle

    paper_cfg = PaperConfig.load(paper_config)
    risk_cfg = RiskConfig.load(config)

    def quotes_loader() -> dict:
        from nwt_engine.data.ingest.alpaca_stocks import fetch_latest_quotes

        prefix = "ALPACA_PAPER"
        equities, cryptos = [], []
        import yaml as _yaml

        for file in paper_cfg.universe_files:
            for entry in _yaml.safe_load(Path(file).read_text())["instruments"]:
                (cryptos if entry["asset_class"] == "crypto" else equities).append(
                    entry["symbol"]
                )
        return fetch_latest_quotes(
            equities,
            cryptos,
            os.environ.get(f"{prefix}_KEY_ID", ""),
            os.environ.get(f"{prefix}_SECRET", ""),
        )

    runner = build_paper_cycle(paper_cfg, risk_cfg, broker, db, quotes_loader, _now)
    report = runner.run_cycle()
    typer.echo(f"state: {report.state.value}  reconciled: {report.reconciled}")
    typer.echo(
        f"proposals: {report.proposals}  intents: {report.intents}  "
        f"approved: {report.approved}  submitted: {report.submitted}  "
        f"crosses: {report.crosses_executed}"
    )
    if report.rejected_reasons:
        typer.echo(f"rejections: {report.rejected_reasons}")
    for note in report.notes:
        typer.echo(f"note: {note}")
    if report.state is not TradingState.ACTIVE:
        typer.echo(
            "hint: trading state is not ACTIVE — review latches with `nwt-risk status`"
            " and arm with `nwt-risk resume` if appropriate"
        )


@app.command()
def poll(
    db: Path = _DB_OPT,
    config: Path = _CONFIG_OPT,
    env: str = _ENV_OPT,
    paper_config: Path = typer.Option(Path("config/paper.yaml"), "--paper-config"),
) -> None:
    """Collect fills for open paper orders and apply them to sleeve ledgers."""
    env = _check_env(env)
    if env != "paper":
        typer.echo("error: poll is paper-only in Phase 3", err=True)
        raise typer.Exit(2)
    machine, outbox = _open(db, env)
    _audit_command(outbox, "poll")
    broker = _require_broker(env)

    from .paper import PaperConfig, build_paper_cycle

    paper_cfg = PaperConfig.load(paper_config)
    risk_cfg = RiskConfig.load(config)
    runner = build_paper_cycle(paper_cfg, risk_cfg, broker, db, dict, _now)
    ledgers = runner.store.fold_ledgers(runner.sleeve_specs)
    applied = runner.poll_fills(ledgers)
    typer.echo(f"fills applied: {applied}")
    # A verified reconcile counts as one wherever it happens — this keeps the
    # startup latch from accumulating across attended poll-only sessions.
    reconciled = runner.reconcile_and_arm(ledgers)
    typer.echo(f"reconcile ok: {reconciled}")
    if not reconciled:
        typer.echo("reconcile mismatch — HALTED; see `nwt-risk status`", err=True)


@app.command()
def run(
    db: Path = _DB_OPT,
    config: Path = _CONFIG_OPT,
    env: str = _ENV_OPT,
    paper_config: Path = typer.Option(Path("config/paper.yaml"), "--paper-config"),
    schedule_config: Path = typer.Option(Path("config/schedule.yaml"), "--schedule-config"),
) -> None:
    """Run the market-aware scheduler loop (the container's main process)."""
    env = _check_env(env)
    if env != "paper":
        typer.echo("error: run is paper-only in Phase 4", err=True)
        raise typer.Exit(2)
    machine, outbox = _open(db, env)
    _audit_command(outbox, "run")
    broker = _require_broker(env)

    from .paper import PaperConfig
    from .scheduler import ScheduleConfig, Scheduler
    from .supervision import SupervisionStore

    paper_cfg = PaperConfig.load(paper_config)
    risk_cfg = RiskConfig.load(config)
    schedule_cfg = ScheduleConfig.load(schedule_config)
    equities, cryptos = _universe_symbols(paper_cfg)

    def quotes_loader() -> dict:
        from nwt_engine.data.ingest.alpaca_stocks import fetch_latest_quotes

        return fetch_latest_quotes(
            equities,
            cryptos,
            os.environ.get("ALPACA_PAPER_KEY_ID", ""),
            os.environ.get("ALPACA_PAPER_SECRET", ""),
        )

    def bars_ingest_fn() -> str:
        # Reuse the `nwt ingest-stocks` / `ingest-crypto` bodies rather than
        # re-implementing the merge-on-write; every option is passed explicitly
        # because their defaults are typer descriptors, not values.
        from nwt_engine.cli import ingest_crypto, ingest_stocks

        start = (_now().date() - timedelta(days=_INGEST_LOOKBACK_DAYS)).isoformat()
        if equities:
            ingest_stocks(
                symbols=",".join(equities),
                start=start,
                end=None,
                root=paper_cfg.data_root,
                env=env,
                feed="iex",
            )
        if cryptos:
            ingest_crypto(
                symbols=",".join(cryptos),
                start=start,
                end=None,
                root=paper_cfg.data_root,
            )
        return f"{len(equities)} equities + {len(cryptos)} crypto since {start}"

    scheduler = Scheduler(
        paper_cfg=paper_cfg,
        risk_cfg=risk_cfg,
        schedule_cfg=schedule_cfg,
        broker=broker,
        db_path=db,
        quotes_loader=quotes_loader,
        bars_ingest_fn=bars_ingest_fn,
        now_fn=_now,
        supervision=SupervisionStore(db),
        state_machine=machine,
        alerts=outbox,
        log_fn=typer.echo,
    )
    typer.echo(
        f"scheduler: env={env} tz={schedule_cfg.tz} ingest={schedule_cfg.ingest_at_et}"
        f" cycle={schedule_cfg.cycle_at_et} poll={schedule_cfg.poll_every_min}m"
        f" eod={schedule_cfg.eod_poll_at_et} grace={schedule_cfg.heartbeat_grace_s}s"
    )
    try:
        scheduler.run_forever()
    except KeyboardInterrupt:
        raise typer.Exit(0)
