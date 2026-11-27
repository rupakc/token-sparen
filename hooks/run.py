"""Claude Code hook launcher: put the plugin root on sys.path, then run the fail-open entry.

Works under ``python3 -I`` (no reliance on cwd, PYTHONPATH or user site-packages). When the
``python3`` that started it is older than 3.11, it re-launches itself on a Python >= 3.11 found by
``_pyfind.py`` (once; the result is cached); when there is none it logs one line a day and prints
a no-op. If the package cannot even be imported, it prints the no-op ``{}`` and exits 0 so the
session is never broken. Keep this file parseable by Python 3.6.
"""

import os
import sys

_FLAG = "TOKEN_SPAREN_REEXEC"


def _load_pyfind():
    # a plain import from this directory (importlib.util costs tens of ms on an old Python)
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    try:
        import _pyfind
    finally:
        sys.path.remove(here)
    return _pyfind


def _noop(message=""):
    import json

    sys.stdout.write((json.dumps({"systemMessage": message}) if message else "{}") + "\n")
    sys.stdout.flush()


def _hop(argv, env, version):
    """None: carry on in this interpreter; an int: exit with it (relaunched, or no-op)."""
    child = env.pop(_FLAG, None) is not None  # never leaks into the hook's own subprocesses
    old = tuple(version[:2]) < (3, 11)
    force = env.get("TOKEN_SPAREN_LAUNCHER_FORCE_REEXEC") == "1" and bool(
        env.get("TOKEN_SPAREN_HOME")
    )
    if not old and not force:
        return None
    if child:  # never a second hop
        if old:
            _noop()
            return 0
        return None
    pyfind = None
    found = None
    try:
        pyfind = _load_pyfind()
        if old or pyfind.forced(env):  # the seed needs a TOKEN_SPAREN_HOME the state root accepts
            found = pyfind.find_python(env, tuple(version[:2]))
    except Exception:  # noqa: BLE001 - fail open
        found = None
    if found is not None:
        rc = pyfind.relaunch(found[0], ["-I", "-S", os.path.abspath(__file__)] + list(argv[1:]))
        if rc is not None:
            return rc
    if not old:
        return None
    message = ""
    if pyfind is not None and found is None:
        try:
            message = pyfind.report_missing(env, version)
        except Exception:  # noqa: BLE001
            message = ""
    _noop(message)
    return 0


def _run() -> int:
    hopped = _hop(sys.argv, os.environ, sys.version_info)
    if hopped is not None:
        return hopped
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
