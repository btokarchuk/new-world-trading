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


if __name__ == "__main__":
    app()
