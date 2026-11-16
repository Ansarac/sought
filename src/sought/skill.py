"""Locating and installing the bundled agent skill.

The skill is a directory — `SKILL.md` plus `references/` that an agent reads
only when it needs them — in the Agent Skills format, which Claude Code, pi
and others load from their own skills directories. One copy serves them all;
`install_skill` puts it where a given agent looks.

Two lookup branches, both hot: an editable install resolves the skill from the
source checkout, a wheel resolves it from package data. Neither is a fallback
for the other — test both.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from sought.errors import SoughtError

PKG = "sought"
NAME = "sought"


def skill_roots() -> dict[str, Path]:
    """Return each supported agent's user-level skills directory, by target name.

    Computed per call rather than at import, so `PI_CODING_AGENT_DIR` (pi's
    own override for `~/.pi/agent`) and a monkeypatched home both apply.
    """
    home = Path.home()
    pi_agent_dir = os.environ.get("PI_CODING_AGENT_DIR")
    return {
        "claude": home / ".claude" / "skills",
        "pi": (Path(pi_agent_dir).expanduser() if pi_agent_dir else home / ".pi" / "agent") / "skills",
        # The Agent Skills shared location; pi reads it too.
        "agents": home / ".agents" / "skills",
    }


TARGETS = ("claude", "pi", "agents")
DEFAULT_TARGET = "claude"


@dataclass(frozen=True)
class Installed:
    """What `install_skill` did, so the CLI can say it rather than imply it."""

    directory: Path
    written: list[Path]
    # Files already in the destination that the bundled skill does not ship
    # (an older version's leftovers, or the user's own). Never deleted.
    untouched: list[Path] = field(default_factory=list)


def locate_skill(*, pkg: str = PKG, name: str = NAME, anchor: Path | None = None) -> tuple[Path, str]:
    """Return the bundled SKILL.md and which branch found it.

    The skill's directory is the returned path's parent. The branch name is
    reported by `install-skill --print` so a wrong install layout is visible
    rather than merely broken.
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


def skill_files(skill_dir: Path) -> list[Path]:
    """Every file of the skill, relative to its directory, SKILL.md first.

    Bytecode caches and dotfiles are not part of a skill even when a checkout
    happens to hold them.
    """
    # Judge only the parts below skill_dir: the root itself may well sit
    # under a dot directory (~/.pi/agent/skills, ~/.claude/skills).
    relative = (p.relative_to(skill_dir) for p in skill_dir.rglob("*") if p.is_file())
    files = [p for p in relative if not any(part.startswith(".") or part == "__pycache__" for part in p.parts)]
    return sorted(files, key=lambda p: (p != Path("SKILL.md"), p.as_posix()))


def _refuse_conflicts(skill_dir: Path, dest: Path, shipped: list[Path]) -> None:
    """Raise SoughtError, before anything is written, for a destination the copy would trip over.

    Each of these used to escape as a raw traceback (SameFileError,
    FileExistsError), which `run()` rightly does not catch: a destination that
    is the bundled skill itself — a developer's `~/.claude/skills/sought`
    symlinked to the checkout — and a file where the install needs a
    directory, or a directory where it needs a file.
    """
    if dest.exists() and dest.resolve() == skill_dir.resolve():
        msg = f"{dest} is the bundled skill itself ({skill_dir.resolve()}) — nothing to install; it is already current"
        raise SoughtError(msg)
    for rel in shipped:
        target = dest / rel
        for parent in (dest, *(dest / p for p in reversed(rel.parents[:-1]))):
            if parent.exists() and not parent.is_dir():
                msg = f"{parent} is a file, but the skill needs a directory there; move it away and re-run"
                raise SoughtError(msg)
        if target.is_dir():
            msg = f"{target} is a directory, but the skill needs a file there; move it away and re-run"
            raise SoughtError(msg)


def install_skill(*, dest_root: Path, force: bool = False, name: str = NAME) -> Installed:
    """Copy the bundled skill directory to `dest_root/<name>/`.

    Without `force`, an existing SKILL.md there is an error. With it, every
    file the skill ships is overwritten; other files in the destination are
    left alone and reported, never deleted — the directory may hold something
    of the user's, and a skills root is no place for a recursive delete.
    """
    source, _branch = locate_skill(name=name)
    skill_dir = source.parent
    dest = dest_root / name

    if (dest / "SKILL.md").exists() and not force:
        msg = f"{dest / 'SKILL.md'} already exists; pass --force to overwrite"
        raise SoughtError(msg)

    shipped = skill_files(skill_dir)
    _refuse_conflicts(skill_dir, dest, shipped)
    written = []
    for rel in shipped:
        target = dest / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(skill_dir / rel, target)
        except OSError as exc:
            done = f"; {len(written)} of {len(shipped)} files were written — fix it and re-run with --force"
            msg = f"cannot write {target}: {exc.strerror or exc}{done if written else ''}"
            raise SoughtError(msg) from exc
        written.append(target)

    shipped_set = set(shipped)
    untouched = [dest / rel for rel in skill_files(dest) if rel not in shipped_set]
    return Installed(directory=dest, written=written, untouched=untouched)
