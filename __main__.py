"""Entry point for Gemini Coder Web Edition."""

import io
import logging
import sys
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from . import __version__, __app_name__


def main() -> None:
    # Always log to file so pythonw launches still produce logs
    log_dir = Path.home() / ".autocoder"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "autocoder.log"

    handlers = [
        logging.FileHandler(log_file, encoding="utf-8"),
    ]
    # Also log to console if available (python, not pythonw)
    if sys.stdout is not None and hasattr(sys.stdout, "write"):
        try:
            handlers.append(logging.StreamHandler(sys.stdout))
        except Exception:
            pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )

    from .ui.app_web import GeminiCoderWebApp
    app = GeminiCoderWebApp()
    app.mainloop()


if __name__ == "__main__":
    main()
