# Fresh-clone install drill

```
python harness/fresh-clone-install-drill/fresh_clone_install_drill.py \
    [--out <dir>] [--clone-from <path-or-url>] [--ref <ref>] [--bare-python <path>]
```

Exits 0 iff every limb passed, or skipped with a stated reason. Writes
`<out>/fresh-clone-install-drill.md`; `--out` defaults to a temp directory, so a bare run
never touches a live config home, data root or Claude root. It clones over the network and
takes several minutes — it is an on-demand drill, not a CI step (see **In CI** below).

> **`--clone-from` executes code from whatever you point it at.** The drill clones that target
> and then runs `install/provision.py` and `harness/check.py` **from that clone**, on this
> machine, in your user account, outside any container. The throwaway config home / data root /
> Claude root exist so the run cannot disturb your live agentflow install — they are not a
> security boundary against hostile code. The default is this repository. Anything else should
> be a repository whose installer you would have run by hand anyway.

> **Limb 2 is `gh#186`'s regression guard.** It was that bug's *witness* — it required the
> PyYAML-less run to **die** — and it was **inverted, not deleted**, when the fix landed, so
> the fix is checked rather than asserted. It now requires the real install, run on an
> interpreter **without** PyYAML, to hand itself over to `<clone>/.venv` at the end of step 4
> and reach `11. done.` with exit 0. Both the handover line and the last step are required: a
> run that never handed over is a run on an interpreter that quietly had PyYAML, and accepting
> it would be the old blind spot in mirror image — worse, because it looks like success. A
> death carrying `ModuleNotFoundError: No module named 'yaml'` is reported as a **regression of
> `gh#186` by name**; any other failure is refused rather than filed under it.
>
> **The issue's second finding, split out as `gh#322`, is fixed for the case it recorded.**
> Step 2 refuses to pre-create a scraped `data_root` or `worktree_root` that is relative, or
> whose only existing ancestor is the drive root, and leaves the loud failure to the step-10
> resolver. This drill writes a well-formed config and never exercises a malformed one; the
> gh#322 tests in `install/test_provision_fresh_machine.py` cover that case offline. Every
> record says so.

## Why it exists

`harness/README.md` publishes a table of what runs "out of the box" on a fresh clone, and
`docs/INSTALL.md` publishes a clone-then-provision recipe. Both are the repository's most
public promise, and both are the first thing a new contributor tests.

The evidence behind them was a recorded PASS against beta-test HEAD **`3c959cd`** — a commit
that **no longer exists**. The private working repository's master was re-cut on 2026-08-28
with a new root (`271611b`, which no public cut contains), and the beta-test history went
with it. So the citation does not resolve, and there is no way to
tell from the record whether the claim ever held on the tree that shipped. That is worse than
having no evidence: an unresolvable citation reads as verification.

This drill replaces it with a run whose sha an auditor can check out.

## What it does

1. Clones the repo into a throwaway tree at a named ref and **resolves the tree id and the
   sha**, which the record carries — a record that cannot name something resolvable
   reproduces the exact failure that made this drill necessary. The **tree id**
   (`git rev-parse HEAD^{tree}`) is the one to cite: it hashes the files alone, so a
   single-commit share cut of the same files carries it, while the sha names a commit that
   no such cut contains. An unresolvable tree fails the drill, as an unresolvable sha does.
2. Builds a throwaway config home, data root, Claude root and settings file, writes the
   minimal config the strict resolver accepts, and exports `AGENTFLOW_CONFIG` into the child
   environment while removing `AGENTFLOW_PROJECT`. **The `--config-home` flag alone does not
   sandbox the installer**: steps 10 and 11 call `agentflow.config.load_environment()`, which
   resolves its config home from `$AGENTFLOW_CONFIG` or `~/.agentflow/config` and takes no
   argument, so the flag steers provision's bookkeeping while the resolver walks off and
   scaffolds into the machine's real data root. `AGENTFLOW_PROJECT` is removed for a
   different reason: the closing doctor check reads it, and an ambient project name from the
   operator's shell produces a spurious FAIL about a project the fresh config home has never
   heard of.
