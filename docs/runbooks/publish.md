# Runbook: publishing a fresh single-commit cut

**Who runs this: a human. Always.**

No loop, builder, or automation in this suite pushes to the publish target, opens a pull
request against it, or grounds against it. The `publish_repo` key exists so the resolved
config can *name* the public repo; it is read-only to every builder, and the schema says so
next to the key (`skills/_shared/SCHEMA.md` §2). If you are an agent reading this file
because a task mentioned publishing: stop, and hand it back to the human.

Every command below marked **HUMAN ONLY** changes something outside the local machine: a
push, a repo create or rename, a tag, an invite, a settings change. They are written here as
text for the human to run. No agent runs them, and `python -m agentflow.publish` never runs
any of them either: it builds a cut in a local directory and checks it, and that is all.

---

## What a publish is

Daily work lands in the **working repo** (`canonical_repo`), which is private. The share
repo (`publish_repo`) receives **one fresh commit**: the tree of a chosen working-repo commit,
with no parent and a neutral noreply author. None of the working history goes out. The
exclusion list (`.env`, `.env.*`, `*.pem`, `.worktrees/`) is a tripwire, not a filter: the
cut leaves a matching path out of its tree and then fails, so a cut passes only once the
working repo carries no such path. So a passing cut's tree is the whole commit's tree.

There is no continuous mirror, no post-merge hook, no scheduled sync.
`publish_repo.cadence` is prose describing this human act; nothing reads it as a schedule,
and nothing should be built that does. If publishing ever becomes automatic, the
private/public split stops being a decision and becomes an accident waiting on a merge.

**Absent `publish_repo` is the normal state.** It means there is no public repo yet, and it
is the correct encoding of that. Do not add the key to make a config look complete.

### Why the old history push is gone

An earlier version of this runbook pushed the working history to a `publish` remote. Its own
precondition 2 refused that history: some working-repo commits carry an assistant session URL
or trailer, and every commit carries a personal author address. Following it either failed its
own precondition or published what it forbade. Rewriting that history and then making the
working repo public would not fix it either, and that is why the working repo is never made
public and a rewrite is not a route to sharing. The host keeps what a rewrite leaves behind:
GitHub keeps every pull request's `refs/pull/*` refs, serves an orphaned object to anyone who
asks for its SHA, keeps the edit history of every issue and pull request, and retains Actions
run logs. None of those can be cleaned from the repo side, so flipping the working repo to
public would publish all of them, whatever the rewritten branches say. The decision is
permanent for that reason, not by preference; a future "just flip it" meets the same four
facts. The only share that satisfies the preconditions is a fresh cut in a new repo, and that
is the only procedure here.

**Updating the share after its first cut is not yet a supported procedure.** The cut shares
no history with the working repo, so there is no range to push and no fast-forward to take.
Do not improvise one; stop and design it first.

---

## Preconditions: every one of these, verified, not assumed

Run these against the exact commit you are about to cut. A green run from last week is not
evidence about this week's commit. Step (0) below runs them in order.

1. **The sanitize gate is green.** The CI job named `sanitize grep gate` must pass on the
   commit you are cutting. It greps the whole first-party tree, comments, docstrings and test
   fixtures too, because those ship exactly like code does. Its pattern has two sources.
   `ci.yml` ships in the cut, so the pattern written there holds only generic shapes that name
   nobody: machine-specific paths such as absolute home directories. The install-specific
   alternatives (product names, hostnames, private address ranges) come from the working
   repo's `SANITIZE_EXTRA_PATTERN` repository secret, which mirrors the local extra pattern
   file of precondition 3 and is appended to the same scan. Check that the secret is set on
   the working repo (**HUMAN ONLY** to change: a settings change); a run without it logs
   `no extra pattern configured` and scans the generic shapes alone. Do not cut on a red or skipped
   sanitize gate, or on a run that only covered an older commit. On a private repo a push to
   master runs no CI job, so the master commit shows a skipped `sanitize grep gate`: before
   cutting, run `gh workflow run ci.yml --ref master` and wait for its `sanitize grep gate` to
   pass on `<rev>`, unless the last green nightly (where the check reads
   `ci / sanitize grep gate`) tested exactly `<rev>`. (The cut's own
   `sanitize-terms` check re-applies the same union, the cut's `ci.yml` pattern plus the local
   extra pattern file, with none of the CI job's directory exclusions, because the cut ships
   `skills/_vendored/`.)
