"""The skill's own prose, checked the way the code is.

`SKILL.md` and the cookbooks are PRODUCT SURFACE — an agent reads them at
runtime and follows them. A link that resolves to nothing is therefore a
broken feature, not a typo, and it is invisible to every other test here:
the suite imports `templates/`, and none of it opens a markdown file.

This exists because one shipped. `SKILL.md`'s startup step told the agent to
"offer the install cookbook" through a release and a rename, and no cookbook
had ever been written — so an agent landing in a repo with no factory had
nothing to read and improvised the install, which is exactly how a repo ends
up stamped but with no `.env` and no `ASF_SKILL`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = SKILL_ROOT / "SKILL.md"
COOKBOOKS = SKILL_ROOT / "cookbooks"

# [text](target) — skipping external links and pure in-page anchors.
LINK = re.compile(r"\[[^\]]*\]\((?!https?://|#)([^)]+)\)")


def docs() -> list[Path]:
    return sorted([SKILL_MD, *COOKBOOKS.glob("*.md"), *(SKILL_ROOT / "references").glob("*.md")])


@pytest.mark.parametrize("doc", docs(), ids=lambda p: p.name)
def test_every_link_in_the_skill_s_prose_resolves(doc: Path):
    """A link to a file that is not there is a dead end for the agent reading it."""
    broken = []
    for target in LINK.findall(doc.read_text()):
        path = (doc.parent / target.split("#", 1)[0]).resolve()
        if not path.exists():
            broken.append(target)
    assert not broken, f"{doc.name} links to {broken}, which do not exist"


def test_every_cookbook_is_reachable_from_skill_md():
    """An orphan cookbook is as bad as a dangling link: written, never routed,
    and therefore never read by the agent it was written for."""
    routed = set(LINK.findall(SKILL_MD.read_text()))
    for cookbook in sorted(COOKBOOKS.glob("*.md")):
        target = f"cookbooks/{cookbook.name}"
        assert any(link.split("#", 1)[0] == target for link in routed), \
            f"{target} exists but SKILL.md never links to it"


def test_the_install_cookbook_is_what_startup_offers():
    """Step 1 of Startup is the ONLY thing an agent reads in a repo with no
    factory. It must hand over a real path, not the words 'install cookbook'."""
    text = SKILL_MD.read_text()
    startup = text.split("## Startup", 1)[1].split("##", 1)[0]
    assert "cookbooks/install.md" in startup, \
        "Startup does not name cookbooks/install.md — the one file that path needs"
