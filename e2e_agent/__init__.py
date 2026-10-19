"""Notebook-derived, executable web testing workflow."""

__version__ = "0.1.0"

# Historical records keep their import names; new runs use e2e_agent.core.
from pathlib import Path as _Path
__path__.append(str(_Path(__file__).with_name("legacy")))