2. **No session URL or session trailer in the cut.** The `session-urls` check of
   `python -m agentflow.publish check` scans every file of the cut and the cut commit's
   message for the session-URL shape `agentflow/publish.py` defines and for a session trailer
   at the start of a line. The two rule statements that forbid them (`CLAUDE.md` and
   `skills/_shared/PR-PROSE.md`) match neither form. The working history is not scanned
   because it is never published.
3. **No personal literals.** Real names, usernames, machine hostnames, absolute home-directory
   paths, private IP addresses, tokens, internal URLs. Use placeholders in tracked files.
   The sanitize gate catches the known shapes; it does not catch a literal nobody has taught
   it about yet, so read the diff.

   The bare personal name is not in the sanitize pattern and, by decision (precondition 4),
   never joins it. The cut's `person-name` REPORT line looks for it instead: it reads
   `person.name` from the environment config (or `--person-name <name>`), matches it ignoring
   case as a whole word over every text file in the cut, and prints each hit as `path:line`,
   never the name. Only the `Copyright (c)` line of `LICENSE` is exempt,
   because the license names its holder on purpose; the name on any other line of `LICENSE` is
   a hit. A path holding the name prints as its position, as a path holding a term does,
   but matched as a whole word, as the report matches it, where a term masks a path by plain
   substring. The line never changes the exit code, and when the environment config will not
   load it reads `not measured: <why>`.

   A term that must never ship cannot be taught to the sanitize pattern, because `ci.yml`
   ships in the cut and would publish it. Put such terms (a former employer's name, a personal
   address) in the **local-only** list
   `<data_root>/projects/<project>/publish/sensitive-terms.txt`, which lives in the data root
   and never ships. One term per line. Blank lines are skipped, and so is a line whose first
   non-blank character is `#` (a whole-line comment); a `#` anywhere else is part of the term.

   A term matches ignoring case, as a whole word where only a letter or digit continues a word:
   an underscore or punctuation is a boundary, so `term` is found in `TERM_API_KEY`,
   `term_client` and `jane@term.example`, but not in `terms` or `term2`. A term that starts or
   ends with punctuation (`@term.example`) matches wherever that text appears. The cut's
   `sensitive-terms` check reports a hit as its file, line and list line number, never the
   term, so a run log cannot republish it. Every check prints paths the same way: a path that
   holds a term anywhere, even inside a longer word, prints as `<cut path #K>` (or
   `<dropped path #K>`), its position in the cut's file listing, never the path itself. The
   cut's own refusals (a submodule entry, a file the tree did not take) run before any check
   and mask the same way, as `<source path #K>`; if the list cannot be read, they withhold
   every path they name. A git step that fails while the cut is written prints git's own
   message, which can name the path it failed on, only when no source path holds a term and the
   terms list and extra pattern file were both read; otherwise it prints just the git
   subcommand and its exit code, and you run that step by hand to see why.

   The masking covers paths inside the source tree, never the paths you hand the run. The
   ledger, the source checkout, the terms list, the extra pattern file, an explicit
   `--ci-yml` and the cut directory (`--out` when building, `--tree` when checking) all print
   whole: the first line of output names the ledger and the source, check lines name the terms
   list, the extra pattern file and `--ci-yml`, and refusals and failures name the source and
   the cut directory. Keep every one of them out of any directory whose name holds a term, or
   the run log carries the term in a path nothing masks.

   Without the list the check prints `not configured: <where it looked>` and passes: it is an
   extra guard, not a requirement. That holds only where the check knew where to look: on the
   explicit route (`--ledger`/`--source`/`--host-ref`, no `--project`) with no
   `--sensitive-terms`, the check is `cannot measure`. `--project` finds the list through the
   environment config's data root. If the project or environment config will not load, the check is
   `cannot measure`; pass `--sensitive-terms <path>` to name the list directly. Naming a path
   that does not exist prints `not configured: <path> does not exist`, never a silent pass, so
   read that line to confirm the list you meant was the one read. Never write a real sensitive
   term into a tracked file, test or comment, including a test fixture.

   A sanitize **pattern** alternative that must never ship (an install-specific name with the
   lookarounds that keep legitimate words around it alive, a host alias, an address range)
   goes in the second local-only file,
   `<data_root>/projects/<project>/publish/sanitize-extra-pattern.txt`, beside the list. Where
   the list holds literal terms, this file holds regular-expression alternatives that join the
   sanitize pattern itself: one per line, valid for both Python `re` and `grep -P`, surrounding
   whitespace stripped, blank lines and whole-line `#` comments skipped, the lines OR-ed. The
   cut's `sanitize-terms` check and the pillar residue check (precondition 4) union it with
   `ci.yml`'s pattern. Mirror the same alternatives, joined with `|`, into the working repo's
   `SANITIZE_EXTRA_PATTERN` repository secret so the PR-time gate applies them too (precondition
   1). Without the file the check line says `extra pattern not configured: <where it looked>`
   and passes on the generic shapes (on the explicit route with no `--sanitize-extra-pattern`
   it is `cannot measure` instead); a file that cannot be read, or a line that does not
   compile, is `cannot measure`. `--project` finds it through the data root, and
   `--sanitize-extra-pattern <path>` names it directly. No output ever prints the pattern or a
   matched term, and a path the extra pattern matches is masked like a path holding a term.

   Enter `SANITIZE_EXTRA_PATTERN` as one line with no surrounding whitespace: the CI step
   appends it as written, without the per-line stripping the file gets, so a trailing space
   narrows the last alternative and a newline breaks every scan (which fails closed). The gate
   never echoes the secret. GitHub lists a step's env at the top of its log and masks a
   repository secret's value there as `***`, and in any other line that prints it whole; the
   mask matches the whole value only, so one alternative printed on its own would not be
   masked. A secret cannot be read back once saved, so the local file stays the copy you edit:
   re-enter the whole joined line after every change to it. Set it on the working repo only,
   never on a public copy.