3. **Limb 1** — `provision.py --dry-run` exits 0 **and writes nothing**, proved by comparing
   the sandbox data root before and after rather than by trusting the banner.
4. **Limb 2** — the `gh#186` regression guard described above: the PyYAML-less run hands
   over to the suite venv and completes.
5. **Limb 3** — the real run on the interpreter step 4 builds (`<clone>/.venv`) exits 0 and
   prints `11. done.`. Exit 0 without that step is the shape of a run that stopped early
   and reported success anyway, so both are required. **11 rather than the highest step
   number**: step 12 (the watchdog registration) is opt-in (`--wake-watch`, off by default)
   and platform-gated, so it prints a SKIP with no completion line on a default run and off
   Windows; 11 is the last step every run completes.
6. **Limb 4** — the artifacts the unconditional steps claim to produce are looked for on disk: the
   `.agentflow-deploy.json` manifest, at least one deployed skill and one deployed command,
   a `SessionStart` hook registered in the throwaway settings file, all eight data-subtree
   directories, and a non-empty seeded `status/registry.md`.
7. **Limb 5** — `harness/check.py --self-test` from the clone, on the venv interpreter.
8. **Limb 6** — `harness/check.py --only C,F,G` after copying `harness.config.example.json`
   **verbatim, placeholders unreplaced**, because that is what the harness README tells a
   reader to do and the fresh-clone rows must hold before any editing. The full-suite numbers
   are captured afterwards as **context only** and are never gated on.
9. Writes a record naming the timestamp, the tree id, the sha, the OS (the distribution's
   `PRETTY_NAME` from os-release where there is one), both interpreters with their `X.Y.Z`,
   every limb's verdict *and its stated blind spot*, and the golden-script numbers.

Every PASS line states what that limb does **not** cover, and the renderer prints headline
and caveat together, always. A reader who cites a bare "PASS" from this drill has cited
something the limb never checked.

## What it deliberately does not prove

- **One OS, the one it ran on.** The record names the platform. CI covers Windows and Linux
  for the test suite; this drill's result is single-platform and is cited as such.
- **The record-dependent scenario groups.** Groups A, B, D and E2 read `records/`, which is
  deliberately never committed (`harness/run-scenario.md`), so a fresh clone fails them by
  design. Their reds are recorded as context, not as a regression.
- **The human interview in `install/SKILL.md`.** The drill *writes* a config by hand and
  hands it to `provision.py`. The questions, the one-way secret write and the judgement about
  what a machine actually has are untested here, and a green run says nothing about them.
- **The autodoc Node runtime step.** `--no-autodoc` skips step 6, keeping the drill runnable
  without a Node toolchain and leaving npm-side breakage invisible to it.
- **A malformed config.** `gh#186`'s second finding, split out as `gh#322`, is fixed: step 2
  no longer pre-creates a scraped `data_root` or `worktree_root` that is relative or has no
  existing ancestor below the drive root. The drill writes a well-formed config, so it never
  exercises that refusal; the gh#322 tests in `install/test_provision_fresh_machine.py` do.

## Running it on Linux, in a clean container

`docs/INSTALL.md` is written for a stranger on Ubuntu 24.04 running the system `python3`,
which on that release is 3.12. This is the run that checks that path, against a dry-run cut
at `<cut>` (the directory `python -m agentflow.publish cut --out <cut>` writes):

```
MSYS_NO_PATHCONV=1 docker run --rm -e DEBIAN_FRONTEND=noninteractive \
    -v <cut>:/src:ro ubuntu:24.04 bash -euc '
  apt-get update && apt-get install -y sudo
  echo "Defaults env_keep += DEBIAN_FRONTEND" > /etc/sudoers.d/keep-debian-frontend
  sudo apt update && sudo apt install -y git python3-venv python-is-python3 tmux gh
  useradd -m drill && cp -a /src /tmp/cut && chown -R drill:drill /tmp/cut
  sudo -H -u drill git clone /tmp/cut /tmp/cut-src
  sudo -H -u drill python3 /tmp/cut-src/harness/fresh-clone-install-drill/fresh_clone_install_drill.py \
      --clone-from /tmp/cut --ref master --bare-python /usr/bin/python3 --out /tmp/drill-out
'
```

