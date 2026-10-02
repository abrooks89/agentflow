"""Read a target checkout and recommend how its work should be verified.

The install interview asks the user how the system should decide a change is
good (``verification.strategy``). Most people cannot answer that cold, and the
honest answer - "I don't know" - used to leave the interviewer guessing or the
user picking the first plausible option. The evidence is already on disk: what
the project is written in, whether it has a user-facing surface, what test
tooling it already carries.

This module reads that evidence and turns it into a recommendation *with its
reasons attached*, so the interviewer proposes rather than decides and the user
can overrule it knowing what it saw.

Deliberately shallow. It reads manifests and looks for marker files; it does not
parse source, execute anything, or hit the network. A wrong guess here costs a
follow-up question, so cheap and explainable beats clever.

Detection is evidence-based and additive: an unknown stack yields empty evidence
and a `none` recommendation that says so plainly, never a confident default.

CLI:  python -m agentflow.project_probe <checkout>
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #

#: manifest filename -> language label
_MANIFESTS = {
    "package.json": "javascript/typescript",
    "pyproject.toml": "python",
    "setup.py": "python",
    "requirements.txt": "python",
    "go.mod": "go",
    "Cargo.toml": "rust",
    "Gemfile": "ruby",
    "pom.xml": "java",
    "build.gradle": "java",
    "build.gradle.kts": "java",
    "composer.json": "php",
}

#: marker -> the browser-testing tool it implies
_BROWSER_MARKERS = {
    "playwright.config.ts": "playwright",
    "playwright.config.js": "playwright",
    "playwright.config.mjs": "playwright",
    "cypress.config.ts": "cypress",
    "cypress.config.js": "cypress",
}

#: marker -> the web framework it implies (i.e. this project renders a UI)
_UI_MARKERS = {
    "next.config.ts": "next.js",
    "next.config.js": "next.js",
    "next.config.mjs": "next.js",
    "vite.config.ts": "vite",
    "vite.config.js": "vite",
    "svelte.config.js": "svelte",
    "nuxt.config.ts": "nuxt",
    "angular.json": "angular",
    "index.html": "static html",
}

#: dependency name -> the UI framework it implies (package.json deps)
_UI_DEPS = ("react", "vue", "svelte", "@angular/core", "solid-js", "preact")


@dataclass
class Evidence:
    """What the checkout demonstrably contains. Empty fields mean 'not found'."""

    checkout: str = ""
    exists: bool = False
    languages: list = field(default_factory=list)
    ui_frameworks: list = field(default_factory=list)
    browser_tools: list = field(default_factory=list)
    test_commands: list = field(default_factory=list)
    e2e_dirs: list = field(default_factory=list)
    dev_commands: list = field(default_factory=list)
    containerized: bool = False

    @property
    def has_ui(self) -> bool:
        return bool(self.ui_frameworks)

    @property
    def has_suite(self) -> bool:
        return bool(self.test_commands)

    @property
    def has_browser_tooling(self) -> bool:
        return bool(self.browser_tools or self.e2e_dirs)


@dataclass
class Recommendation:
    """A proposed `verification.strategy`, the evidence for it, and what's missing."""

    strategy: str
    reasons: list = field(default_factory=list)
    follow_ups: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Probe