4. **The `wiki/` pillar is clean by its own check, not by the sanitize gate.** The gate greps
   the *checkout*, and the pillar is not in it: pages live under the data root, which is
   outside the repository and is not a git repository at all. Extending CI to walk it is not
   a deferred task, it is unbuildable, and `ci.yml` says so at the sanitize job. What covers
   the pillar is a ratchet that runs where the pillar is:

   ```sh
   python -m agentflow.wiki residue --project <project>
   ```

   It reports two classes and never sums them: the terms the sanitize pattern already covers
   (`ci.yml`'s pattern parsed at run time, so there is no second copy to drift, unioned with
   the local extra pattern file of precondition 3, which `--project` finds; the report names
   the sources it applied), and the bare personal name, which that pattern deliberately does
   not contain. An extra pattern file that cannot be used is a `2`. Exit `0` means the ratchet
   held, `1` means the corpus got dirtier than its recorded baseline, and `2` means the check
   **could not measure** (a config that will not load is a `2` too), never that the pillar is
   clean. Do not read a non-zero as "dirty"
   without looking at which one it was.

   **Green here is not the same as an empty pillar.** The baseline
   (`<data_root>/projects/<project>/wiki/residue-baseline.json`) records the pages that were
   already dirty when the ratchet shipped (71 of 507 on 2026-09-22), and the check passes with
   them present, on purpose: a gate that was red on day one is a gate somebody turns off. The
   decision on those pages is made (answered 2026-09-23): **the pages stay, the ratchet stays
   the control, and the bare personal name does not join the sanitize pattern.** Pillar pages
   are not edited to clear this; they are create-only by standing decision. A violation names
   each hit as `<path>:<line> <class>`, never the matched text, and the remedy is to supersede
   the page with a clean one that names people by role and move the dirty page into
   `pages/archived/`; an archived page leaves the ratchet. Read the printed per-class counts
   before publishing anything derived from the pillar.
