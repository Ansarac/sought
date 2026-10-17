"""Locating and installing the bundled Claude skill.

Two lookup branches, both hot: an editable install resolves the skill from the
source checkout, a wheel resolves it from package data. Neither is a fallback
for the other — test both.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from sought.errors import SoughtError

PKG = "sought"
NAME = "sought"
DEFAULT_SKILL_ROOT = Path.home() / ".claude" / "skills"


def locate_skill(*, pkg: str = PKG, name: str = NAME, anchor: Path | None = None) -> tuple[Path, str]:
    """Return the bundled SKILL.md and which branch found it.

    The branch name is reported by `install-skill` and `doctor` so a wrong
    install layout is visible rather than merely broken.
    """
    anchor = anchor or Path(__file__)

    packaged = _packaged(pkg)
    if packaged is not None:
        return packaged, "packaged"

    source = _from_source(name, anchor)
    if source is not None:
        return source, "source"

    msg = f"cannot find SKILL.md for {name} (checked package data and the source checkout)"
    raise SoughtError(msg)


def _packaged(pkg: str) -> Path | None:
    """Find SKILL.md shipped inside the wheel via force-include."""
    from importlib.resources import files

    try:
        candidate = files(pkg) / "_skill" / "SKILL.md"
    except ModuleNotFoundError, TypeError:
        return None
    try:
        path = Path(str(candidate))
    except TypeError, ValueError:
        return None
    return path if path.is_file() else None


def _from_source(name: str, anchor: Path) -> Path | None:
    """Find SKILL.md in the checkout: src/sought/skill.py -> repo/skills/<name>/."""
    candidate = anchor.resolve().parents[2] / "skills" / name / "SKILL.md"
    return candidate if candidate.is_file() else None


def install_skill(*, dest_root: Path = DEFAULT_SKILL_ROOT, force: bool = False, name: str = NAME) -> Path:
    """Copy the bundled SKILL.md into the Claude skills directory."""
    source, _branch = locate_skill(name=name)
    dest = dest_root / name / "SKILL.md"

    if dest.exists() and not force:
        msg = f"{dest} already exists; pass --force to overwrite"
        raise SoughtError(msg)

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
    return dest