# --------------------------------------------------------------------------- #
def _read_package_json(path: Path) -> tuple:
    """Return (scripts, dependency names) - ({}, []) when unreadable.

    A malformed package.json is evidence we cannot use, not a crash: the probe
    degrades to "found nothing here" and the interview asks the user instead.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, []
    if not isinstance(data, dict):
        return {}, []
    scripts = data.get("scripts") if isinstance(data.get("scripts"), dict) else {}
    deps = []
    for key in ("dependencies", "devDependencies"):
        block = data.get(key)
        if isinstance(block, dict):
            deps.extend(block.keys())
    return scripts, deps


def probe(checkout) -> Evidence:
    """Read `checkout` and report what verification tooling it already carries."""
    root = Path(checkout).expanduser()
    ev = Evidence(checkout=str(root))
    if not root.is_dir():
        return ev
    ev.exists = True

    for manifest, language in _MANIFESTS.items():
        if (root / manifest).is_file() and language not in ev.languages:
            ev.languages.append(language)

    for marker, tool in _UI_MARKERS.items():
        if (root / marker).is_file() and tool not in ev.ui_frameworks:
            ev.ui_frameworks.append(tool)

    for marker, tool in _BROWSER_MARKERS.items():
        if (root / marker).is_file() and tool not in ev.browser_tools:
            ev.browser_tools.append(tool)

    for name in ("e2e", "tests/e2e", "integration", "cypress"):
        if (root / name).is_dir():
            ev.e2e_dirs.append(name)

    ev.containerized = (root / "Dockerfile").is_file() or (
        root / "docker-compose.yml"
    ).is_file()

    # --- javascript/typescript: scripts are the authoritative command list ---
    pkg = root / "package.json"
    if pkg.is_file():
        scripts, deps = _read_package_json(pkg)
        for script in ("test", "e2e", "test:e2e"):
            if script in scripts:
                ev.test_commands.append(f"npm run {script}")
        for script in ("dev", "start"):
            if script in scripts:
                ev.dev_commands.append(f"npm run {script}")
        for dep in deps:
            if dep in _UI_DEPS and dep not in ev.ui_frameworks:
                ev.ui_frameworks.append(dep)
            if dep in ("playwright", "@playwright/test") and "playwright" not in ev.browser_tools:
                ev.browser_tools.append("playwright")
            if dep == "cypress" and "cypress" not in ev.browser_tools:
                ev.browser_tools.append("cypress")

    # --- python / go / rust: conventional commands, gated on real markers ---
    if (root / "tests").is_dir() or (root / "pyproject.toml").is_file():
        if "python" in ev.languages:
            ev.test_commands.append("pytest")
    if (root / "go.mod").is_file():
        ev.test_commands.append("go test ./...")
    if (root / "Cargo.toml").is_file():
        ev.test_commands.append("cargo test")

    return ev


# --------------------------------------------------------------------------- #
# Recommend
# --------------------------------------------------------------------------- #
def recommend(ev: Evidence) -> Recommendation:
    """Turn evidence into a proposed strategy. Never returns a silent default."""
    if not ev.exists:
        return Recommendation(
            strategy="none",
            reasons=[f"no checkout found at {ev.checkout} - nothing to read"],
            follow_ups=["clone the project first, then re-run the probe"],
        )

    if not (ev.languages or ev.ui_frameworks or ev.test_commands):
        return Recommendation(
            strategy="none",
            reasons=["no manifest, UI marker, or test tooling found - the probe has no evidence"],
            follow_ups=["answer from what you know about the project; the probe found nothing to go on"],
        )

    reasons = []
    follow_ups = []

    if ev.has_ui:
        reasons.append(
            "has a user-facing surface (" + ", ".join(ev.ui_frameworks) + "), so a change "
            "can break what a person sees while every unit test still passes"
        )
        if ev.has_suite and ev.has_browser_tooling:
            reasons.append(
                "already carries both a suite (" + ", ".join(ev.test_commands) + ") and browser "
                "tooling (" + ", ".join(ev.browser_tools + ev.e2e_dirs) + ")"
            )
            follow_ups.append(
                "point verification.strategy at a SKILL.md that runs the suite AND drives the "
                "journeys - the enum values are single-mode, this project is not"
            )
            follow_ups.append("a staging.url is required for the browser half")
            return Recommendation("<SKILL.md path>", reasons, follow_ups)

        if ev.has_browser_tooling:
            reasons.append("browser tooling present: " + ", ".join(ev.browser_tools + ev.e2e_dirs))
        else:
            follow_ups.append(
                "no browser tooling found - the journey gate needs something to drive the UI"
            )
        if ev.has_suite:
            follow_ups.append(
                "keep " + ", ".join(ev.test_commands) + " as a regression floor, not the gate"
            )
        follow_ups.append("web-journey requires a staging.url - a running instance to drive")
        return Recommendation("web-journey", reasons, follow_ups)

    if ev.has_suite:
        reasons.append(
            "no user-facing surface detected, and a test suite is present ("
            + ", ".join(ev.test_commands) + ")"
        )
        follow_ups.append(
            "if this project does render a UI the probe missed, say so - a suite gate cannot see it"
        )
        return Recommendation("test-suite", reasons, follow_ups)

    reasons.append(
        "code found (" + ", ".join(ev.languages) + ") but no test tooling and no UI markers"
    )
    follow_ups.append("smoke-cli fits if it is a command-line tool with an observable exit path")
    return Recommendation("none", reasons, follow_ups)


def format_report(ev: Evidence, rec: Recommendation) -> str:
    """Human-readable block for the interviewer to show the user."""
    lines = [f"probe: {ev.checkout}"]
    if ev.exists:
        lines.append("  languages       : " + (", ".join(ev.languages) or "-"))
        lines.append("  ui frameworks   : " + (", ".join(ev.ui_frameworks) or "-"))
        lines.append("  browser tooling : " + (", ".join(ev.browser_tools + ev.e2e_dirs) or "-"))
        lines.append("  test commands   : " + (", ".join(ev.test_commands) or "-"))
        lines.append("  dev commands    : " + (", ".join(ev.dev_commands) or "-"))
        lines.append("  containerized   : " + ("yes" if ev.containerized else "no"))
    lines.append("")
    lines.append(f"  RECOMMENDED verification.strategy: {rec.strategy}")
    for reason in rec.reasons:
        lines.append(f"    because: {reason}")
    for follow_up in rec.follow_ups:
        lines.append(f"    note:    {follow_up}")
    lines.append("")
    lines.append("  This is a proposal from what is on disk. The user decides.")
    return "\n".join(lines)


def main(argv=None) -> int:
    # First, before any output: a legacy cp1252 stdout cannot carry a non-cp1252
    # character a lane wrote (gh#574).
    from agentflow._stdio import utf8_stdio

    utf8_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        sys.stderr.write("usage: python -m agentflow.project_probe <checkout>\n")
        return 2
    ev = probe(argv[0])
    print(format_report(ev, recommend(ev)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
