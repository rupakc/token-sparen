"""Claude Code hook launcher: put the plugin root on sys.path, then run the fail-open entry.

Works under ``python3 -I`` (no reliance on cwd, PYTHONPATH or user site-packages). If the package
cannot even be imported, it prints the no-op ``{}`` and exits 0 so the session is never broken.
"""

import os
import sys


def _run() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, root)
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    try:
        from token_sparen.adapters.claude.entry import main
    except Exception:  # noqa: BLE001 - broken install must not break the host session
        sys.stdout.write("{}\n")
        sys.stdout.flush()
        return 0
    event_name = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        return main(event_name, sys.stdin, sys.stdout, os.environ)
    except Exception:  # noqa: BLE001 - main never raises; never write twice
        return 0


if __name__ == "__main__":
    sys.exit(_run())
