"""Agent daemon entry point."""

import sys

if __name__ == "__main__":
    from .serve import main
    sys.exit(main())
