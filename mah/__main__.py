"""Enables `python -m mah ...` -- e.g. running straight from a repo
checkout with nothing installed. See bin/mah for the installed-binary
entry point (a separate file outside this package, for reasons explained
there)."""

from .cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
