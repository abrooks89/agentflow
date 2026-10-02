# Contributing to agentflow

Thanks for looking. This page covers what you need installed, how to run the suite the way CI
runs it, and the gates a pull request has to pass. It is written for people. Claude Code sessions
working in this repo follow [`CLAUDE.md`](CLAUDE.md), which covers the same gates from their side.

Found a security problem? Don't open a public issue. See [`SECURITY.md`](SECURITY.md).

## What you need

- Python 3.11 or newer
- git
- bash. On Windows, use the Git Bash that ships with Git.
- Node LTS, for the tree-sitter inventory tests and for the graph package's end-to-end tests,
  which drive the Node inventory.

## Running the suite

These are the main commands CI runs, copied from
[`.github/workflows/ci.yml`](.github/workflows/ci.yml). Each one says where to run it. Start
every block from the repo root.

**The `_shared` package** (config, registry, plan parser, waves). CI job `shared-tests`:

```bash
cd skills/_shared
python -m pip install -e ".[test]"
python -m pytest -q
```

**Install and provision**. CI job `install-tests`, run from the repo root:

```bash
python -m pip install -e skills/_shared pytest
python -m pytest install -q
```

**The tree-sitter inventory (Node)**. CI job `inventory-tests`:

```bash
cd skills/autodoc-init/references/inventory
npm ci
node --test
```

**The autodoc graph package**. CI job `graph-tests`. Its end-to-end tests drive the Node
inventory, so run the inventory's `npm ci` above first:

```bash
cd skills/autodoc-init/references/graph
python -m pip install -e ".[test]"
python -m pytest -q
```

**The acceptance harness**. CI job `harness-self-test`, run from the repo root:

```bash
python harness/check.py --self-test
python harness/assert_doc_counts.py
```

**Lint**. CI job `lint`, run from the repo root:

```bash
python -m pip install "ruff==0.15.10"
python -m ruff check .
```

## The gates your pull request meets

CI runs on Ubuntu and Windows. Five checks are the ones an outside PR is most likely to trip.

### Provenance and review pointer

**Where it lives:** the `pr-provenance-gate` job in `ci.yml`. The review-pointer rule and the
reason for it are in [`skills/_shared/PR-PROSE.md`](skills/_shared/PR-PROSE.md), under
*Review pointer*.

**What trips it:** the PR doesn't say where the work came from, or its body doesn't say where the
review went. The job checks both and reports both failures in the same run.

**How to pass it as an outside contributor:**

1. **Say where the work came from.** Either:
   - open an issue first, the way most projects expect, and put `Closes #<n>` in the PR, using the
     issue's number. The gate looks the number up. It has to be an issue in this repository. A
     pull request number doesn't count, even though GitHub numbers them the same way.
   - or, for a typo, a comment, or a formatting-only change, write `Source: trivial` on a line of
     its own.

   The `Epic:` lines in the template are for maintainers, who cite planning records that live
   outside the repo. Leave them out.
2. **Say where the review went.** Put exactly one line in the PR **body**:

   ```
   Review: none
   ```

   Write a link to a review comment instead if one exists. The line has to be filled in. The
   template's unfilled placeholder fails, and so do two `Review:` lines.

The PR template has both lines ready to fill in. The gate runs again whenever you edit the PR
description, so you can fix a failure by editing the description. You don't need a new commit.

### Sanitize gate

**Where it lives:** the `sanitize-gate` job in `ci.yml`, step *Grep for personal/product
literals*. Its pattern has two sources, and this page copies neither. The `pattern=` line in
`ci.yml` ships with the tree, so it holds only generic shapes that name nobody. A maintainer's
install-specific alternatives would be published by that same file, so they never go there:
they live in a local-only file under the data root
(`<data_root>/projects/<project>/publish/sanitize-extra-pattern.txt`, see
[`docs/runbooks/publish.md`](docs/runbooks/publish.md), precondition 3) and, for CI, in the
working repository's Actions variable `SANITIZE_EXTRA_PATTERN`, which the step appends to the
same scan. A repository without the variable runs the generic shapes alone and says so in one
notice line.

**What trips it:** a literal that ties the tree to one person's machine. The shipped shapes
catch:

- Claude Code's per-machine project-directory names
- absolute home paths that include a real account name, on Windows, MSYS, Linux or macOS

The extra pattern adds whatever is specific to one install, such as the names of certain other
products, private network addresses or personal SSH host shortcuts.

The gate searches every first-party text file in the tree (vendored code, `node_modules` and
fixture repositories are excluded), including comments, docstrings and test fixtures. It
also searches **your PR title and body**, because those are published next to the code. If the
failure is in the title or body, the log says so. Edit the description; there is nothing to fix in
the checkout.

