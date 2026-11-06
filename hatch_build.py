"""Ship the lead documents inside the wheel, at agenttalk/docs/, so an installed lead can read them offline (#675).

A link to another shipped page or to a file of the package stays a relative link that works in the
installed folder. Any other link into this repository becomes a link to the same file at this version's
tag on GitHub, so an installed copy never sends its reader to another version's text."""

import posixpath
import re
import tempfile
from pathlib import Path

try:
    from hatchling.builders.hooks.plugin.interface import BuildHookInterface
except ImportError:  # the tests read LEAD_DOCS and rewrite_links without Hatchling installed
    BuildHookInterface = object

REPO_WEB = "https://github.com/zoolok17/agenttalk/blob/"
LEAD_DOCS = (
    "docs/LEAD-SETUP-BRIEF.md",
    "docs/LEAD-GUIDE.md",
    "docs/playbook/index.md",
    "docs/playbook/build-and-test.md",
    "docs/playbook/challenges.md",
    "docs/playbook/dispatch.md",
    "docs/playbook/lead-conduct.md",
    "docs/playbook/learning.md",
    "docs/playbook/machine-safety.md",
    "docs/playbook/merge-gate.md",
    "docs/playbook/review.md",
    "docs/playbook/writing-for-people.md",
    "docs/supervisor-tutorial.md",
    "docs/supervisor-hosting.md",
    "docs/USER-MANUAL.md",
)
LINK = re.compile(r"\]\(([^)\s]+)\)")


def installed_path(path):
    """Where a repository file sits in the wheel, or None when the wheel does not carry it."""
    if path in LEAD_DOCS:
        return "agenttalk/" + path
    if path.startswith("src/agenttalk/"):
        return path.removeprefix("src/")
    return None


def rewrite_links(page, text, version):
    """Rewrite the link targets of one lead page for its place in the wheel; nothing else changes."""

    def rewrite(match):
        target, hash_mark, anchor = match.group(1).partition("#")
        if target.startswith(REPO_WEB + "master/"):
            path = target.removeprefix(REPO_WEB + "master/")
        elif not target or re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", target):
            return match.group(0)
        else:
            path = posixpath.normpath(posixpath.join(posixpath.dirname(page), target))
        installed = installed_path(path)
        if installed:
            new_target = posixpath.relpath(installed, posixpath.dirname(installed_path(page)))
        else:
            new_target = f"{REPO_WEB}v{version}/{path}"
        return f"]({new_target}{hash_mark}{anchor})"

    return LINK.sub(rewrite, text)


class LeadDocsBuildHook(BuildHookInterface):
    """Stage the rewritten pages in a fresh temporary folder and add them to the wheel."""

    def initialize(self, version, build_data):
        if self.target_name != "wheel" or version == "editable":
            return
        self._staging = tempfile.TemporaryDirectory(prefix="agenttalk-lead-docs-")
        for page in LEAD_DOCS:
            # A missing page raises here and fails the build, so a wheel never ships without it.
            text = (Path(self.root) / page).read_text(encoding="utf-8")
            staged = Path(self._staging.name) / page
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(rewrite_links(page, text, self.metadata.version).encode("utf-8"))
            build_data["force_include"][str(staged)] = installed_path(page)

    def finalize(self, version, build_data, artifact_path):
        staging = getattr(self, "_staging", None)
        if staging is not None:
            staging.cleanup()
