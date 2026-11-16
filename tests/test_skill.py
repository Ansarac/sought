"""Skill lookup and installation.

`locate_skill` has two branches — packaged (wheel) and source (checkout) — and
both are hot paths in normal use. Neither is a fallback for the other, so both
are tested here.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from sought import skill as skill_mod
from sought.errors import SoughtError

REPO = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO / "skills" / "sought"
_LINK = re.compile(r"\]\(([^)#\s]+)\)")


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


# --- installing the skill directory -------------------------------------------


def _bundled_dir():
    path, _branch = skill_mod.locate_skill()
    return path.parent


def test_install_copies_the_whole_skill_directory(tmp_path):
    installed = skill_mod.install_skill(dest_root=tmp_path)
    assert installed.directory == tmp_path / "sought"
    shipped = skill_mod.skill_files(_bundled_dir())
    assert Path("references") / "search.md" in shipped
    for rel in shipped:
        assert (installed.directory / rel).read_bytes() == (_bundled_dir() / rel).read_bytes()
    assert installed.written[0] == installed.directory / "SKILL.md"
    assert installed.untouched == []


def test_install_refuses_to_clobber_without_force(tmp_path):
    skill_mod.install_skill(dest_root=tmp_path)
    with pytest.raises(SoughtError, match="--force"):
        skill_mod.install_skill(dest_root=tmp_path)


def test_force_overwrites_every_shipped_file(tmp_path):
    installed = skill_mod.install_skill(dest_root=tmp_path)
    for path in installed.written:
        path.write_text("stale", encoding="utf-8")
    skill_mod.install_skill(dest_root=tmp_path, force=True)
    for path in installed.written:
        assert path.read_text(encoding="utf-8") != "stale"


def test_force_reports_but_never_deletes_files_it_does_not_ship(tmp_path):
    skill_mod.install_skill(dest_root=tmp_path)
    leftover = tmp_path / "sought" / "references" / "renamed-long-ago.md"
    leftover.write_text("from an older version, or the user's own", encoding="utf-8")

    installed = skill_mod.install_skill(dest_root=tmp_path, force=True)

    assert leftover.is_file()
    assert installed.untouched == [leftover]


def test_a_skills_root_under_a_dot_directory_is_not_mistaken_for_a_dotfile(tmp_path):
    # ~/.pi/agent/skills and ~/.claude/skills: only parts below the skill
    # directory may disqualify a file.
    root = tmp_path / ".pi" / "agent" / "skills"
    installed = skill_mod.install_skill(dest_root=root)
    assert skill_mod.skill_files(installed.directory) == skill_mod.skill_files(_bundled_dir())


def test_skill_files_skip_caches_and_dotfiles_and_put_skill_md_first(tmp_path):
    for rel in ("references/b.md", "SKILL.md", "references/a.md", ".DS_Store", "__pycache__/x.pyc", ".git/HEAD"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x", encoding="utf-8")
    assert skill_mod.skill_files(tmp_path) == [Path("SKILL.md"), Path("references/a.md"), Path("references/b.md")]


def test_install_from_the_packaged_layout_brings_the_references(monkeypatch, tmp_path):
    # The wheel ships skills/sought as sought/_skill; installing from it must
    # copy the directory, not just SKILL.md.
    packaged = tmp_path / "site-packages" / "sought" / "_skill"
    (packaged / "references").mkdir(parents=True)
    (packaged / "SKILL.md").write_text("---\nname: sought\n---\n", encoding="utf-8")
    (packaged / "references" / "search.md").write_text("# search", encoding="utf-8")
    monkeypatch.setattr(skill_mod, "_packaged", lambda _pkg: packaged / "SKILL.md")

    installed = skill_mod.install_skill(dest_root=tmp_path / "skills")

    assert (installed.directory / "references" / "search.md").read_text(encoding="utf-8") == "# search"


def test_the_wheel_ships_the_whole_skill_directory():
    """Only SKILL.md in the wheel would install a skill whose links all dangle."""
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    force_include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert force_include.get("skills/sought") == "sought/_skill"


# --- where each agent looks ---------------------------------------------------


def test_each_target_has_a_root_under_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    roots = skill_mod.skill_roots()
    assert set(roots) == set(skill_mod.TARGETS)
    assert roots["claude"] == tmp_path / ".claude" / "skills"
    assert roots["pi"] == tmp_path / ".pi" / "agent" / "skills"
    assert roots["agents"] == tmp_path / ".agents" / "skills"


def test_the_pi_target_follows_pi_coding_agent_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "elsewhere"))
    assert skill_mod.skill_roots()["pi"] == tmp_path / "elsewhere" / "skills"


# --- the skill itself ---------------------------------------------------------


def _frontmatter(text):
    assert text.startswith("---\n")
    block = text[4 : text.index("\n---\n", 4)]
    fields = {}
    for line in block.splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def test_the_frontmatter_meets_the_agent_skills_spec():
    # Agents skip a skill whose frontmatter they cannot use, without telling
    # the user why their search tool is missing.
    fields = _frontmatter((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))
    assert fields["name"] == SKILL_DIR.name
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", fields["name"])
    assert len(fields["name"]) <= 64
    assert 0 < len(fields["description"]) <= 1024
    assert len(fields.get("compatibility", "")) <= 500


def test_every_link_in_the_skill_resolves():
    for page in (SKILL_DIR / "SKILL.md", *sorted((SKILL_DIR / "references").glob("*.md"))):
        for target in _LINK.findall(page.read_text(encoding="utf-8")):
            if "://" in target:
                continue
            assert (page.parent / target).is_file(), f"{page.name} links to missing {target}"


def test_every_reference_is_reachable_from_skill_md():
    # An agent only ever opens what SKILL.md points it to.
    linked = set(_LINK.findall((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")))
    for ref in (SKILL_DIR / "references").glob("*.md"):
        assert f"references/{ref.name}" in linked, f"{ref.name} is never linked from SKILL.md"


def test_skill_md_stays_small_enough_to_load_whole():
    # SKILL.md is read in full every time the skill triggers; detail belongs
    # in references/, which is read on demand. Raise this deliberately, not
    # by drift.
    assert len((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").splitlines()) <= 170


def test_the_skill_states_the_rule_it_exists_to_carry():
    """No fetching results pages, and how to read the envelope."""
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "Never fetch a search engine results page" in body
    assert "low_relevance" in body
    assert "attempts" in body


def test_the_skill_works_without_a_harness_fetch_tool():
    # pi and other agents have no WebFetch; the skill must name sought's own
    # reader as the way to read a page, not merely as a fallback.
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "sought fetch" in body
    assert "--max-chars 0" in body
