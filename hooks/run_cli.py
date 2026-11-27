"""CLI bootstrap for plugin use when the ``token-sparen`` console script is not installed.

Works under ``python3 -I``: puts the plugin root on sys.path, then runs ``token_sparen.cli.main``.
When the ``python3`` that started it is older than 3.11 it re-launches itself on a Python >= 3.11
found the same way as the hooks (``_pyfind.py``). Keep this file parseable by Python 3.6.
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


def _hop(argv, env, version):
    """None: carry on in this interpreter; an int: exit with it."""
    child = env.pop(_FLAG, None) is not None
    old = tuple(version[:2]) < (3, 11)
    force = env.get("TOKEN_SPAREN_LAUNCHER_FORCE_REEXEC") == "1" and bool(
        env.get("TOKEN_SPAREN_HOME")
    )
    if not old and not force:
        return None
    found = None
    if not child:
        try:
            pyfind = _load_pyfind()
            if old or pyfind.forced(env):
                found = pyfind.find_python(env, tuple(version[:2]))
            if found is not None:
                rc = pyfind.relaunch(
                    found[0], ["-I", "-S", os.path.abspath(__file__)] + list(argv[1:])
                )
                if rc is not None:
                    return rc
        except Exception:  # noqa: BLE001
            found = None
    if not old:
        return None
    sys.stderr.write(
        f"token-sparen needs Python >= 3.11, but this is {version[0]}.{version[1]} and no "
        "newer Python was found. "
        "Install Python 3.11 or newer, or set TOKEN_SPAREN_PYTHON to its path.\n"
    )
    return 1


def _run() -> int:
    hopped = _hop(sys.argv, os.environ, sys.version_info)
    if hopped is not None:
        return hopped
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from token_sparen.cli import main

    return main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(_run())
