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

Deliberately shallow. It reads manifests and looks for marker files in the root
and up to two directory levels below it (a monorepo's `frontend/` or `apps/web/`),
skipping dependency, vendored, build, fixture, docs and example trees; anything
found below the root names the directory it came from. UI evidence inside a docs,
example or sample tree is listed as seen but not counted, so "no UI" can be told
apart from "a UI the probe deliberately set aside". A CI workflow counts as a
test suite only when one of its `run:` commands starts a known runner. It does not
parse source, execute anything, or hit the network. A wrong guess here costs a follow-up
question, so cheap and explainable beats clever.

Detection is evidence-based and additive: an unknown stack yields empty evidence
and a `none` recommendation that says so plainly, never a confident default.

CLI:  python -m agentflow.project_probe <checkout>
"""

from __future__ import annotations

import errno
import json
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    from agentflow._stdio import utf8_stdio
except ModuleNotFoundError as missing:
    # Only an ABSENT package (or an older one without the helper) falls back; a package that
    # is present but fails to import is a real fault and stays loud.
    if missing.name not in ("agentflow", "agentflow._stdio"):
        raise
    # The SCRIPT form (`python skills/_shared/agentflow/project_probe.py <checkout>`) is the
    # one the install interview runs before any venv exists, so the `agentflow` package is
    # not importable: run as a file, Python puts this directory on sys.path, not its parent,
    # and under -I it puts nothing there at all. The helper is stdlib-only, so load the
    # sibling file by path rather than keeping a second copy of it here.
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "_agentflow_stdio_for_script_form", Path(__file__).with_name("_stdio.py")
    )
    if _spec is None or _spec.loader is None:
        raise ImportError(f"cannot load _stdio.py beside {__file__}") from None
    _stdio_module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_stdio_module)
    utf8_stdio = _stdio_module.utf8_stdio

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
    container_files: list = field(default_factory=list)
    unreadable: list = field(default_factory=list)
    #: UI evidence seen inside a docs/examples/samples tree. Never feeds the verdict;
    #: it lets a reader overrule the skip when that tree IS the product's UI.
    not_counted: list = field(default_factory=list)

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
def _read_package_json(path: Path, label: str, unreadable: list) -> tuple:
    """Return (scripts, dependency names) - ({}, []) when unreadable.

    A malformed package.json is evidence we cannot use, not a crash: the probe
    degrades to "found nothing here" and the interview asks the user instead. It
    goes on `unreadable` under `label` (checkout-relative), so the report says so.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        unreadable.append(f"{label} ({type(exc).__name__})")
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


#: How far below the checkout root to look. A monorepo keeps its frontend in
#: `frontend/` or `apps/web/`; reading only the root gave such a project a confident
#: "no user-facing surface". Two levels covers those layouts without becoming a walk.
_SCAN_DEPTH = 2

#: Directory names never scanned: installed dependencies, vendored copies, build
#: output and test fixtures carry manifests that describe someone else's project.
#: Documentation and example trees describe the project without being it: a CLI
#: library whose docs site is a React/Vite app must not be read as having a UI.
#: Dot-directories (.git, .venv, .github, ...) are skipped by prefix.
#: The docs/example names are split out because their UI evidence is still reported
#: (as not counted): a product's real UI can live under examples/, and only a person
#: can say so. Dependency and build trees stay silent - they are never the product.
_NOT_PRODUCT_DIRS = frozenset({"docs", "doc", "examples", "example", "samples", "sample"})
_SKIP_DIRS = frozenset({
    "node_modules", "venv", "env", "__pycache__",
    "_vendored", "vendor", "third_party",
    "fixtures", "testdata",
    "dist", "build", "target", "htmlcov",
}) | _NOT_PRODUCT_DIRS

#: A workflow `run:` command counts as a test suite only when it starts with one of
#: these. Mentioning a runner is not running it (`pip install pytest`), so the match
#: is anchored at the start of a command.
_CI_RUNNER = re.compile(
    r"(?:python[\d.]*\s+-m\s+pytest|pytest|npm\s+(?:run\s+)?test|go\s+test|cargo\s+test)(?=\s|$)"
)
_CI_RUN_KEY = re.compile(r"^(\s*)(?:-\s+)?run:\s*(.*)$")

#: The errors pathlib's is_file()/is_dir()/is_symlink() read as "not there" before Python
#: 3.14: the path is absent, or is not reachable as a path at all (a link loop). Windows
#: reports some of these only as winerror: not ready, invalid name, cannot resolve (loop).
_ABSENT_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP})
_ABSENT_WINERRORS = frozenset({21, 123, 1921})


