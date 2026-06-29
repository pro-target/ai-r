"""Module entry point so ``python -m ai_r`` works.

Delegates to :func:`ai_r.cli.main` so the console-script logic and
``python -m ai_r`` share a single code path.
"""

import sys

from ai_r.cli import main

if __name__ == "__main__":
    sys.exit(main())
