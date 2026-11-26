"""``python3 -m token_sparen``: same as the ``token-sparen`` console script."""

import sys

from token_sparen.cli import main

if __name__ == "__main__":
    sys.exit(main())