5. **Every CI suite passes on `<rev>` itself**, not on the install at `suite_root`, which can
   lag the commit being cut. `.github/workflows/ci.yml` is the list: the `_shared`, install
   and graph pytest suites, the inventory and orchestrator `node --test` suites and the
   harness job, each on Ubuntu and Windows, plus ruff and the gate jobs on Ubuntu. Prove it
   one of two ways:

   - **Cite the `workflow_dispatch` run** that precondition 1 already starts (`gh workflow run
     ci.yml --ref master`). It runs the full matrix (the PR-only provenance gate skips). It
     counts only when its head sha is `<rev>` and its conclusion is `success`:

     ```sh
     gh run list --workflow ci.yml --event workflow_dispatch --limit 5 --json databaseId,headSha,conclusion
     ```

   - **Or check `<rev>` out** into a scratch directory and run every job's commands from
     there (`CONTRIBUTING.md`, *Running the suite*, gives the main ones; `ci.yml` is the
     whole set). That covers only the operating system you run it on, so prefer the
     dispatch run.

6. **`python -m agentflow.doctor --project <project>` reports the split you expect**:
   `publish` OK, naming `working <repo> -> publish <repo>` with two *different* repos. This one
   cannot pass before the rename, because a `publish_repo` equal to the working repo is a
   doctor FAIL. It is checked at step (4), not in step (0).
7. **Somebody has read the diff.** Not skimmed the file list: read it. Publishing is the one
   moment in this workflow where a mistake is not revertible, because you cannot un-publish a
   commit somebody has already fetched.

If any precondition fails: fix it in the working repo, land the fix normally, and start from
step (0) again.

---

## The checklist

Do the steps in order. **Nothing outward happens before step (0) is green.** Each step names
the check that proves it happened; a step is done when its proof holds, not when its command
returned.

Placeholders used throughout: `<owner>/<name>` is the share repo (the name the working repo
has today), `<owner>/<name>-private` is the working repo after its rename, `<noreply>` is a
GitHub noreply address (`<id>+<user>@users.noreply.github.com` or
`<user>@users.noreply.github.com`; the cut refuses any other address), `<project>` is the
agentflow project, and `<rev>` is the working-repo commit you intend to share.

### (0) Gate first, before anything outward

Key rotation gates the cut unless the ledger records a waiver. Until every row of
`<data_root>/projects/<project>/status/key-rotation-ledger.md` carries a `rotated_on` date,
the cut's `ledger-dated` check fails. Every visible table with a `rotated_on` column is
counted, not only the first, so an undated row in a later table (an addendum section) fails the
same; with more than one table the line reads `<d> of <n> rows dated across <k> rotated_on
tables`, and each table needs its own separator row and at least one row or the check is
`cannot measure`. The header cell matches in any case (`Rotated_On` counts). A `rotated_on`
table directly under another table with no blank line between them, inside a blockquote, or
with two `rotated_on` columns is `cannot measure`. The one way past it
without rotating is a `## Waiver`
section in that ledger, recording the human's decision not to rotate, one bullet per field:

```markdown
## Waiver

- waived_by: <name>
- waived_on: <YYYY-MM-DD>
- scope: publish
- reason: <why the cut ships without rotating>
- evidence: <what was checked to support that>
```