**How to fix it:** remove the literal, or replace it with a placeholder. The gate allows a set of
placeholder account names, so documentation can show a home path without naming anyone:

```
/home/you/agentflow
C:/Users/you/agentflow
```

Watch for this one: printing `agentflow.__file__` to prove which tree a test run imported
produces a real home path. Don't paste that output into the PR body as it is.

**Run it locally before you push.** Open `.github/workflows/ci.yml` and find the step *Grep for
personal/product literals* in the `sanitize-gate` job. From the repo root, in bash, run its
`export LC_ALL=C.UTF-8` line, then its `pattern=` line, then its `grep -rniP` command with all of
its `--exclude` flags. No output means the tree is clean of the generic shapes. If you hold the
local extra pattern file, set `EXTRA_PATTERN` to its alternatives joined with `|` and run the
step's `if` block too, so your local scan matches CI's. Your PR title and body are only checked
in CI.

### Instruction-prose history gate

**Where it lives:** the `sanitize-gate` job in `ci.yml`, step *Instruction prose carries no
history*, which runs [`.github/scripts/prose_history_gate.py`](.github/scripts/prose_history_gate.py).
It is a rule for people building agentflow, not a product feature: it checks this repository's
own prose and never runs on a user's project.

**What trips it:** event history in the instruction prose agents read. That prose is every tracked
`skills/**/*.md` (except `skills/_vendored/` and `test/` and `tests/` directories),
`commands/*.md`, `templates/*`, the skills' `templates/` files, and `install/SKILL.md`. The gate
derives the set from `git ls-files`, so a new skill file is covered from its first commit. In
that prose it refuses:

- an issue reference, `gh#<n>` or `issue #<n>`
- a registry or queue id such as `Q-<n>`, `B-<n>` or `E-<n>`, except the `Q-00NN` and `B-00NN`
  example ranges; a queue example uses the placeholder `E-NNNN`
- a date, unless it is listed in the script's `DATE_OK` table
- the copyright holder's name, read from `LICENSE`

Code, tests, `docs/`, `README.md`, `CHANGELOG.md` and this file are outside the set. History
belongs there.

**How to fix it:** restate the rule and its reason in the present tense, as a property of the
system, and drop the event. Put the history in the commit message, the PR body, a code comment or
test docstring (which keep their refs), `CHANGELOG.md`, the registry, or the `wiki/` pillar, as
the gate's error message lists. To add a dated
example (a config value, a path or a format example), give it a `DATE_OK` entry with its reason
in the script. An entry whose text is no longer in its file fails the gate.

**What it cannot see:** narrative with no token in it, commit shas and priority handles (the
prose teaches with examples of both), the text a `.js` prompt builder sends, and a real id that
happens to sit in the example range. Review reads for those.

**Run it locally before you push.** From the repo root:

```
python .github/scripts/prose_history_gate.py
```

Pass one or more repo-relative paths to check only those files. It exits 0 when clean, 1 on a
finding, and 2 when the scan could not run; 2 is never a pass.

### README evidence counts

**Where it lives:**
[`skills/_shared/tests/test_evidence_table.py`](skills/_shared/tests/test_evidence_table.py),
which runs as part of the `_shared` suite.

**What trips it:** adding or removing tests. Each row of the [`README.md`](README.md#evidence)
*Evidence* table prints a count beside the command that produces it. The guard re-runs the rows it
can run where it is running (in CI, the `_shared`, install and harness rows) and fails when a
count no longer matches. The graph and Node inventory rows are not compared in CI, so update
those by hand.

**How to fix it:** re-run the command in the row for the layer you changed and put the new number
in that row.

**A local run may not catch it.** The guard compares counts only on Linux, which in CI is the
`ubuntu-latest` leg, because test collection can differ between platforms. If you run the suite on
Windows, a stale count passes on your machine and fails in CI. Update the row whenever your change
adds or removes tests, whatever your local run says.

### ruff

**Where it lives:** the `lint` job in `ci.yml`. The rule set and its two exclusions are in
[`ruff.toml`](ruff.toml).

**What trips it:** dead code and real mistakes (pyflakes and pycodestyle errors), not style
opinions.

**How to fix it:** install the pinned version and run it from the repo root, as shown under
[Running the suite](#running-the-suite). Other ruff versions can report different findings.

## Loop-authored PRs

agentflow's own loops write PRs following [`skills/_shared/PR-PROSE.md`](skills/_shared/PR-PROSE.md).
That includes an `Agentflow [<lane>]` attribution line at the top of the body. That line is only
for loops. Leave it out of a PR you write yourself. One rule in that file applies to everyone:
don't publish Claude Code session links in commits, PRs, comments or tracked files.
