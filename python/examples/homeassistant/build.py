#!/usr/bin/env python3
"""Bundle the integration into something a Home Assistant config directory takes.

In the source tree ``custom_components/kemper/libkp`` is a symlink to the
library beside it, so the integration runs against the working copy with no
install step and no copy to keep in sync. Home Assistant would not tolerate
that symlink in a config directory, so the bundle **dereferences** it: the
library is copied in as an ordinary package, and the shipped integration
depends on nothing but the standard library.

Usage::

    python build.py                       # dist/custom_components/kemper + the zip
    python build.py --install ~/homeassistant   # copy into a config directory
"""

from __future__ import annotations

import argparse
import json
import shutil
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "custom_components" / "kemper"
DIST = HERE / "dist"
DOMAIN = "kemper"

#: Never shipped: bytecode caches, editor droppings, and the integration's own
#: tests, which import Home Assistant's test harness.
IGNORE = shutil.ignore_patterns("__pycache__", "*.py[co]", ".DS_Store", "tests")


def version() -> str:
    """The version in the manifest — the one Home Assistant shows."""
    return json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))["version"]


def build() -> Path:
    """Write ``dist/custom_components/kemper`` and the zip beside it."""
    staged = DIST / "custom_components" / DOMAIN
    if DIST.exists():
        shutil.rmtree(DIST)
    staged.parent.mkdir(parents=True)
    # symlinks=False is the point: the libkp symlink lands as a real directory.
    shutil.copytree(SOURCE, staged, symlinks=False, ignore=IGNORE)

    archive = DIST / f"{DOMAIN}-{version()}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(staged.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(DIST))
    return archive


def install(config_dir: Path) -> Path:
    """Replace ``<config>/custom_components/kemper`` with the freshly built copy."""
    staged = DIST / "custom_components" / DOMAIN
    target = config_dir / "custom_components" / DOMAIN
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(staged, target)
    return target


def main() -> int:
    """Build, and optionally install into a config directory."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--install",
        metavar="HA_CONFIG_DIR",
        type=Path,
        help="also copy the bundle into this Home Assistant configuration directory",
    )
    args = parser.parse_args()

    archive = build()
    staged = DIST / "custom_components" / DOMAIN
    files = sum(1 for path in staged.rglob("*") if path.is_file())
    print(f"built {staged.relative_to(HERE)} ({files} files)")
    print(f"       {archive.relative_to(HERE)}")

    if args.install is not None:
        config_dir = args.install.expanduser().resolve()
        if not config_dir.is_dir():
            parser.error(f"{config_dir} is not a directory")
        target = install(config_dir)
        print(f"installed into {target}")
        print("restart Home Assistant, then add the integration from Devices & services")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
