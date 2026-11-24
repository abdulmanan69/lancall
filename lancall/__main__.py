"""Entry point: ``python -m lancall``."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import signal
import sys

from .config import Config, parse_args
from .server import serve


def setup_logging(cfg: Config) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(cfg.log_level)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    root.addHandler(console)

    logfile = logging.handlers.RotatingFileHandler(
        cfg.data_dir / "lancall.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    logfile.setFormatter(fmt)
    root.addHandler(logfile)

    # Call detail records: one JSON object per finished call, kept apart from the server log.
    cdr = logging.getLogger("lancall.cdr")
    cdr.propagate = False
    cdr.setLevel(logging.INFO)
    records = logging.handlers.RotatingFileHandler(
        cfg.data_dir / "calls.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    records.setFormatter(logging.Formatter("%(message)s"))
    cdr.addHandler(records)

    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


async def _run(cfg: Config) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass  # Windows: Ctrl+C arrives as KeyboardInterrupt instead
    await serve(cfg, stop)


def main(argv: list[str] | None = None) -> int:
    cfg = parse_args(argv)
    setup_logging(cfg)
    log = logging.getLogger("lancall")
    try:
        asyncio.run(_run(cfg))
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        log.error("Could not start: %s", exc)
        log.error("Is another copy already running, or is port %d in use? Try --port.", cfg.port)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
