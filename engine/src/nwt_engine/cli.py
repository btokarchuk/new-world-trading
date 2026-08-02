from pathlib import Path

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.command()
def backtest(
    config_path: Path,
    check_determinism: bool = typer.Option(
        False, "--check-determinism", help="Run twice and compare journal hashes."
    ),
) -> None:
    """Run a backtest experiment from a YAML config."""
    from nwt_engine.experiments import BacktestRunner, ExperimentConfig

    config = ExperimentConfig.load(config_path)
    result = BacktestRunner(config).run()
    typer.echo(f"run_id:        {result.run_id}")
    typer.echo(f"journal_hash:  {result.journal_hash}")
    for sleeve in result.sleeves:
        typer.echo(f"\nsleeve {sleeve.sleeve_id}  (final equity {sleeve.final_equity:.2f})")
        for name, value in sleeve.metrics.items():
            typer.echo(f"  {name:>14}: {value:+.4f}")
    if check_determinism:
        second = BacktestRunner(config).run()
        if second.journal_hash == result.journal_hash:
            typer.echo("\ndeterminism: OK (identical journal hashes)")
        else:
            typer.echo("\ndeterminism: FAILED — journal hashes differ", err=True)
            raise typer.Exit(1)


@app.command("make-fixture")
def make_fixture(
    root: Path = typer.Option(Path("data/parquet"), help="Parquet store root."),
    symbol: str = "SYNTH",
) -> None:
    """Generate the deterministic synthetic fixture dataset."""
    from nwt_engine.data import ParquetStore
    from nwt_engine.data.fixtures import write_synthetic_fixture

    write_synthetic_fixture(ParquetStore(root), symbol=symbol)
    typer.echo(f"wrote synthetic fixture for {symbol} under {root}")


@app.command("ingest-crypto")
def ingest_crypto(
    symbols: str = typer.Option("BTC/USD,ETH/USD", help="Comma-separated crypto symbols."),
    start: str = typer.Option(..., help="Start date (YYYY-MM-DD)."),
    end: str | None = typer.Option(None, help="End date (YYYY-MM-DD); defaults to today."),
    root: Path = typer.Option(Path("data/parquet"), help="Parquet store root."),
) -> None:
    """Ingest daily crypto bars from Alpaca's keyless historical endpoint."""
    from datetime import date

    from nwt_engine.data import ParquetStore
    from nwt_engine.data.ingest import ingest_crypto as run_ingest

    counts = run_ingest(
        ParquetStore(root),
        [s.strip() for s in symbols.split(",") if s.strip()],
        date.fromisoformat(start),
        date.fromisoformat(end) if end else date.today(),
    )
    for symbol, count in counts.items():
        typer.echo(f"{symbol}: {count} bars")


@app.command("ingest-stocks")
def ingest_stocks(
    symbols: str = typer.Option(
        "SPY,QQQ,IWM,EFA,EEM,TLT,IEF,GLD", help="Comma-separated equity symbols."
    ),
    start: str = typer.Option(..., help="Start date (YYYY-MM-DD)."),
    end: str | None = typer.Option(None, help="End date (YYYY-MM-DD); defaults to today."),
    root: Path = typer.Option(Path("data/parquet"), help="Parquet store root."),
    env: str = typer.Option("paper", help="Key set to use: paper|live."),
    feed: str = typer.Option(
        "iex", help="Data feed: iex|sip (sip needs a paid data plan)."
    ),
) -> None:
    """Ingest daily equity bars from Alpaca (requires API keys).

    Merges with existing bars: re-running over an overlapping window is safe
    and is the intended way to top up before a decision cycle.
    """
    import os
    from datetime import date

    from nwt_engine.data import ParquetStore
    from nwt_engine.data.ingest.alpaca_stocks import fetch_stock_daily_bars
    from nwt_engine.domain import Timeframe

    env_file = Path("secrets") / f"{env}.env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

    prefix = "ALPACA_PAPER" if env == "paper" else "ALPACA_LIVE"
    key_id = os.environ.get(f"{prefix}_KEY_ID", "")
    secret = os.environ.get(f"{prefix}_SECRET", "")
    if not key_id or not secret:
        typer.echo(
            f"error: missing {prefix}_KEY_ID / {prefix}_SECRET "
            f"(or create secrets/{env}.env)",
            err=True,
        )
        raise typer.Exit(2)

    store = ParquetStore(root)
    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    fetched = fetch_stock_daily_bars(
        symbol_list,
        date.fromisoformat(start),
        date.fromisoformat(end) if end else date.today(),
        key_id,
        secret,
        feed=feed,
    )
    for symbol in symbol_list:
        new_bars = fetched.get(symbol, [])
        if not new_bars:
            typer.echo(f"{symbol}: no bars returned")
            continue
        try:
            existing = store.read_bars("alpaca", Timeframe.D1, symbol)
        except FileNotFoundError:
            existing = []
        merged = {bar.ts_open: bar for bar in existing}
        merged.update({bar.ts_open: bar for bar in new_bars})
        ordered = sorted(merged.values(), key=lambda b: b.ts_open)
        store.write_bars("alpaca", Timeframe.D1, symbol, ordered)
        typer.echo(
            f"{symbol}: +{len(new_bars)} fetched, {len(ordered)} total "
            f"through {ordered[-1].ts_open.date()}"
        )


if __name__ == "__main__":
    app()