A complete waiver passes the check as `waived by <name> on <date>: <n> of <m> rows undated`. A
field missing, empty or given twice, a `waived_on` that is not an ISO date, or a `scope` other
than `publish` makes it `cannot measure` (exit `2`), naming the field. The ledger is read as it
renders: lines inside a fenced code block (```` ``` ```` or `~~~`) or an HTML comment are
skipped, so a filled-in waiver example pasted in either waives nothing. A comment block starts
only on a line that begins with `<!--` and runs to the line holding `-->`; elsewhere `<!--`
hides text only when `-->` closes it on the same line, and inside an inline code span it is
code. So a stray `<!--` in a table cell is plain text and hides no rows. A fence or comment
block left unclosed is `cannot measure`, naming the line it opened on: the gate will not guess
what it was meant to hide. One known gap: a comment block indented four or more spaces (inside a
nested list item, say) is read as literal text, though it renders hidden. The same goes for the `rotated_on` table: an example table in a
fence or a comment is never read as the ledger, and a ledger whose only such table is hidden
has no table (`cannot measure`). A heading that is nearly but not exactly `## Waiver`
(`## waiver`, `## Waiver (publish)`) waives nothing either; the `FAIL` line names its line
number. A waiver never fills `rotated_on`: those cells stay blank until a rotation actually
happens. There is still no flag. The ledger lives in the data root and never ships, and only
the human's word puts a waiver there. Rotating, and deciding to waive it, are human tasks and
are not in this runbook; do one of them first.

Run preconditions 1, 3, 4, 5 and 7 on `<rev>`. Then build a dry-run cut into an empty local
directory. This writes only to that directory; it adds no remote and pushes nothing:

```sh
python -m agentflow.publish cut --rev <rev> --out <dry dir> --author-email <noreply> --project <project>
```

`--project` is required (or all three of `--ledger`, `--source` and `--host-ref`): `cut` and
`check` refuse to fall back to the session's default project. The explicit route has no data
root to find the local-only files of precondition 3 in, so it also needs
`--sensitive-terms <path>` and `--sanitize-extra-pattern <path>`; without them `sensitive-terms`
and `sanitize-terms` print `cannot measure` naming the flag, and the run exits `2`. The first
line of output names the project, ledger path, source checkout and owner the run resolved;
check that they are the ones you meant before reading anything below it.

**Proof:** it exits `0`, and every check line reads `PASS` or `REPORT`. If it exits `1`, a
hard check failed: fix master (or, for `ledger-dated`, finish rotation or record a waiver)
and start again in a fresh empty directory (delete the dry-run directory, or pass a new
`--out`: the cut never overwrites); nothing has been renamed yet. If it exits `2`, the gate
could not measure; that is never a pass. A `REFUSED` line on stderr exits `2` too: the cut
refused its inputs, the line says why, and nothing was checked. An environment config error
(say, a notifier env var that is not set) leaves the owner and source alone, since they come
from the project file, but blanks every path under the data root and the personal name:
`ledger-dated`, `sanitize-terms` and `sensitive-terms` each print `cannot measure` naming the
config error, and `person-name` prints `not measured`. Fix the config error the line names,
or pass the check's own path instead (`--ledger <path>`, `--sanitize-extra-pattern <path>`,
`--sensitive-terms <path>`, `--person-name <name>`). Either way the cut has already been
built, so re-run into a fresh empty directory, as for exit `1`. **Record the
printed source sha and tree id.** Steps (5), (9) and (10) compare against them.

**What each check line means.** A hard check prints `PASS` or `FAIL`; `FAIL <name> cannot
measure - <why>` makes the exit `2`. A finding names paths, line numbers and counts, never
the matched text.

