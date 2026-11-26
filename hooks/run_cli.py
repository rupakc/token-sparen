"""CLI bootstrap for plugin use when the ``token-sparen`` console script is not installed.

Works under ``python3 -I``: puts the plugin root on sys.path, then runs ``token_sparen.cli.main``.
"""

import os
import sys


def _run() -> int:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from token_sparen.cli import main

    return main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(_run())
