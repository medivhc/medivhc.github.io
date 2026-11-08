#!/usr/bin/env python3
"""`rvd` CLI entry usable directly from the plugin directory."""
import sys
import _bootstrap  # noqa: F401
from rvd_core.cli import main

sys.exit(main())
