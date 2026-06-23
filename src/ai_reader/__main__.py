"""Module entry point so ``python -m ai_reader`` works.

Delegates to :func:`ai_reader.cli.main` so the console-script logic and
``python -m ai_reader`` share a single code path.
"""

import sys

from ai_reader.cli import main

if __name__ == "__main__":
    sys.exit(main())
