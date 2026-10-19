"""Shared test setup.

scripts/ is not a package, so its directory goes on sys.path. VERTIGO_HOME
defaults to the example portfolio, which is what the registry-coverage tests
check; point it elsewhere to check your own theses.yaml against your own
evaluators.py.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE_HOME = os.path.join(ROOT, "examples", "vertigo-home")

sys.path.insert(0, os.path.join(ROOT, "scripts"))
os.environ.setdefault("VERTIGO_HOME", EXAMPLE_HOME)
