#!/usr/bin/env python3
"""Thin executable entrypoint for the fail-closed restart coordinator."""

from hermes_cli.restart_coordinator import main


if __name__ == "__main__":
    raise SystemExit(main())
