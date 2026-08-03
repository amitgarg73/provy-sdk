"""The version is stated once and read everywhere else (#3).

0.5.1 shipped with `provy.__version__ == "0.5.0"` because the literal in `provy/__init__.py` was not
bumped alongside `pyproject.toml`. The consequence was specific and bad: 0.5.1 exists because 0.5.0
duplicated spans on retry, so the value a user checks to confirm they have the fix named the version
that had the bug.
"""
import re
import pathlib
import sys

import provy

ROOT = pathlib.Path(__file__).resolve().parents[1]


def declared_version() -> str:
    """The single source of truth, read as text so this does not depend on the build backend."""
    m = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M)
    assert m, "pyproject.toml has no version"
    return m.group(1)


def test_reported_version_matches_pyproject():
    # In CI and any installed environment this is the real assertion. From a bare source checkout
    # the package is not installed, and the sentinel is the correct answer rather than a wrong number.
    if provy.__version__ == "0.0.0.dev":
        import importlib.util
        assert importlib.util.find_spec("provy") is not None
        return
    assert provy.__version__ == declared_version(), (
        f"provy.__version__ is {provy.__version__} but pyproject.toml says {declared_version()}. "
        "The version must be stated once, in pyproject.toml."
    )


def test_version_is_not_hardcoded_in_source():
    """Guard the mechanism, not just today's value: a literal would drift again the next release."""
    src = (ROOT / "provy" / "__init__.py").read_text()
    literal = re.search(r'^__version__\s*=\s*["\'](\d+\.\d+)', src, re.M)
    assert literal is None, (
        f"__version__ is hardcoded as {literal.group(1) if literal else ''} in provy/__init__.py. "
        "Derive it from importlib.metadata instead so it cannot go stale."
    )


def test_version_is_reported_at_all():
    assert provy.__version__
    assert re.match(r"^\d+\.\d+", provy.__version__)