| Line | What it checks | On `FAIL` |
|---|---|---|
| `cut-shape` | The cut directory is still what the cut built: one parentless commit by `agentflow` with a noreply address, `refs/heads/master` only, no hook file (git's inert `*.sample` files are allowed), and a repo config (local and worktree scope, includes followed) holding only allow-listed keys: `core.repositoryformatversion`, `core.filemode`, `core.bare`, `core.logallrefupdates`, `core.symlinks`, `core.ignorecase`, `core.precomposeunicode`, `core.autocrlf`, `core.safecrlf`, `extensions.objectformat` and `extensions.refstorage`, each pinned to a value `git init` or the cut writes (`core.bare`, `core.logallrefupdates`, `core.autocrlf` and `core.safecrlf` only `false`; booleans read case-insensitively, as git reads them). Any other key fails it, named with its scope; a remote, URL rewrite, include or `core.hooksPath` gets a message of its own; an allow-listed key at another value fails it, named with its value and the values allowed. An `objects/info/alternates` file, even an empty one, fails it too: the cut never writes one, and it lends the cut another repository's objects. | Delete the directory and build the cut again; never repair it by hand. |
| `ledger-dated` | Every row of every visible `rotated_on` table in the key-rotation ledger (not only the first table; header matched in any case) has a real `rotated_on` date, or the ledger records a complete `## Waiver` (step (0)). A malformed waiver, a blockquoted table, a table with no blank line above it under another, or a second `rotated_on` column is `cannot measure`. | Finish key rotation, or record a waiver. No flag skips it. |
| `session-urls` | No file and not the cut's commit message carries an assistant session URL or a session trailer. | Remove it in the working repo, land that, cut again. |
| `sanitize-terms` | No text file matches the cut's `ci.yml` sanitize pattern unioned with the local extra pattern file (precondition 3), including `skills/_vendored/`, which the CI job skips. The line names the sources it applied: `ci.yml pattern + extra pattern from <path>`, or `ci.yml pattern; extra pattern not configured: <where it looked>`. An extra file that cannot be read, a line that does not compile, a data root that cannot be resolved, or the explicit route without `--sanitize-extra-pattern` or `--project`, is `cannot measure` (pass `--sanitize-extra-pattern <path>`). | Replace the literal in the working repo. |
| `sensitive-terms` | No file, file path or the cut's commit message holds a term from the local-only list (precondition 3), ignoring case, with `_` and punctuation as word boundaries. A hit prints `path:line (list line N)`. A path holding a term prints as `<cut path #K>` here and in every other check's lines. No list prints `not configured: <where it looked>` and passes; a list that cannot be read, a data root that cannot be resolved, or the explicit route without `--sensitive-terms` or `--project`, is `cannot measure` (pass `--sensitive-terms <path>`). | Replace the term in the working repo, land that, cut again. Look it up by its list line number. |
| `key-shapes` | No file matches any credential shape `publish.py` lists in `KEY_SHAPES`. | Remove the file or value, and rotate the key: it is in the working history too. |
| `excluded-paths` | No path in the cut, and none the cut dropped from the source rev, matches `.env`, `.env.*`, `*.pem` or `.worktrees/`, in any segment and any case. The cut leaves such a path out of its tree and still fails, **by design**: a conventional `.env.example` fails too. Only `cut` measures the dropped half, since only it knows what it dropped; `check` measures the paths in the cut and labels the dropped half `dropped paths not measured: only cut knows them`, so a `check` PASS never vouches for a source rev that `cut` failed. | Remove or rename the path on the source branch (`env.example` passes), land that, cut again. |
| `tracker-links` | No line links an issue or pull request under the working repo's owner; those links reach the private tracker, or the share itself after the rename. | Replace the link with a plain `gh#N` reference. |
| `runbook-present` | `docs/runbooks/publish.md` is in the cut. | Restore it in the working repo. |

The `REPORT` lines never change the exit code. Read each before step (1):

| Line | What it reports | What to do |
|---|---|---|
| `sha-citations` | Each `path:line` that names a commit sha resolving in the source checkout, each sha listed once per line. The share does not carry those commits, so to a share reader each is a dead reference. | Nothing is required. Rewrite, in the working repo, any a share reader needs to follow. |
| `tracker-refs` | The count of plain `gh#N` references. They name issues in the working repo's tracker, which a share reader cannot open. | Nothing is required; they are labels, not links. |
| `install-evidence` | How many files besides `docs/INSTALL.md` differ from the tree the Linux install was last verified on. | Follow *Linux install evidence* below. |
| `person-name` | Each `path:line` of a text file that carries the bare personal name (precondition 3), never the name. The `Copyright (c)` line of `LICENSE` is exempt. `not measured: <why>` when the name does not resolve. | Nothing is required; read each hit before you publish. |

**Linux install evidence.** The share should carry an install that was verified on the tree
being shared. `docs/INSTALL.md`'s Last-verified line cites the **tree id** `<T1>` the Linux
fresh-clone drill last passed on, and `harness/fresh-clone-install-drill/README.md`, section
*The cut-time rule*, says how to compare it with the cut. The dry-run cut's
`install-evidence` REPORT line does the first half: it counts the files besides
`docs/INSTALL.md` that differ between `<T1>` and the cut tree. Never require the ids to be
equal: the Last-verified record itself is the difference between them. Then, from the working
checkout:

```sh
git -C <working checkout> diff --stat <T1> <tree>
git -C <working checkout> diff -U0 <T1> <tree> -- docs/INSTALL.md
```

The first must list only `docs/INSTALL.md` (the REPORT line reads `0 file(s)`), and every
hunk of the second must fall inside the Last-verified block. If anything else differs, re-run
the Linux drill on the dry-run tree, as that rule says, before step (1). If the REPORT line
says `not measured`, the comparison did not run, and the line says why. If `--source` does not
exist, is not a directory, is not a git repository, or sits inside one without being its top
level, point `--source` at the working checkout's top level and check again. If
it is a valid checkout that lacks `<T1>`, fetch `<T1>` into it, or re-run the drill.

### (1) Quiesce

Stop every loop that can push to the working repo, and restart them only after step (7).
A loop that pushes a branch between the rename and the repoint can publish it.

**Proof:** the fleet board shows every loop idle.

### (2) Rename, then repoint EVERY clone on every machine

First publish only: skip if `publish_repo` is already set.

A repo name is unique per owner: you cannot create `<owner>/<name>` while `<owner>/<name>` is
the private working repo, so the working repo is renamed first.

**The old links do not keep working.** The host redirects the old URL only until a new repo
takes the old name, which step (3) does. From that moment, every old-URL link and every clone
still pointing at the old URL reaches the **new** repo. A clone that was not repointed and then
pushes a branch publishes that branch. So every clone, on every machine, is repointed before
step (3), not just the parent clone (worktrees share their parent's remotes, but other clones
and other machines do not).

First list every clone, **by path**. Do not list them with `git remote -v`: it prints any
inline token in a clone URL. A read that pipes the URL straight into a count is safe:

```sh
# On each machine: every clone whose origin names the working repo prints its path.
for d in <search root>/*/ <search root>/*/*/; do
  if [ -d "$d/.git" ] && [ "$(git -C "$d" remote get-url origin | grep -c '<owner>/<name>')" = 1 ]; then
    echo "$d"
  fi
done
```

Then, **HUMAN ONLY** (outward: renames the repo on the host):

```sh
# HUMAN ONLY. Renames the working repo on the host.
gh repo rename <name>-private --repo <owner>/<name>
```

Repoint each listed clone, update `canonical_repo.host_ref` in
`~/.agentflow/config/projects/<project>.md` to `<owner>/<name>-private`, and confirm:

```sh
git -C <clone> remote set-url origin <working repo url>
git -C <clone> remote get-url origin | grep -c '<name>-private'
python -m agentflow.doctor --project <project>
```

**Proof:** the `grep -c` prints `1` on **every** listed clone (never run an unpiped
`get-url`: it prints the URL), and doctor resolves the checkout.

### (3) Create the new repo

First publish only: skip if `publish_repo` is already set.

Its visibility is the owner's decision. The suite's first share is private, plus a
collaborator invite at step (10).

```sh
# HUMAN ONLY. Creates the share repo on the host. Empty: no README, license or gitignore.
gh repo create <owner>/<name> --private
```

**Proof:**

```sh
gh repo view <owner>/<name> --json visibility
```

### (4) Set `publish_repo` to the new repo

First publish only: skip if `publish_repo` is already set.

Set `publish_repo` in `~/.agentflow/config/projects/<project>.md` to `<owner>/<name>`.
Precondition 6 is checked here.

**Proof:** `python -m agentflow.doctor --project <project>` reports `publish` OK, naming
`working <owner>/<name>-private -> publish <owner>/<name>`.

### (5) Build the real cut from the recorded sha

Use the source sha recorded at step (0), not a branch name that may have moved:

```sh
python -m agentflow.publish cut --rev <recorded source sha> --out <cut dir> --author-email <noreply> --project <project>
```

The author and committer are the fixed name `agentflow` and the given noreply address; no git
config identity reaches the commit.

**Proof:** it exits `0`, and its printed tree id equals step (0)'s. If it does not, stop:
something changed between the two builds.

### (6) Re-check the cut directory right before the push

Step (5) checked the cut as it was built. Nothing re-verifies it between that build and the
push, so check the directory you are about to push from, immediately before pushing:

```sh
python -m agentflow.publish check --tree <cut dir> --project <project>
git -C <cut dir> rev-parse 'HEAD^{tree}'
```

Its `cut-shape` check refuses anything that is not the cut `build_cut` made: exactly one
parentless commit, authored and committed by `agentflow` with a noreply address, on `master`
alone, with no hook file (git's inert `*.sample` files aside), and with only the allow-listed
keys in step (0)'s table, at their pinned values, in its own config, worktree config and
anything they include, and with no `objects/info/alternates` file. A
second commit would be pushed with the first. The config is an allow-list because the keys
that can redirect step (7)'s push by URL or run code during it (a URL rewrite, an include, a
hook path, a credential helper, a proxy, an ssh command, an fsmonitor) are too many to list
the other way round, and a `pre-push` hook file runs during the push too. A stray remote, ref
or identity would not ride out on the one-ref push, but it means the directory was touched
after the build, so it is no longer the cut step (0) described.

**Proof:** the check exits `0`, every check line reads `PASS` or `REPORT`, and the `rev-parse`
prints the tree id recorded at step (0). `cut-shape` alone cannot see a commit amended as
`agentflow`; the tree id can, here rather than at step (10), after the push. If either does
not hold, delete the cut directory and go back to step (5); never repair a cut by hand.

### (7) Push the one commit, from the cut directory, by URL

No `publish` remote is ever added to the working checkout that agents run in. The push goes
from the cut directory, by URL, and names exactly one ref.

```sh
# HUMAN ONLY. Pushes the single cut commit to the share repo.
git -C <cut dir> push <new repo url> HEAD:refs/heads/master
```

- **Never `--force`.** If the push is refused, stop and work out why; a force-push to a shared
  repo destroys history other people may hold.
- **Never `--mirror`, never `--tags`.** Push one ref, by name.

**Proof:**

```sh
git ls-remote <new repo url> refs/heads/master
```

prints the cut's commit id. Restart the loops stopped at step (1).

### (8) Recreate branch protection (gh#354)

The required status-check contexts live only in the host's settings, nowhere in the tree.
Read them from the renamed working repo with a read-only GET:

```sh
gh api repos/<owner>/<name>-private/branches/master/protection/required_status_checks --jq .contexts
```

Apply the same list to `<owner>/<name>` in the host's branch-protection settings (**HUMAN
ONLY**: a settings change). Do not copy the list into the tree; recording it there is
gh#354's own fix.

