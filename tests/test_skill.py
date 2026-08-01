"""Skill lookup and installation.

`locate_skill` has two branches — packaged (wheel) and source (checkout) — and
both are hot paths in normal use. Neither is a fallback for the other, so both
are tested here.
"""

from __future__ import annotations

import pytest

from sought import skill as skill_mod
from sought.errors import SoughtError


def test_the_skill_resolves_in_this_checkout():
    """Whichever branch fires, an installed sought must find its skill."""
    path, branch = skill_mod.locate_skill()
    assert path.is_file()
    assert branch in {"packaged", "source"}
    assert path.read_text(encoding="utf-8").lstrip().startswith("---")


def test_the_source_branch_finds_a_checkout_layout(tmp_path):
    repo = tmp_path / "repo"
    anchor = repo / "src" / "sought" / "skill.py"
    anchor.parent.mkdir(parents=True)
    anchor.touch()
    wanted = repo / "skills" / "sought" / "SKILL.md"
    wanted.parent.mkdir(parents=True)
    wanted.write_text("# skill", encoding="utf-8")

    assert skill_mod._from_source("sought", anchor) == wanted  # noqa: SLF001


def test_the_source_branch_declines_when_there_is_no_checkout(tmp_path):
    anchor = tmp_path / "a" / "b" / "skill.py"
    anchor.parent.mkdir(parents=True)
    anchor.touch()
    assert skill_mod._from_source("sought", anchor) is None  # noqa: SLF001


def test_the_packaged_branch_declines_for_a_package_without_one():
    assert skill_mod._packaged("json") is None  # noqa: SLF001


def test_neither_branch_matching_is_an_error_not_a_silent_miss(monkeypatch, tmp_path):
    monkeypatch.setattr(skill_mod, "_packaged", lambda _pkg: None)
    monkeypatch.setattr(skill_mod, "_from_source", lambda _name, _anchor: None)
    with pytest.raises(SoughtError, match=r"cannot find SKILL\.md"):
        skill_mod.locate_skill(anchor=tmp_path / "skill.py")


def test_install_writes_the_file(tmp_path):
    dest = skill_mod.install_skill(dest_root=tmp_path)
    assert dest == tmp_path / "sought" / "SKILL.md"
    assert dest.read_text(encoding="utf-8").lstrip().startswith("---")


def test_install_refuses_to_clobber_without_force(tmp_path):
    skill_mod.install_skill(dest_root=tmp_path)
    with pytest.raises(SoughtError, match="--force"):
        skill_mod.install_skill(dest_root=tmp_path)


def test_force_overwrites(tmp_path):
    dest = skill_mod.install_skill(dest_root=tmp_path)
    dest.write_text("stale", encoding="utf-8")
    skill_mod.install_skill(dest_root=tmp_path, force=True)
    assert dest.read_text(encoding="utf-8") != "stale"


def test_the_skill_states_the_rule_it_exists_to_carry():
    """The no-WebFetch-on-results-pages rule is the skill's reason for being."""
    path, _branch = skill_mod.locate_skill()
    body = path.read_text(encoding="utf-8")
    assert "WebFetch" in body
    assert "low_relevance" in body
    assert "attempts" in body
