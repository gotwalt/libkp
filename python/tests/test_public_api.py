"""The package's public surface: everything ``__all__`` promises is importable."""

from __future__ import annotations

import libkp
from libkp import errors


def test_every_exported_name_exists():
    missing = [name for name in libkp.__all__ if not hasattr(libkp, name)]
    assert missing == []


def test_the_whole_error_family_is_exported():
    """A caller must be able to name every raisable error from the package root."""
    missing = [name for name in errors.__all__ if not hasattr(libkp, name)]
    assert missing == [], f"errors missing from libkp: {missing}"


def test_every_error_derives_from_the_base():
    for name in errors.__all__:
        assert issubclass(getattr(errors, name), libkp.LibKPError)
