#!/usr/bin/env python3
"""Start the hub (what the unit, the watchdog and the end-to-end test run).

Not `python -m hub.app`: run that way, app.py is the module `__main__`, and the other modules'
`from .app import HTTPError` import a second copy of it as `hub.app`, so app.py's
`except HTTPError` never matches the errors they raise and every refusal (401, 404, the CLI
login's 428) goes out as a 500. Imported as `hub.app`, there is one class."""
import sys

from hub.app import main

if __name__ == "__main__":
    sys.exit(main())
