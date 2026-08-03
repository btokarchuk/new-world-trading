"""Entry point: `nwt-watchdog`, or `python -m nwt_watchdog`.

Config comes from WATCHDOG_CONFIG (default config/watchdog.yaml); credentials
come from secrets/watchdog-{env}.env, a different file from the engine's
secrets/{env}.env and holding a different Alpaca key pair. One shared,
revoked, or rate-limited credential would otherwise blind the supervisor at
the same instant it disables the thing being supervised.
"""

import os
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path

from .alerts import WatchdogAlerts
from .broker import LIVE_URL, PAPER_URL, AlpacaReadOnly
from .config import WatchdogConfig
from .monitor import Watchdog

_DEFAULT_CONFIG = "config/watchdog.yaml"


def _now() -> datetime:
    return datetime.now(UTC)


def load_env_file(env: str) -> Path:
    """Explicit path, no cwd-walking dotenv magic; real environment variables
    win over file values, matching the engine's convention."""
    path = Path("secrets") / f"watchdog-{env}.env"
    if not path.exists():
        return path
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())
    return path


def main() -> int:
    env = os.environ.get("WATCHDOG_ENV", "paper")
    if env not in ("paper", "live"):
        print(f"error: WATCHDOG_ENV must be 'paper' or 'live', got {env!r}", file=sys.stderr)
        return 2

    config_path = Path(os.environ.get("WATCHDOG_CONFIG", _DEFAULT_CONFIG))
    if not config_path.exists():
        print(
            f"error: watchdog config not found: {config_path}"
            " (set WATCHDOG_CONFIG or run from the repo root)",
            file=sys.stderr,
        )
        return 2
    config = WatchdogConfig.load(config_path)

    env_path = load_env_file(env)
    prefix = "ALPACA_PAPER" if env == "paper" else "ALPACA_LIVE"
    key_id = os.environ.get(f"{prefix}_KEY_ID", "")
    secret = os.environ.get(f"{prefix}_SECRET", "")
    if not key_id or not secret:
        print(
            f"error: missing watchdog broker credentials: set {prefix}_KEY_ID and"
            f" {prefix}_SECRET in {env_path} (see {env_path}.example)."
            " These must be a SEPARATE Alpaca key pair from the engine's.",
            file=sys.stderr,
        )
        return 2

    broker = AlpacaReadOnly(PAPER_URL if env == "paper" else LIVE_URL, key_id, secret)
    alerts = WatchdogAlerts(
        config.state_db,
        _now,
        webhook_url=config.webhook_url,
        healthcheck_url=config.healthcheck_url,
    )
    watchdog = Watchdog(config, broker, _now, alerts)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: watchdog.stop())

    alerts.raise_alert(
        "INFO",
        "watchdog_start",
        f"watchdog supervising {env} every {config.poll_interval_s}s",
        {
            "config": str(config_path),
            "dry_run": config.dry_run,
            "risk_db": str(config.risk_db),
            "limits": config.model_dump(mode="json"),
        },
    )
    watchdog.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
