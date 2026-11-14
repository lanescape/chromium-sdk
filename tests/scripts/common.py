"""Shared by the script tests: puts scripts/ on the import path."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO/'scripts'))