Why each piece is there:

- **`--rm`** keeps finished containers from piling up on the host.
  **`DEBIAN_FRONTEND=noninteractive`** stops the tzdata package that `python3` pulls in from
  waiting at a debconf prompt nobody can answer in a non-TTY run.
- **`apt-get install -y sudo` first**, because a stock `ubuntu:24.04` has no `sudo`. The next
  line is `docs/INSTALL.md` step 2's install line **byte for byte**, run as root, where `sudo`
  is a no-op for privilege, so the walkthrough's own package list is what gets exercised.
- **The `env_keep` line**, because `sudo` is not a no-op for the environment: Ubuntu's
  `env_reset` default drops `DEBIAN_FRONTEND` even when root runs it, and the tzdata prompt
  would come back on exactly the line that installs `python3-venv`. The package line stays
  byte for byte; the sudoers drop-in carries the variable through the reset instead.
- **`useradd`, `cp -a`, `chown`**: git refuses a local clone source owned by another user
  (Ubuntu's git enforces the dubious-ownership check on it), and a root-owned copy would still
  trip it, so the copy is re-owned by the user that runs the drill. It lives under `/tmp`, not
  `/home/`, so the record's `Cloned from:` line does not trip the sanitize gate's home-path
  pattern when the record is pasted into a pull request.
- **The drill runs as `drill`, from a checkout of the cut.** `git clone /tmp/cut /tmp/cut-src`
  gives it one. If the cut directory has no work tree, `git -C /tmp/cut checkout master` and
  run the drill from `/tmp/cut` instead.
- **`--bare-python /usr/bin/python3`** is the system interpreter, which has no PyYAML on a
  stock image, so limb 2 runs the handover rather than skipping. A SKIP there means the run
  did not test the fresh-machine path.
- **`MSYS_NO_PATHCONV=1`** is only needed from Git Bash on Windows, which otherwise rewrites
  the `-v` mount's `/src` into a Windows path. It is harmless elsewhere.

The record is printed to stdout at the end of a green run. Its `Resolved tree:` line must equal
the tree id the cut printed, and its `OS:` line names the distribution.

## The cut-time rule

The Last-verified line in `docs/INSTALL.md` cites a **tree id** (`T1`, below), never a
commit: a single-commit cut carries no working-history commit, but it carries the same tree
for the same files. A file cannot contain the hash of the tree that holds it, so the published
tree differs from `T1` in exactly one place, the Last-verified block itself. That is expected.

At the real cut, after key rotation and **before the push**, run both of these in the working
checkout, where `T1` resolves:

```
git diff --stat <T1> <real cut tree>
git diff -U0 <T1> <real cut tree> -- docs/INSTALL.md
```

If the first lists anything besides `docs/INSTALL.md`, or any hunk of the second falls outside
the Last-verified block, something merged since the drill has changed what the cut ships. Then:

1. Re-run the container command above against a fresh dry-run cut.
2. Replace the Last-verified paragraph with the new tree id.
3. Re-cut.

That converges, because the second delta is again only that block. **If a limb fails, do not
cut.**

## In CI

`skills/_shared/tests/test_fresh_clone_install_drill.py` is the companion. It does **not**
clone or provision — that is networked and takes minutes, and a suite that does it stops
being run. Instead it injects a fake `Runner` and a pre-built sandbox and breaks each limb's
assertion in turn — a dry run that mutates the data root, a PyYAML-less run that dies the
`gh#186` death, one that dies some other way, one that exits 0 having never handed over, a
real run that exits 0 without reaching step 11, a deleted deploy manifest, a red self-test,
a `--only C,F,G` that exits 0 with no assertions — and requires the drill to FAIL each time.
It also re-derives the handover line out of `install/provision.py`'s own source and requires
the drill's pattern to match it, so rewording that print cannot silently leave limb 2 green
on a run that never handed over.

Each mutation is paired with a **control**: the same fake, unmutated, must pass. A mutation
that leaves the drill failing for some other reason has not proved the limb detects anything,
and a mutation that does not remove the property under test proves nothing at all.
