"""Package entry point so ``python -m ai_r.cli`` works.

Mirrors :mod:`ai_r.__main__`: delegates to :func:`ai_r.cli.main`.
"""

import sys

from ai_r.cli import main

if __name__ == "__main__":
    sys.exit(main())
