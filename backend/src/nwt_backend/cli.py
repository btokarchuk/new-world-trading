"""nwt-backend: the observer's CLI — today's report, or the read-only API.

Deliberately credential-free: there is no broker client to construct and no
env file to load. If a future command here ever seems to need a secret, it
belongs in nwt-risk, not in the observer.
"""

from datetime import UTC, datetime
from pathlib import Path

import typer

from .report import RiskDbUnavailable, generate

app = typer.Typer(no_args_is_help=True, add_completion=False)

_DB_OPT = typer.Option(Path("data/risk.db"), "--db", help="Risk db (opened read-only).")
_CONFIG_OPT = typer.Option(
    Path("config/paper.yaml"), "--config", help="Paper config (sleeve capitals, data root)."
)


@app.command()
def report(
    db: Path = _DB_OPT,
    config: Path = _CONFIG_OPT,
    out_dir: Path = typer.Option(
        Path("data/reports"), "--out-dir", help="Where YYYY-MM-DD.md lands."
    ),
) -> None:
    """Write today's markdown report to --out-dir and stdout."""
    try:
        path, markdown = generate(db, config, out_dir, datetime.now(UTC))
    except RiskDbUnavailable as exc:
        # A missing db is an answerable condition, not a stack trace.
        typer.echo(f"cannot report: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(markdown)
    typer.echo(f"written: {path}", err=True)


def _loopback_only(host: str) -> str:
    """The API has no auth until Phase 6, so 'localhost only' is enforced,
    not suggested: any host that is not a loopback address is refused."""
    import ipaddress

    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host == "localhost"
    if not is_loopback:
        raise typer.BadParameter(
            f"'{host}' is not a loopback address; this API has no auth and"
            " binds only to localhost until Phase 6"
        )
    return host


@app.command()
def serve(
    db: Path = _DB_OPT,
    config: Path = _CONFIG_OPT,
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        callback=_loopback_only,
        help="Loopback addresses only (enforced) until Phase 6 auth.",
    ),
    port: int = typer.Option(8787, "--port"),
) -> None:
    """Serve the read-only API (GET-only; the kill switch is Phase 6, not here)."""
    import uvicorn

    from .api import create_app

    uvicorn.run(create_app(db_path=db, paper_config=config), host=host, port=port)