**Proof:** the same GET against `repos/<owner>/<name>/...` returns the same list.

### (9) Tag the source sha, not the cut

Tag the sha recorded at step (0), in the **working** repo, as `published/<date>`. Keep the
tag in the working repo; never push it to the share. The cut shares no history with the
working repo, so the tag is a record of what was shared, not a range marker for a next push.

```sh
# HUMAN ONLY. Tags the recorded source sha in the working checkout. Never pushed to the share.
git -C <working checkout> tag -a published/<date> <recorded source sha> -m "cut shared to <owner>/<name>"
```

**Proof:**

```sh
git -C <working checkout> rev-parse 'published/<date>^{commit}'
git ls-remote --tags <new repo url> 'published/*'
```

The first prints the recorded sha. The second prints nothing. (A version tag may exist on the
share; a `published/*` tag must not.)

### (10) Grant access, then verify from outside

Invite the collaborator in the host's settings (**HUMAN ONLY**). Then, from a machine with no
prior access to the working repo, clone the share fresh:

```sh
git clone <new repo url> <scratch clone>
git -C <scratch clone> rev-list --count HEAD
git -C <scratch clone> log -1 --format=%ae
git -C <scratch clone> rev-parse 'HEAD^{tree}'
```

**Proof:** the count prints `1`, the author is the noreply address, and the tree id equals
step (0)'s.

`canonical_repo` is still where all work lands tomorrow. Nothing else changes.