def _mode(path: Path, follow: bool):
    """Return `path`'s st_mode, or None when it is absent; raise when it is there but unreadable.

    From Python 3.14 the Path.is_* checks answer False for EVERY OSError, so a directory
    the probe cannot enter would read as empty rather than unreadable. These helpers keep
    the older contract on every version: missing is False, PermissionError (or any other
    OSError) propagates to the caller that reports it.
    """
    try:
        return (path.stat() if follow else path.lstat()).st_mode
    except OSError as exc:
        if exc.errno in _ABSENT_ERRNOS or getattr(exc, "winerror", None) in _ABSENT_WINERRORS:
            return None
        raise


def _is_file(path: Path) -> bool:
    mode = _mode(path, follow=True)
    return mode is not None and stat.S_ISREG(mode)


def _is_dir(path: Path) -> bool:
    mode = _mode(path, follow=True)
    return mode is not None and stat.S_ISDIR(mode)


def _is_symlink(path: Path) -> bool:
    mode = _mode(path, follow=False)
    return mode is not None and stat.S_ISLNK(mode)


def _scan_dirs(root: Path, unreadable: list, not_product: list | None = None) -> list:
    """Return (directory, prefix) for root and its subdirectories to _SCAN_DEPTH.

    `prefix` is the path relative to root with a trailing slash ('' for root), so
    evidence found below the root can say where it came from. A directory that
    cannot be listed goes on `unreadable`, so the report can say what it never saw.
    A skipped docs/examples/samples tree goes on `not_product` (when given) as
    (directory, prefix, remaining depth), so its UI evidence can be reported.
    """
    return _walk(root, "", _SCAN_DEPTH, unreadable, not_product)


def _walk(top: Path, top_prefix: str, depth: int, unreadable: list,
          not_product: list | None) -> list:
    found = [(top, top_prefix)]
    frontier = [(top, top_prefix)]
    for level in range(depth):
        deeper = []
        for directory, prefix in frontier:
            try:
                children = sorted(directory.iterdir())
            except OSError as exc:
                # Not a crash - the rest of the tree is still evidence - but not silent
                # either: a recommendation that never saw frontend/ must say so.
                unreadable.append(f"{prefix or './'} ({type(exc).__name__})")
                continue
            for child in children:
                name = child.name
                if name.startswith("."):
                    continue
                try:
                    # Symlinks can point outside the checkout; its evidence must be its own.
                    if _is_symlink(child) or not _is_dir(child):
                        continue
                except OSError as exc:
                    # A directory that can be listed but not searched names its children
                    # and then refuses to stat them. The directory is what cannot be
                    # entered, so it is the one reported - once, not once per child.
                    _add(unreadable, f"{prefix or './'} ({type(exc).__name__})")
                    continue
                if name in _SKIP_DIRS:
                    if not_product is not None and name in _NOT_PRODUCT_DIRS:
                        not_product.append((child, f"{prefix}{name}/", depth - level - 1))
                    continue
                deeper.append((child, f"{prefix}{name}/"))
        found.extend(deeper)
        frontier = deeper
    return found


def _add(bucket: list, item: str) -> None:
    if item not in bucket:
        bucket.append(item)


