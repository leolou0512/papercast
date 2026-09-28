"""`python -m papercast_cli`: the same as the `papercast` command (the worker starts itself and
its jobs this way, with the same Python)."""
import sys

from .cli import main

sys.exit(main())
