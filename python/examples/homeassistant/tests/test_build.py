"""The bundler: what leaves the source tree, and in what shape.

The one thing that has to be true of a bundle is that the ``libkp`` symlink
became a real directory: Home Assistant copies a custom component into its
configuration and would find nothing at the other end of a relative symlink.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

import build


@pytest.fixture(autouse=True)
def dist_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build into the test's own directory, never the working tree's dist/."""
    dist = tmp_path / "dist"
    monkeypatch.setattr(build, "DIST", dist)
    return dist


def test_the_library_is_vendored_as_real_files(tmp_path: Path) -> None:
    """No symlink survives the copy, and the generated module comes with it."""
    build.build()
    staged = tmp_path / "dist" / "custom_components" / "kemper"

    assert (staged / "manifest.json").is_file()
    library = staged / "libkp"
    assert library.is_dir()
    assert not library.is_symlink()
    generated = library / "_generated.py"
    assert generated.is_file()
    assert not generated.is_symlink()
    assert "SPEC_VERSION" in generated.read_text(encoding="utf-8")


def test_nothing_that_should_not_ship_ships(tmp_path: Path) -> None:
    """Bytecode caches and the integration's tests stay behind."""
    build.build()
    staged = tmp_path / "dist" / "custom_components" / "kemper"

    assert not list(staged.rglob("__pycache__"))
    assert not list(staged.rglob("*.pyc"))
    assert not list(staged.rglob("tests"))


def test_the_zip_unpacks_into_a_config_directory(tmp_path: Path) -> None:
    """Its paths are relative to the configuration directory, ready to unzip."""
    archive = build.build()
    assert archive.name == f"kemper-{build.version()}.zip"

    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
    assert "custom_components/kemper/manifest.json" in names
    assert "custom_components/kemper/libkp/_generated.py" in names
    assert "custom_components/kemper/translations/en.json" in names


def test_install_replaces_an_existing_copy(tmp_path: Path) -> None:
    """Installing twice leaves one copy, not a merge of two."""
    build.build()
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    target = build.install(config_dir)
    stale = target / "stale.py"
    stale.write_text("# left over from an older version\n", encoding="utf-8")

    build.install(config_dir)
    assert (target / "manifest.json").is_file()
    assert not stale.exists()