def _probe_dir(ev: Evidence, directory: Path, prefix: str) -> None:
    """Record the evidence one directory carries, naming it when it is not the root."""

    def at(item: str, source: str) -> str:
        # Root evidence keeps its bare label; anything deeper says where it was seen,
        # so the user can overrule a finding that came from the wrong place.
        return item if not prefix else f"{item} in {prefix}{source}"

    local_languages = []
    for manifest, language in _MANIFESTS.items():
        if _is_file(directory / manifest):
            _add(local_languages, language)
            _add(ev.languages, at(language, manifest))

    for marker, tool in _UI_MARKERS.items():
        if _is_file(directory / marker):
            _add(ev.ui_frameworks, at(tool, marker))

    for marker, tool in _BROWSER_MARKERS.items():
        if _is_file(directory / marker):
            _add(ev.browser_tools, at(tool, marker))

    for name in ("e2e", "tests/e2e", "integration", "cypress"):
        if _is_dir(directory / name):
            _add(ev.e2e_dirs, f"{prefix}{name}")

    for container_file in ("Dockerfile", "docker-compose.yml"):
        if _is_file(directory / container_file):
            ev.containerized = True
            _add(ev.container_files, f"{prefix}{container_file}")

    # --- javascript/typescript: scripts are the authoritative command list ---
    pkg = directory / "package.json"
    if _is_file(pkg):
        scripts, deps = _read_package_json(pkg, f"{prefix}package.json", ev.unreadable)
        for script in ("test", "e2e", "test:e2e"):
            if script in scripts:
                _add(ev.test_commands, at(f"npm run {script}", "package.json"))
        for script in ("dev", "start"):
            if script in scripts:
                _add(ev.dev_commands, at(f"npm run {script}", "package.json"))
        for dep in deps:
            if dep in _UI_DEPS:
                _add(ev.ui_frameworks, at(dep, "package.json"))
            if dep in ("playwright", "@playwright/test"):
                _add(ev.browser_tools, at("playwright", "package.json"))
            if dep == "cypress":
                _add(ev.browser_tools, at("cypress", "package.json"))

    # --- python / go / rust: conventional commands, gated on real markers ---
    if _is_dir(directory / "tests") or _is_file(directory / "pyproject.toml"):
        if "python" in local_languages:
            _add(ev.test_commands, at("pytest", ""))
    if _is_file(directory / "go.mod"):
        _add(ev.test_commands, at("go test ./...", ""))
    if _is_file(directory / "Cargo.toml"):
        _add(ev.test_commands, at("cargo test", ""))


def _workflow_commands(text: str) -> list:
    """Return the shell commands a workflow's `run:` keys carry, read textually.

    Stdlib only - the script form runs before any venv, so there is no YAML parser.
    Handles a one-line `run: cmd` and a `run: |` / `run: >` block (every following
    line indented deeper than the key). Anything stranger is simply not seen.
    """
    commands = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        match = _CI_RUN_KEY.match(lines[i])
        i += 1
        if not match:
            continue
        indent, value = len(match.group(1)), match.group(2).strip()
        if value[:1] in ("|", ">"):
            while i < len(lines):
                line = lines[i]
                if line.strip() and len(line) - len(line.lstrip()) <= indent:
                    break
                commands.append(line.strip())
                i += 1
        else:
            commands.append(value.strip("'\""))
    return commands


def _probe_workflows(ev: Evidence, root: Path) -> None:
    """Count a CI workflow as test evidence only when it runs a known runner."""
    workflows = root / ".github" / "workflows"
    try:
        if not _is_dir(workflows):
            return
        entries = sorted(workflows.iterdir())
    except OSError as exc:
        ev.unreadable.append(f".github/workflows/ ({type(exc).__name__})")
        return
    for path in entries:
        if path.suffix not in (".yml", ".yaml"):
            continue
        try:
            if not _is_file(path):
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            ev.unreadable.append(f".github/workflows/{path.name} ({type(exc).__name__})")
            continue
        for command in _workflow_commands(text):
            for segment in re.split(r"&&|\|\||;|\|", command):
                runner = _CI_RUNNER.match(segment.strip())
                if runner:
                    label = " ".join(runner.group(0).split())
                    _add(ev.test_commands, f"{label} in .github/workflows/{path.name}")


def _probe_not_counted(ev: Evidence, trees: list) -> None:
    """Record the UI evidence skipped docs/examples/samples trees carry, without counting it.

    Each tree is read into a scratch Evidence, within the depth budget left where it
    was found, and only its ui_frameworks entries are kept. A directory inside such a
    tree that cannot be read is dropped quietly, not put on ev.unreadable: that list
    warns the verdict may be missing evidence, and nothing here could change the verdict.
    """
    for tree, prefix, remaining in trees:
        scratch = Evidence()
        for directory, sub_prefix in _walk(tree, prefix, remaining, [], None):
            try:
                _probe_dir(scratch, directory, sub_prefix)
            except OSError:
                # A listable but unenterable directory raises from _is_file(); this tree
                # never feeds the verdict, so it must never be able to break the probe.
                continue
        for item in scratch.ui_frameworks:
            _add(ev.not_counted, item)


def probe(checkout) -> Evidence:
    """Read `checkout` and report what verification tooling it already carries."""
    root = Path(checkout).expanduser()
    ev = Evidence(checkout=str(root))
    if not root.is_dir():
        return ev
    ev.exists = True

    not_product = []
    for directory, prefix in _scan_dirs(root, ev.unreadable, not_product):
        try:
            _probe_dir(ev, directory, prefix)
        except OSError as exc:
            # A listable but unenterable directory raises from _is_file() on every Python
            # version (see _mode). Outside a docs tree it may hold the product, so unlike
            # _probe_not_counted this is reported: what was read before the error stands,
            # the rest is unseen.
            _add(ev.unreadable, f"{prefix or './'} ({type(exc).__name__})")
    _probe_workflows(ev, root)
    _probe_not_counted(ev, not_product)

    return ev


# --------------------------------------------------------------------------- #
# Recommend
# --------------------------------------------------------------------------- #
def recommend(ev: Evidence) -> Recommendation:
    """Turn evidence into a proposed strategy. Never returns a silent default."""
    rec = _recommend(ev)
    if ev.unreadable:
        rec.follow_ups.append(
            "the probe could not read " + ", ".join(ev.unreadable) + " - the recommendation "
            "may be missing evidence from those paths"
        )
    if ev.not_counted and not ev.has_ui:
        rec.follow_ups.append(
            "UI evidence was seen under a docs/examples/samples tree and not counted ("
            + ", ".join(ev.not_counted) + ") - if that is the product's UI, say so"
        )
    return rec


def _recommend(ev: Evidence) -> Recommendation:
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
        lines.append(
            "  containerized   : "
            + (f"yes ({', '.join(ev.container_files)})" if ev.containerized else "no")
        )
        if ev.unreadable:
            lines.append("  unreadable      : " + ", ".join(ev.unreadable))
        if ev.not_counted:
            lines.append("  not counted     : " + ", ".join(ev.not_counted))
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
    # character a lane wrote (gh#574). Resolved at import so the script form works too.
    utf8_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    # A typo'd path, a file, or `--help` used to print a "no checkout found" report and
    # exit 0, which reads as an answer. None of them is one, so all are usage errors.
    if len(argv) != 1 or argv[0].startswith("-") or not Path(argv[0]).expanduser().is_dir():
        if len(argv) == 1 and not argv[0].startswith("-"):
            sys.stderr.write(f"not a directory: {argv[0]}\n")
        sys.stderr.write("usage: python -m agentflow.project_probe <checkout>\n")
        return 2
    ev = probe(argv[0])
    print(format_report(ev, recommend(ev)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
