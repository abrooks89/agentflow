# Installing agentflow

Plain-English setup for a fresh Linux machine — Ubuntu 24.04 or newer. No prior terminal
experience assumed.

If you already know your way around a shell and want the mechanics — the typed config
schema, `provision.py`'s twelve steps, the deployment policy — read
[`install/SKILL.md`](../install/SKILL.md) instead. This page is the friendly version of
the same install.

The one thing to know up front: **you never hand-write a config file.** Step 7 has Claude
interview you and write the settings itself. That is the intended path, not a shortcut.

---

## Before you start

You need five things:

1. **A Linux machine** you can leave running, with a screen and keyboard attached, running
   **Ubuntu 24.04 or newer**. It should be a box where nothing else depends on the system
   Python — the installer makes agentflow's Python environment the default for your shell.
   The version floor is not a preference: the installer refuses to run on any Python older
   than 3.11, which it checks before it does anything else.
2. **An internet connection.**
3. **A Claude subscription**, so you can sign in to Claude Code.
4. **A GitHub account**, with the project you want worked on already pushed to it. Step 5
   stores that account's credentials on this machine, so read the next paragraph before you
   decide which account to use.
5. **Claude Code, and the session tools the loops call.** Step 3 installs Claude Code. The
   installer then writes into three of its features: skills and slash commands placed in its
   config folder, the optional `PreToolUse` auto-allow hooks, and a `SessionStart` hook
   (`provision.py` steps 7, 8 and 9). The loops also call three tools that come from the Claude
   Code session itself, which nothing here installs: `ScheduleWakeup`, required by every
   self-waking loop; `Workflow`, required for wave execution and for the two autodoc skills,
   which have no single-agent fallback; and `CronCreate`,
   recommended, not required. How to check your session has them, and what to do if one is
   missing, is in
   [Before you launch a loop](#before-you-launch-a-loop-what-is-not-installed-by-anything-above),
   after the install.

**If you plan to run the builder loops, the machine itself is part of the setup.** Step 10
turns off Claude's permission prompts for those loops and explains why in full when you get
there. The condition attached to that comes up here rather than there, because step 5 puts
real credentials on the box five steps earlier:

> That trade is only sane on a machine you could wipe and rebuild without losing anything, and
> whose stored passwords don't reach beyond the project it's working on. If that's not this
> machine, skip the flag, skip `/lead-dev`, and drive `/dev-manager` yourself — answering its
> questions is a real way to use this, not a lesser one.

**On Windows:** agentflow runs there too — CI runs every test job on `windows-latest` as
well as `ubuntu-latest`, on every pull request and on pushes to `master` or `main`. This
walkthrough is Linux only, though. On Windows, follow
[`install/SKILL.md`](../install/SKILL.md) and use the PowerShell forms it gives; the config's
`shell` setting is `posix` or `powershell`, and there is no Git-Bash variant.

**Last verified fresh-clone install:** tree `16357f1355a3d477523e91a7cead70ae3c5be809`,
2026-10-01, Ubuntu 24.04.5 LTS (`ubuntu:24.04` container), system Python 3.12.3 — by
`harness/fresh-clone-install-drill/`, which clones this repo into a throwaway tree and
provisions it against a throwaway config home, data root and Claude root. The run followed that
directory's README section *Running it on Linux, in a clean container*: step 2's install line
byte for byte, then the drill on the system `python3`. All six of its limbs passed. This record
covers Linux only; Windows is covered by CI, whose test jobs also run on `windows-latest`.

The record names a tree rather than a commit. A tree id is a hash of the files, so it survives
the single-commit cut this repository is published as, and a commit sha does not. The published
tree differs from that id in this block only — these two paragraphs and the note under them — because a
file cannot carry the hash of the tree that holds it. So `git rev-parse HEAD^{tree}` on your
clone will not print the id above, and that is expected.

> **The run went through the fresh-machine path of `gh#186`.** On an interpreter without
> PyYAML — which is what a genuinely fresh machine has — the install used to die at step 5
> with `ModuleNotFoundError: No module named 'yaml'`, because step 4 installs PyYAML into the
> suite venv rather than into the interpreter running the script, and five later steps import the
> resolver in that parent interpreter. Step 4 now ends by checking whether the interpreter running
> it can import the dependencies, and hands the rest of the run over to the venv it has just built
> when it cannot — one line of output naming the handover, then every step from 5 on continues in
> the venv and the install exits 0. The recorded run started on a stock `python3` with no PyYAML
> and completed through that handover. Nothing changes for an interpreter that already has
> PyYAML: no child is spawned. (The run used `--no-autodoc`, so step 6 — the Node runtime for
> autodoc — was skipped rather than exercised.)
>
> The **second** finding in that issue, split out as `gh#322`, is fixed for the case it
> recorded: step 2 no longer pre-creates a scraped `data_root` or `worktree_root` that is
> relative, or whose only existing ancestor is the drive root, and leaves the loud failure to
> the step-10 resolver. It still creates a well-formed path under an existing directory before
> the resolver runs, by design, because a fresh `data_root` does not exist yet.

Set aside about twenty minutes, most of it waiting on downloads.

---

## 1. Open the terminal

Press `Ctrl` + `Alt` + `T`. A window with a text prompt appears. Everything below gets
pasted into it, one block at a time — paste, press Enter, wait for the scrolling to stop
before moving on.

Keep this window open for the whole install. Step 8 is the only step that asks you to close
it.

## 2. Install the basic tools

```
sudo apt update && sudo apt install -y git python3-venv python-is-python3 tmux gh
```

It will ask for your password. Type it and press Enter — the characters won't appear on
screen, which is normal.

What these are: `git` fetches code, `python3-venv` builds the Python environment agentflow
runs in, `python-is-python3` makes plain `python` mean `python3` (several later steps and
the fleet's own skills call bare `python`), `tmux` keeps sessions alive after you close the
terminal, and `gh` talks to GitHub.

No Node here on purpose. agentflow's pipeline doesn't use it — the installer only checks for
`node` and `npm` when you turn on the optional autodoc code-wiki, and skips them entirely
otherwise. If you enable autodoc later, install Node (with npm) then, set `autodoc_root` to an
existing directory (create it first; a path that does not exist fails config loading), and
re-run `python install/provision.py --autodoc` from the checkout **before** `/autodoc-init` —
adding `--no-auto-allow` if you declined the auto-allow hooks, since a re-run without it writes
them back (§13). An install with autodoc off skipped provision step 6 (`npm ci`, then the
`autodoc_graph` install into the suite venv), and setting the field afterwards does not run it
for you.
(Step 3 names Node once more, as a
fallback route for installing Claude Code — that would be Claude Code's dependency, not
agentflow's.)

## 3. Install Claude Code

```
curl -fsSL https://claude.ai/install.sh | bash
```

That command is Claude Code's own native installer, published in Claude Code's installation
docs — it is not part of this repository, and nothing here can check it for you. The only
thing this page can vouch for is the result: a working `claude` on your PATH.

If the `claude` command isn't found right after this, a freshly opened terminal window
normally has it; carry on in that one. If the installer itself fails, Claude Code's docs
also publish an npm install, which needs Node — the one place in this walkthrough that
does:

```
sudo apt install -y nodejs npm
sudo npm install -g @anthropic-ai/claude-code
```

Both commands come from Claude Code's own documentation rather than from agentflow, so if
neither works, that documentation is the place to look — not this page.

Before going further, check the two things everything below depends on:

```
python3 --version
claude --version
```

`python3` must print **3.11 or newer** — the installer's first step hard-fails below that
with `Python 3.11+ required`. `claude` should print a version. If either command isn't
found, fix that before step 4; nothing later works without them.

## 4. Sign in to Claude

```
claude
```

It prints a link. Open it in a browser, approve the sign-in, and come back to the
terminal. Then type `/exit` and press Enter.

One thing to check before moving on. The installer writes into a settings file at
`~/.claude/settings.local.json`, and the session-hook step always reads it, as does the
auto-allow-hooks step unless you decline those hooks. The installer checks for that file in
its very first step and stops there, before it has written anything, if the file doesn't
exist; the error names the path and points you back to this section. It must exist whether
or not you keep the auto-allow hooks. It may already be there after the sign-in above and it may
not, so don't guess either way — run this, which checks and fills the gap only if there is
one:

```
mkdir -p ~/.claude
[ -f ~/.claude/settings.local.json ] || echo '{}' > ~/.claude/settings.local.json
```

The second line writes an empty `{}` only if the file isn't already there, so it can't
overwrite settings you already have.

## 5. Sign in to GitHub

```
gh auth login
```

Four questions, in this order. Answer: **GitHub.com** → **HTTPS** → **Yes** (authenticate
Git with your credentials) → **Login with a web browser**. It shows a short code; paste
that into the page it opens.

This is the step that leaves credentials on the machine — answering **Yes** to the third
question also hands them to `git`, which is how the fleet pushes. They are exactly the
"stored passwords" the box in *Before you start* is about.

If you would rather use a personal access token than a browser login, it is two commands,
not one. The second is what teaches `git` to use the token; without it the clone in step 6
and the fleet's pushes both fail:

```
gh auth login --with-token < token.txt
gh auth setup-git
```

### Supported git hosts

**`gh` is the only git-host CLI agentflow is verified against, and GitHub is the only host it is
verified on.** The config schema is wider than the implementation, which is why this is worth
saying out loud rather than leaving for you to discover: `git_host.kind` accepts `github`,
`gitea`, `gitlab` and `local`, and `git_host.cli` accepts `gh`, `glab` and `none`, so a GitLab
or Gitea profile loads cleanly and reports nothing wrong. (`tea` is not an accepted value at
all — config validation rejects it when the profile loads.)

Where it stops is the issue tracker. `agentflow.issue_intake` builds gh's argv shape — `<cli>
issue list --repo R --state open --json number,title,labels,body,url` — which `glab` does not
accept, so `_require_verified_tracker` refuses it at the first tracker call, **before spawning a
process**. `work_source.kind: gitea` is refused the same way even with `gh`, since gh can only
reach GitHub. Per-CLI argv builders are tracked as
`gh#9`.

**That guard covers less of the fleet than you would expect, and the gap is the part worth
knowing.** Three groups of host calls, and only the first reads your `git_host.cli` and stops:

1. **The shared intake helper** — bug selection and `triaged` labelling. Reads the CLI,
   short-circuits to nothing on `none`, refuses `glab` loudly.
2. **Skill prose that interpolates the CLI** — steward's `<git_host.cli> issue create`,
   lead-dev's pre-create `<git_host.cli> issue comment`. No guard at all: under `cli: none` these
   render the literal command `none issue create`.
3. **A hardcoded `gh`** — PR create, merge, view and list in the orchestrator and lead-dev, the
   review comment orchestrator step 7b posts on the cited issue, and `/report`'s merged-PR
   window. These never consult `git_host.cli`. They are gated on `gh` being installed and
   authenticated and on the origin URL looking like GitHub; where it does not, the orchestrator
   degrades to push-only rather than erroring.

So `cli: none` silences group 1 and nothing else, and `cli: glab` is refused in group 1, broken
in group 2, and ignored in group 3.

One more edge, because knowing where a guard stops is the point of having one: **a Gitea or
GitLab repository reached with `cli: gh` clears every check and then fails against the wrong
host.** The guard reads the CLI name, and nothing downstream can redirect `gh` — `host_ref` is a
bare `owner/repo` and no config field carries a hostname — so the call goes to **github.com**,
which reports a repository that does not exist there. Expect a GitHub error naming a repo you
never meant to reach; your own host's logs will show nothing.

Everything that goes through plain `git` — clone, worktrees, branches, commits, pushes — works
on any host.

## 6. Download agentflow and your project

Replace the second address with your own repository's.

```
git clone https://github.com/abrooks89/agentflow.git ~/agentflow
mkdir -p ~/projects && git clone https://github.com/YOU/YOUR-PROJECT.git ~/projects/your-project
```

## 7. Let Claude install it

```
cd ~/agentflow && claude
```

That folder is where the installer lives, and only for this one session. It is **not** this
machine's launch directory: every session after the install, fleet lanes included, is started
from your **home directory**, because that is where the installer writes the settings those
sessions load. Step 10 is where that starts to matter, and says why.

Then type this and press Enter:

> Read install/SKILL.md and install agentflow on this machine. Interview me for anything
> you need, and don't assume — ask.

It asks a short series of questions in plain English: your name, where your project
lives, which branch is the main one, how it should check its own work, whether you want
push notifications, whether you keep a notes vault it should mirror documents into, and
whether to install the auto-allow hooks.
Answer them and it writes the config, builds the Python environment,
installs the hooks, and creates the folders the system works out of.

Six of those questions are worth thinking about before you're asked:

- **"How should it verify its work?"** — **"I don't know" is a fine answer here.** Say it,
  and the installer will read your project — what it's written in, whether it has screens,
  what test tools it already has — and suggest an approach with its reasons shown, which
  you can accept or overrule. If you'd rather answer yourself, answer with what would have
  caught your *last real bug*. If your project has a screen
  a person uses, the honest answer is usually "drive it in a browser," with the test suite
  as a safety net underneath rather than the gate itself — a passing suite proves the
  functions work, not that anything calls them. You can also point this at a skill file
  that does both.
- **"Where can it reach a running copy of your project?"** — a browser check needs
  somewhere to browse. Give it the address of a test environment, and describe how your
  changes get deployed there. If you don't have one yet, that's worth setting up before you
  start rather than after.
- **"Where should project files live?"** — one folder holds everything for every project.
  Pick somewhere you'll remember; `~/agentflow-data` is a fine default.
- **"Do you want push notifications?"** — these go out through ntfy, and the topic name is
  the only thing standing between them and a stranger: on a public ntfy server a topic is
  just a name, and anyone who knows or guesses it can read everything posted to it. The
  notices name your epics and their progress. So use a long random topic name, or run your
  own ntfy server. What you must *not* do is pick a topic that requires a login to publish
  to — agentflow posts with no credentials at all (its sender sets a title, a priority, tags
  and a content type, and no authorization header), so every notification would be rejected.
  Answering "no" is also a real answer; the config has a `none` setting for exactly that.
- **"Do you keep a notes vault?"** — the one question on this list that catches people out,
  because it sounds like it is about something you have not set up yet. It is asked once and
  never again. If you keep an Obsidian-style vault, agentflow will **mirror design specs and
  plans into it** — the `backlog` and `project` skills hand it specs, the `solution` skill
  hands it plans — so those
  files get version history from the vault rather than from a repo, since nothing under the data
  folder is in one. Nothing else mirrors yet. **`autodoc/` is not mirrored**, so a built corpus is
  untracked: its pages can be re-rendered from the graph store inside it
  (`<autodoc_root>/.source/graph.db`), but deleting the tree loses the store with it
  ([`/autodoc-init` Step 7](../skills/autodoc-init/SKILL.md#step-7--commit-the-autodoc-retired)).
  `wiki/` pages are not mirrored either. Separately, if `vault.save_skill` is configured, lead-dev
  and the orchestrator file one knowledge-base note per completed epic into the vault through that
  skill; that note is new writing, not a copy of `wiki/`. The installer offers
  what it found by looking for a `.obsidian/` directory, which Obsidian itself writes, so the
  hits are real vaults rather than folders named hopefully; check the path before accepting one,
  because a stale backup copy looks identical to that check and mirroring into a dead tree is
  worse than not mirroring.

  **Saying no costs you nothing that stops anything working.** Every place that would mirror a
  document does nothing instead; no step fails and no loop degrades. What you give up is exactly
  the mirror — file history for your specs and plans — and those per-epic notes, nothing else. Say so
  plainly if the answer is no, because there is a third state here that matters: the installer
  writes `vault: null` to record that you were **asked and declined**, which is what stops
  `doctor` offering you vault candidates on every future run. A missing key means "nobody ever
  asked", and that is the state that makes a diagnostic nag.
- **"Install the auto-allow hooks?"** — these let Claude run a long list of commands without
  asking you first, in every session started from your home directory, including the one you
  drive yourself. *What the install changes on your machine* below lists exactly what they
  allow; read it before answering. The default is to install them, which is what an
  unattended fleet needs. Say no if this machine is not one you could wipe, and, in the default
  permission mode, Claude asks before those commands. The answer is not saved anywhere: it becomes the installer's
  `--no-auto-allow` flag for this run only.

### What the install changes on your machine

Worth reading before you run it, because four of these reach outside agentflow — one into
every shell on the box and three into every Claude Code session on it. A fifth, which runs on
a schedule whether or not any session is open, happens only if you opt in with `--wake-watch`:

- **A Python environment inside the repo** — `~/agentflow/.venv` — with agentflow's own
  package installed into it.
- **A line in your shell profile** (`~/.bashrc` or `~/.zshrc`) that activates that
  environment in every new shell. This *displaces* whatever `python` the box had before,
  which is why *Before you start* asks for a machine where nothing else depends on the
  system Python. If you said yes to push notifications, two `export` lines for the ntfy
  address and topic go beside it — step 8 says why a terminal needs them.
- **agentflow's skills and slash commands placed into `~/.claude`** — that is what makes
  `/dev-manager` and the rest resolve in a fresh session, in any project. On Linux they are
  **symlinks** back into `~/agentflow`, so the deployed skills track your checkout live. On
  Windows the skill *directories* are **junctions**, which track the checkout live the same way
  and need no admin rights; the `commands/*.md` files and the hook script remain copies, because
  a junction can only point at a directory, and those stay stale until you re-run provision
  (gh#296). A manifest at `~/.claude/.agentflow-deploy.json` records one entry per
  deployed skill *directory*, per `commands/*.md` file, **and one hook script** —
  `skills/_shared/scripts/plan-lint.sh`, which lands at the stable path
  `~/.claude/scripts/plan-lint.sh` so the host copy is never unmanaged (gh#110). Provision
  registers no hook for it: nothing runs it unless you add one yourself. Each entry carries
  source, target, and a single sha256 over that whole entry, not a per-file listing. Three kinds of thing, not two: the
  script is the one people forget, and a deployed file nothing claims is a file nothing updates.
- **A `SessionStart` hook** in `~/.claude/settings.local.json`, which prints agentflow's
  resolved config into every new Claude Code session **started from your home directory** —
  `settings.local.json` is the *project* settings file for a session rooted there, not the
  user-global `settings.json` beside it, and nothing searches upward for it. It also injects the resolved project's own repo
  `CLAUDE.md`, if it has one: sessions start in the launch directory rather than in your
  repository, so those rules can no longer be found by looking upward from the working
  directory, and this is what keeps a lane bound to the rules of the project it resolved
  instead of the folder it was opened in.
- **`PreToolUse` auto-allow hooks** in that same file. These are the ones to read twice.
  They pre-approve the `Edit`, `Write` and `Agent` tools outright, plus 44 Bash command
  patterns listed in `skills/orchestrator/bootstrap.py` — among them `Bash(git *)`,
  `Bash(curl -s *)`, `Bash(scp *)`, `Bash(chmod *)`, `Bash(mv *)` and `Bash(sed *)`. Note
  that `Bash(git *)` matches *every* git command, `git push` and `git reset --hard`
  included. `rm` is not on the list — but do not read that as a boundary: `Bash(python *)`, `Bash(node *)` and `Bash(npm run *)` are, and each of those runs arbitrary code. **And these hooks are not scoped to agentflow**:
  `~/.claude/settings.local.json` is the project settings file for sessions rooted at your
  home directory, so they apply to every Claude Code session you start from there — every
  fleet session, and any other work you happen to begin in that directory — whether
  agentflow is involved or not. A session started elsewhere does not load them.

  **You can decline them.** Say no when the interview asks about the auto-allow hooks, or run
  the provisioner with `--no-auto-allow`; the rest of the install is unchanged, the
  `SessionStart` hook included. The flag only stops them being written: it removes nothing an
  earlier install wrote, and section 13 says how to take existing ones out by hand. Whether a
  lane launched with `--dangerously-skip-permissions` needs these hooks at all has not been
  verified, so do not read declining them as either safe or unsafe for such a lane.
- **Optionally, a scheduled missed-wake watchdog — off unless you ask for it.** A plain
  install registers nothing on the OS scheduler: step 12 prints `SKIPPED` and names the flag.
  Run the provisioner with `--wake-watch` to opt in. Opted in on Windows, the installer
  registers one Task Scheduler entry per project, named
  `agentflow-wake-watch-<project>`, firing hourly. It runs

  ```
  python -m agentflow.wake_watch --project <name>
  ```

  through a small wrapper at `~/.agentflow/watchdog/wake-watch-<project>.cmd`, appending both
  output streams to `wake-watch-<project>.log` beside it. **On macOS and Linux the installer
  does not register it even when you opt in** — a crontab is your file and may hold your own
  jobs, so a provisioner that rewrote it could silently drop them. An opted-in install prints
  the exact `crontab -e` line instead; it is the same command. Either way, re-running
  provision with the flag converges on the same single entry rather than adding a second.

  **What opting in buys you.** Everything else in agentflow runs inside a Claude Code
  session. The watchdog runs outside them, so it can still report a loop that has stopped
  waking when no session is running to notice. Without it, a stalled lane is caught by the
  steward's in-session sweep or by you.

  **Existing registrations are left alone.** An install run without `--wake-watch` deletes
  no scheduled task, including one an earlier install registered when the watchdog was on by
  default. Removing it is your call: section 13, item 6 (`schtasks /Delete`).

  **It reports and nothing else.** It reads the board, prints what it finds, and pushes an
  ntfy notification when a loop is past the time it said it would wake. It never starts,
  restarts or stops a loop, never merges and never writes to the board. It is agentflow's
  **one** background job by explicit rule — the suite had none before, and the budget for
  them is one. Section 13 says how to remove it.
- **The folders the fleet works out of**, under the data folder you picked above.

## 8. Restart the terminal

**If you said yes to push notifications, check one thing first.** Open `~/.bashrc` (or
`~/.zshrc`) and find the line the installer added that activates agentflow's environment — it
ends `agentflow/.venv/bin/activate`. Beside it there should be two more:

```
export NTFY_URL='https://your-ntfy-server'
export NTFY_TOPIC='your-long-random-topic'
```

with your real server and topic. The installer also saved those two values in
`~/.claude/settings.local.json`, but that copy only reaches Claude Code sessions started from
your home directory — never a terminal. Without the exports, step 9's diagnostic fails on its
very first line, `named env var 'NTFY_URL' absent from os.environ`, and skips every check after
it, on an install that is otherwise correct. If they are missing, add them yourself. If your
profile is kept in a dotfiles repo or synced by OneDrive, put the two lines in an untracked file
and source that from the profile instead — the topic is what keeps strangers off your
notifications. If you said no to notifications, there is nothing to add.

Then close the window and open a new one. This makes the Python environment the installer
built your shell's default, and loads those two values.

## 9. Check that it worked

**Run the diagnostic first.** One command checks the whole resolved surface — config, paths,
project profile, checkout, registry, the document globs, autodoc and vault — and it is built to
run when nothing else does, so a broken config gets *reported* rather than raised:

```
python -m agentflow.doctor --project <name>
```

A healthy install prints one line per check, in a fixed order, and ends with a verdict. This is
the shape of a brand-new install that answered no to the vault question — it is worth reading
once, because five of these lines read `info` and all five are correct:

```
ok   environment           resolves; data_root=/home/you/agentflow-data
ok   path:data_root        /home/you/agentflow-data
ok   path:lock_dir         /tmp
ok   path:suite_root       /home/you/agentflow
ok   path:worktree_root    /home/you/worktrees
ok   project:your-project  resolves; repo=you/your-project
ok   checkout              /home/you/projects/your-project
ok   base_branch           main (refs/heads/main)
info publish               working repo you/your-project; no publish target configured
ok   registry              /home/you/agentflow-data/projects/your-project/status/registry.md
info glob:spec             /home/you/agentflow-data/projects/your-project/design/specs exists but holds nothing yet
info glob:plan             /home/you/agentflow-data/projects/your-project/design/plans exists but holds nothing yet
ok   glob:status           1 match(es)
ok   vocabulary            no epic declares a queue_class
info autodoc               no autodoc root configured or derived
info vault                 no vault - recorded as declined (`vault: null`)

resolved surface healthy
```

- **`info publish`** — you have no public repo to publish to. That is the normal state and the
  correct encoding of it; see [`docs/runbooks/publish.md`](runbooks/publish.md) before you add one.
- **`info autodoc`** — nothing scaffolds the `autodoc/` pillar, and running `/autodoc-init` will
  not create it either: it exits when no root resolves. `/autodoc-init`, `/autodoc-update` and this
  line all ask the resolver — `resolve_autodoc_root` tries the project's own `autodoc_root`, then the
  machine-level one, then `<data_root>/projects/<name>/autodoc/` if that directory already exists.
  On a fresh install this line is what *correct* looks like, and the pillar stays absent until
  someone builds a corpus, in this order: install Node LTS (with npm) if it is absent, create
  the directory and set the machine-level field to it (it must already exist), re-run
  `python install/provision.py --autodoc` so provision step 6 installs the autodoc runtimes it
  skipped (add `--no-auto-allow` if you declined the auto-allow hooks, or the re-run writes them
  back — §13), then run `/autodoc-init`. Skip the re-run and this
  line turns to `warn`, naming the missing `autodoc_graph` package or `node` and that same
  remedy.
- **`info vault`** — you have no vault, or you declined one in step 7. Nothing is degraded. If you
  have not been asked yet, this line lists the vaults it found on disk and offers them; once you
  have declined, it stops offering.

**On a brand-new install, before you have written a single spec, `glob:spec` and `glob:plan`
read `info` — `<dir> exists but holds nothing yet`.** The installer scaffolds `design/specs/`
and `design/plans/` empty, and an existing empty directory is a layout that agrees with the
globs. Those lines only `fail` when the directory itself is missing, which is the case they
exist to catch: a layout that has moved out from under the glob defaults. When the whole
`<data_root>/projects/<name>/` subtree is missing, one `fail glob` line replaces all three and
says the project subtree was never scaffolded - re-run `python install/provision.py`.

The same scaffold seeds the project's `wiki/` pillar. When the pillar has no pages yet, that
includes an empty residue baseline (`wiki/residue-baseline.json`, dated at provision time), so the
steward's residue check (`python -m agentflow.wiki residue`) can measure from its first wake. A
pillar that already has pages gets no baseline: the check exits 2 until you read its report and
record one with `--update-baseline`.

There are four statuses. `ok` and `info` are both healthy — **`info` is not a soft failure**, it
is "this is switched off, deliberately". `warn` means a value resolved but *not as you wrote it*
(a dial that was clamped, say), which is reported loudly and still exits 0. Only `fail` exits 1,
and the run ends with `N FAILING:` and each one restated. Two closing lines are healthy:
`resolved surface healthy`, and `resolved surface healthy, but NOT everywhere as configured — see
the warning(s) above`. A run that selected no project (two or more profiles and no `--project`)
ends `environment and paths only: ...` instead, because it examined no checkout, registry or
glob; pass `--project <name>` to check one.

**`doctor` is the install-health command; `python harness/check.py --self-test`, run from the suite root, is the deeper behaviour check** — see [`harness/README.md`](../harness/README.md).

Then check the two things the loops depend on directly. The install registers a hook that prints
agentflow's resolved config into every new Claude
Code session. Rather than hunting for that in the session UI, run the hook yourself — same
code, visible output:

```
cd ~/agentflow && python hooks/sessionstart-inject-config.py
```

It should print a block that starts `=== agentflow resolved config ===`. If the project's
repository carries a `CLAUDE.md`, a second block follows with that file's contents — expected,
not noise: it is how repo rules reach a session that starts outside the repo. Look at the
`[project]` line in it. If you configured exactly one project, it names that project — which
is what you want. If it says `[project] (no project)`, the hook still succeeded and still
exits 0: it only picks a project automatically when `AGENTFLOW_PROJECT` is set or when
exactly one project file exists under `~/.agentflow/config/projects/`. With several
configured and no `AGENTFLOW_PROJECT`, it can't choose. Exporting the variable in your shell
does *not* fix that for a Claude Code session — Claude Code's settings `env` block overrides
the process environment — so put the project on the slash command instead
(`/<loop> --project <name>`), per §12 below and *Add another project later* in
[`install/SKILL.md`](../install/SKILL.md).

If instead it prints `agentflow SessionStart config injection FAILED` followed by a
`ConfigError`, read what the error names — it tells you the exact field and how to fix it —
or paste it back to Claude and ask it to fix the config.

Then confirm the write path the loops actually use resolves, replacing `<name>` with your
project's name:

```
python -m agentflow.registry validate --project <name>
```

That should print `registry valid` and exit quietly. A `ModuleNotFoundError` here means the
shell isn't using the environment the installer built — the profile line from step 7 didn't
take, or you're in a terminal opened before step 8.

## 10. Start it up

Start `tmux` **from the launch directory**, not from your project — new windows open in the
folder the session was created in, so doing the `cd` first is what makes every later window
land in the right place.

**Which folder is the launch directory?** The one the installer wrote the settings for — your
home directory. Step 7 put the `SessionStart` hook and, unless you declined them, the auto-allow matchers in
`~/.claude/settings.local.json`, which is the *project* settings file for a session rooted at
your home directory and for no other; nothing searches upward for it. Launch anywhere else and
those hooks are simply not loaded. That is a fact about where the installer writes, not a
preference: `install/provision.py` resolves the settings path as `Path.home() / ".claude" /
"settings.local.json"`, and `skills/orchestrator/bootstrap.py` does the same. So the launch
directory is your home directory — which, incidentally, is also why the fleet's own rule is
that a lane sits in **no repository at all** and every git command it runs names its checkout
explicitly.

```
cd ~
tmux new -s af
claude
```

Then type this, replacing the name with your project's — the working directory no longer
says which project you mean, so the loop's own command does:

```
/dev-manager --project your-project
```

**This is the one you talk to.** Tell it what you want built; it answers the other loops'
questions and approves work.

**Your first spec.** On a fresh install the board is empty — there is nothing to approve yet,
and `dev-manager` says so and recommends the `refine` skill. Work starts as a written spec, and
two skills the installer deployed make one; type either in that same session:

- **`/refine <your idea>`** — the usual one. It asks you questions one at a time, proposes a
  design, and when you approve it writes a full spec.
- **`/backlog <your idea>`** — for an idea you want parked rather than worked out now. It writes a
  short placeholder spec, which still has to go through `refine` before anything builds it.

Either way the file lands in `~/agentflow-data/projects/your-project/design/specs/` (under
whatever data folder you chose in step 7), and step 9's `glob:spec` line turns `ok`. A new spec
is not approved work: `dev-manager` grooms it with you — priority, domain, the files it touches — and
only once you promote it does the builder below pick it up.

**Why the launch directory and not your project's folder.** Claude Code keys two things to
the folder a session is opened in: the `CLAUDE.md` chain it reads by looking upward, and the
history it has accumulated there. A lane started inside your repository — or inside one of
the per-epic worktrees a build creates — therefore comes up bound to *that* folder's
instructions, with none of the fleet's own accumulated context, and every relative `git` and
test command it runs resolves against a tree nobody chose. And the step-7 hooks do not save
you either — the opposite. They live in the settings file for sessions rooted at your home
directory, so a session started anywhere else never loads them: no resolved-config block, no
auto-allow rules, no injected project `CLAUDE.md`. The lane comes up looking like an ordinary
Claude Code session that happens to know nothing, which is why step 10 fixes the launch
directory rather than suggesting one.

To add the builder: press `Ctrl`+`b` then `c` for a new window, check with `pwd` that you are
still in the launch directory, then start it like this — note the flag, which the builder
needs to work on its own and `dev-manager` doesn't require:

```
claude --dangerously-skip-permissions
```

and type `/lead-dev --project your-project`. The first time you use that flag it shows a
warning and asks you to accept; answer it once and it won't ask again.

**What that flag means, plainly.** Normally Claude asks permission before running a command
that could change things. The builder works while you're asleep, so there's nobody to ask —
without the flag it stops at the first question and waits forever, which looks exactly like
working. With it, an unsupervised program can run anything on that machine.

That trade is only sane on a machine you could wipe and rebuild without losing anything, and
whose stored passwords don't reach beyond the project it's working on. If that's not this
machine, skip the flag, skip `/lead-dev`, and drive `/dev-manager` yourself — answering its
questions is a real way to use this, not a lesser one.

**One caveat, now that step 7 has run.** The paragraph above describes Claude's stock
ask-first behaviour, and on this box that is no longer what "no flag" means unless you
declined the auto-allow hooks: the hooks the installer wrote (see *What the install changes on
your machine*) apply to every session started in the launch directory, `/dev-manager` included,
and to no session started anywhere else. Leaving the flag off still
keeps a checkpoint in the lane you're watching — it is just a narrower one than it sounds. To
keep the stock ask-first behaviour, install with `--no-auto-allow` (or answer no at the
interview), or remove the hooks as section 13 describes.

**The rest of the fleet.** Five more loops exist, three of them optional. Each is launched the
same way as `/lead-dev` — new window with `Ctrl`+`b` then `c`, a `pwd` that still reads the
launch directory, then `claude --dangerously-skip-permissions`, then the slash command with
`--project your-project` on it. Every one of them is a lane, so every one of them fails the
same silent way if its window sits anywhere else. The full order, which is the one
[`install/SKILL.md`](../install/SKILL.md) gives under *Launch the fleet*:

1. `/dev-manager` — done above, and the one that doesn't take the flag.
2. `/lead-dev` — done above.
3. `/jr-dev-1`, then `/jr-dev-2`, then `/jr-dev-3` — **only as far as the extra-builder count
   you set during setup.** Step 9's config block spells it out on the `[active builders]` line:
   turned off, skip all three; `count: 1` launches `/jr-dev-1` only; `count: 2` launches the
   first two; `count: 3` launches all three. A junior launched past that count refuses to build
   and stops. (`jr-dev-3` is an interim lane — a hand-copy of `jr-dev-2` that a later epic
   replaces with a count-driven set — so do not be surprised when it goes away.)
4. `/auditor`.
5. `/steward`.

If you decided against the flag earlier, skip all of these too: they are the other
unattended loops and they need it for the same reason `/lead-dev` does. Each loop lives only
as long as its window — nothing restarts itself after a reboot, so launch them again the
same way.

### Every place a launch instruction can be read from

You will eventually change how the fleet is launched — a new flag, a different directory, a
loop that takes different arguments — and then you have to find every place that still tells
somebody the old way. **This is that list, and it is deliberately a list of *readers* rather
than of filenames.**

The distinction is not pedantry; it is why the last sweep of this rule missed. That sweep
enumerated the files it knew about, found every one of them correct, and left the machine's
most-loaded instruction file still prescribing the launch that had just been retired — because
nobody had put that file on the list, and a list cannot notice what is absent from it. So ask
the question instead: **where can an instruction about launching be read from?**

The axis that matters is **tracked or untracked.** The tracked side is one kind and it is
genuinely closed, because a repository-wide search reaches all of it. The untracked side is
where every failure of this rule has happened, so it gets the rest of the list — named by the
*place* a reader lives, never by the filenames that happen to be in it today.

1. **Documentation in this repository.** Anything a repository-wide search reaches: this page,
   [`install/SKILL.md`](../install/SKILL.md), [`docs/REGISTRY.md`](REGISTRY.md),
   [`README.md`](../README.md), the loop skills under `skills/` that restate the rule for their
   own lane, and the repo's own root `CLAUDE.md`, which step 7's hook injects into every lane
   that resolves this project. This is the kind that goes least wrong — and the kind a sweep is
   most tempted to stop at, because it is the only one that shows up in a search.
2. **The host config directory — all of it.** `~/.agentflow/config/`, not just one glob inside
   it. `environment.md` carries launch-directory prose of its own and is read in full at config
   resolution by every lane on the machine; `projects/*.md` is one profile per project, read in
   full by every lane on that project; and leftover `settings-*.json` files from a launch form
   that has since been retired sit in the same folder, still passable to `--settings` by anyone
   who finds them. **Grep the directory, not a pattern inside it.** A file added here is
   invisible to every repository search and reviewed by nobody.
3. **The launch directory's own `CLAUDE.md` chain.** Claude Code reads the `CLAUDE.md` chain of
   the folder a session is opened in, and every fleet session is opened in the launch directory —
   so this file is read by every lane, every wake, before anything else. **This repository does
   not track it**, and it is the one that bit: the last sweep left it prescribing the launch that
   had just been retired.
4. **The per-directory memory store the launch directory accumulates** —
   `~/.claude/projects/<launch directory>/memory/`, written by sessions and loaded into every
   later one started there. It is a reader for the same reason kind 3 is, with one extra edge: a
   retired instruction does not merely *survive* here, it was *recorded* here as true at the time,
   and it stays true-looking until something rewrites it. Nothing expires it and no repository
   search reaches it.
5. **What the installer says out loud at the end.** [`install/SKILL.md`](../install/SKILL.md)'s
   *Launch the fleet* is prose that Claude *says* to you while it finishes the install, rather
   than a page you go and read. It is the sentence a new operator actually acts on, and it is the
   easiest one to sweep past, because reviewing documents does not feel like it should leave a
   speaking instruction behind.

Three traps worth knowing before you check any of them:

- **A grep for the retired wording counts hits; it cannot read them.** After a correct sweep the
  old launch form usually still appears — inside the sentence that now forbids it. A hit is a
  place to read, not a finding. The inverse costs more: a *prohibition* in an untracked reader
  reads, to a counter, exactly like the drift you came to remove.
- **When a tracked page and an untracked reader disagree, neither one automatically wins.** The
  temptation is to declare the repository canonical and rewrite the machine from it — which is
  wrong in the only case that has actually occurred, where the untracked file was the one that
  had learned something. What is canonical is the **mechanism**: one fixed launch directory per
  machine, and it is the directory whose `settings.local.json` the installer wrote. Check both
  texts against that, fix whichever disagrees with it, and never resolve a conflict by deleting a
  prohibition you have not traced back to what it was written about.
- **These readers are not equally editable.** Kinds 2, 3 and 4 are host files outside this
  repository. A lane sweeping this list *reports* what it finds there; it does not rewrite them.

## 11. Leaving and coming back

Press `Ctrl`+`b` then `d` to detach — everything keeps running without you. To come back:

```
tmux a -t af
```

## 12. If you add a second project

You can point agentflow at more than one project. This is installer work, so it runs where
step 7 did — in the checkout, not the launch directory, because `install/SKILL.md` is a path
relative to the checkout. Start Claude with `cd ~/agentflow && claude` and say:

> Read install/SKILL.md and add a project profile for `<name>`.

That is the sentence [`install/SKILL.md`](../install/SKILL.md) gives for this job specifically,
and it is a smaller job than step 7's: it writes one new `projects/<name>.md` and re-runs the
provisioner's scaffold step, leaving your first project and everything installed alone. (There is
no `/install` command — the file is read by path.) If you declined the auto-allow hooks, that
re-run keeps them off: `install/SKILL.md` counts them first and passes `--no-auto-allow` when
there are none.

Nothing about launching changes shape for the second one — step 10 already had you name the
project on the command, precisely so that adding another needs no new mechanism. Change the
name and open the window from the same launch directory as before, for the same reason: a
lane opened in the second project's checkout gets none of the fleet's settings and dies at
step 0 without leaving a row to say so.

```
cd ~
claude "/dev-manager --project your-other-project"
claude --dangerously-skip-permissions "/lead-dev --project your-other-project"
```

That's the whole trick. The name you type wins over whatever the session started up with, and
the loop remembers it every time it wakes itself up — so a fleet you launch on one project
stays on that project. Each project keeps its own shared file, and the loops work them side by
side.

**One thing that looks like it should work and doesn't:** setting the name in your shell first,
like `AGENTFLOW_PROJECT=other claude`. Claude Code's own settings replace your shell's version
of that setting, so it's thrown away without a word and the session quietly works the *first*
project instead. Nothing errors — it just builds in the wrong place. That is exactly why the
name goes on the command you type, where nothing can overwrite it.

**Do not reach for `--settings` to avoid typing it.** A per-project settings file looks like
the tidy answer and is the one thing on this page that has taken a live fleet down twice. The
reason is the same override you just read about, pointed the other way: a settings file passed
with `--settings` **replaces** `~/.claude/settings.local.json` rather than merging with it. A
file containing only `{"env": {"AGENTFLOW_PROJECT": "<name>"}}` — which is the obvious thing to
write — therefore strips every other variable in
that `"env"` block: every value the interview collected in step 7. The loop comes up without
them, fails while resolving its config, and dies before it can write the row that would have
said so. Nothing on the board goes red; the lane just never appears. Type the name on the
command.

## 13. Stopping, and undoing the install

**To stop a loop**, go to its window and press `Ctrl`+`C`, or close the window. The fleet
runs as Claude Code skills inside the sessions you launched — there is no server to host — so
when a session ends, its loop ends with it. To stop all of them at once, kill the tmux session:

```
tmux kill-session -t af
```

**What doesn't stop with them.** Killing a session doesn't tidy the shared board. Left
behind:

- the loop's row in the project's `status/registry.md`, still showing its last heartbeat;
- any Queue row it had **claimed** — the work item stays claimed by a loop that no longer
  exists, so nothing else picks it up;
- the deploy lock, if it was holding one. Nothing expires it: it is *stealable*, not
  self-releasing, and only another builder actively running `deploy-lock take --break-stale`
  can steal it, only once both the lock's own age and the dead holder's last heartbeat are older
  than 45 minutes. Without the flag that builder is refused `deploy_stale_unbroken` (gh#232), so
  the steal is a decision some lane took rather than a side effect of the next retry. With no builder running, it just sits there;
- any git worktrees a build created. Those are in neither of the two folders you'd look in
  first: not in your project checkout and not in the data folder, but under the machine's
  configured worktree root, one directory per epic at `<worktree_root>/<project>/<epic-slug>`
  (the field itself is in [`skills/_shared/SCHEMA.md`](../skills/_shared/SCHEMA.md)). One
  worktree home per machine, shared by every fleet and partitioned by project;
- **the scheduled missed-wake watchdog**, if you set one up. It is the suite's one daemon
  (single-daemon rule, 2026-09-22) and it is deliberately outside the sessions: an OS scheduler
  entry invoking `python -m agentflow.wake_watch`, which is exactly why it still reports when
  every lane is dead. It is report-only — it never relaunches anything — so leaving it running
  over a stopped fleet costs you nothing but an ntfy push naming rows that are meant to be
  quiet. Stop it in Task Scheduler (or your cron), not by killing a session — the exact
  commands are under *To uninstall* below, item 6.

**To clean that up**, from a shell where `python` is the installed environment:

```
python -m agentflow.registry reap --actor steward --agent lead-dev --project <name> --force --reason "operator stopped the session"
```

`--force --reason` because you killed the session yourself: `reap` otherwise refuses a row that
`read --stale` would not list yet (`reap_not_stale`), and a loop killed a minute ago still has a
fresh heartbeat. You are the human confirming it is gone, and the reason is recorded in the
reap Note.

`reap` idles the dead loop's row, returns its claims, and clears its deploy lock, in one
write. Run it once per loop you killed, changing `--agent`. **The actor is `steward`, not the
loop you killed and not `dev-manager`** — `reap` is one of the four verbs E-0220 moved to the
steward (with `cull`, `stand-down` and `repair`), and the CLI refuses it from any other actor. If a lock is all that needs
releasing and its holder is still alive:

```
python -m agentflow.registry deploy-lock release --actor <loop> --project <name>
```

Leftover worktrees come off with `git worktree remove`, or `git worktree prune` once their
folders are already gone. Give both ends of that explicitly — the checkout to act on and the
worktree to remove — rather than relying on where your shell happens to be, which from the
launch directory is no repository at all:

```
git -C ~/projects/your-project worktree remove <worktree_root>/<project>/<epic-slug>
git -C ~/projects/your-project worktree prune
```

**To keep agentflow but drop the auto-allow hooks.** Edit `~/.claude/settings.local.json` by
hand (it also holds your tokens, so do not paste it anywhere). Under `hooks` → `PreToolUse`:

- Every entry the installer wrote has a `statusMessage` beginning `auto-allow: `. In the `Bash`
  block, remove those **entries** one by one, not the whole block — it may also hold hooks of
  your own.
- For the `Edit`, `Write` and `Agent` blocks, delete the whole block when its only hook's
  `statusMessage` is `auto-allow: Edit` (or `Write`, `Agent`). An emptied block left behind
  stops a later re-run from putting that tool back, which is surprising in both directions.
- An `Edit`, `Write` or `Agent` block that was there before the install is left alone by the
  installer, so it is not agentflow's; leave it.

Re-running the provisioner **without** `--no-auto-allow` puts them back: it re-adds the Bash
entries, and re-adds an `Edit`, `Write` or `Agent` block only if that block is absent. Pass the
flag on every later run.

**To uninstall.** There is no uninstaller. It is six removals, none of them automatic:

1. `~/.claude/.agentflow-deploy.json` lists every skill directory and command file the
   installer placed in `~/.claude`, with the source and target of each — on Linux the targets
   are symlinks. Delete the targets it names, then delete the manifest.
2. In `~/.claude/settings.local.json`, remove the three things the install writes: the
   `SessionStart` entry that runs `hooks/sessionstart-inject-config.py`, the `PreToolUse`
   auto-allow entries, and any values the interview put in the `"env"` block (`install/SKILL.md`
   Step 3 routes `NTFY_URL`, `NTFY_TOPIC`, git-host URLs **and tokens** there, plus `AGENTFLOW_PROJECT`).
   Check the `"env"` block specifically — skipping it leaves credentials on the machine.
   Leave the rest of the file alone — it is yours, not agentflow's.
3. Remove the `source ~/agentflow/.venv/bin/activate` line from `~/.bashrc` (or wherever
   step 7 put it), so your shell goes back to the system Python. If you enabled notifications,
   also remove the two `export NTFY_URL` / `export NTFY_TOPIC` lines beside it (the `$env:`
   lines in `$PROFILE` on Windows), or the file you sourced them from.
4. Delete `~/.agentflow`, which holds the config the interview wrote.
5. `gh auth logout` — step 5 stored a GitHub token on this machine and `gh auth setup-git`
   wired it into git's credential helper. Removing agentflow's files does not remove either.
6. **The scheduled watchdog, if one is registered.** It is outside every session by design,
   so nothing you close or delete above touches it — left alone it keeps firing hourly
   against a board that is no longer there. On Windows, one command per project:

   ```
   schtasks /Delete /TN agentflow-wake-watch-<name> /F
   ```

   `schtasks /Query /TN agentflow-wake-watch-<name>` afterwards should say it cannot find
   it. On macOS or Linux, `crontab -e` and delete the `agentflow.wake_watch` line you added.
   Then remove `~/.agentflow/watchdog/`, which holds the wrapper script and its log —
   though step 4 above takes the whole of `~/.agentflow` with it if you are doing this in
   order.

Your work is in none of those. Specs, plans, status boards and the registry live under the
data folder you chose in step 7 (`~/agentflow-data` by default), and the code lives in your
own repository. Uninstalling touches neither — delete them separately if that's what you
mean to do.

---

## What you actually have now

Seven loops, each launched once in its own window, coordinating through a single shared
file per project:

| loop | what it does | needs you? |
|---|---|---|
| `dev-manager` | your seat — approves work, answers questions, curates the board. **It does not self-wake**: it runs only while you have the window open | yes, this is where you live |
| `steward` | maintenance — stale frontmatter, orphaned locks, drift, silent failures, dead issues; also the registry's janitor, and the loop that pumps between your visits | no |
| `lead-dev` | builds approved epics, also picks up triaged issues; when there's nothing queued it looks for problems worth fixing | no |
| `jr-dev-1`, `jr-dev-2`, `jr-dev-3` | extra builders, up to the count you set during setup | no |
| `auditor` | checks finished work against what it was supposed to do | no |

Work flows **refine** (what and why) → **solution** (how) → **orchestrator** (build, test,
review, merge). No *epic* is built until you promote its spec in `dev-manager` — that
promotion is the approval, and for epics it is the only gate there is: it covers dispatch,
the pull request, and the merge, with nobody reading the PR in between.

Epics are not the only thing `lead-dev` builds, though, and it's worth knowing the other
door. It also takes open issues from your repository's tracker — after every third epic it
builds, by design, and on any wake where no approved epic is ready. Only issues that have
been labelled `triaged` are selectable, so an untriaged issue does not exist for the builder
however old or loud it is. Triage is `dev-manager`'s job (the `steward` may triage a bounded
bug too; the agent that raised an issue never triages it), and **for an issue, the `triaged`
label is the approval** that promotion is for an epic: the fleet is designed to build a triaged
issue and squash-merge its pull request with no spec, no promotion and nobody reading the PR
(README, *Security & blast radius*). Label an issue `triaged` only if you would accept that.

Most fleet questions do reach you properly: `lead-dev` marks every dispatch as
fleet-launched, and the paths that were taught to recognise that marker append a row to the
project's `status/registry.md` under `## Questions` — carrying a mandatory `default:`, the
answer the builder would give itself, which the registry tool refuses to append a question
without — so `dev-manager` collects it on its next pass.

One of those questions stops an issue fix before it is planned. It is worth knowing where,
and not worth relying on. In a fleet-launched run the `project` skill parks, before
dispatching, anything it is handed that is not a spec promoted to
`current_state: approved-for-autonomous`. An issue fix is not one: `lead-dev` hands it over
as a framed goal with no spec behind it, which the skill takes as a fresh topic, and a fresh
topic leads to the interactive brainstorm step that a fleet-launched run must never enter.
That step's fleet branch raises a `## Questions` row whose `default:` is to abort the
dispatch and re-file the epic through `dev-manager`, and parks without planning anything.
It exists to catch a malformed epic dispatch, not to review issue fixes, so treat the
`triaged` label as the approval it is designed to be, not as a request for a second look.

If there's nothing approved to build, `lead-dev` looks for real problems and files them. When
even that comes up dry, it pings you over ntfy (if you set it up) that there is no work, and
keeps checking, waking less often the longer nothing moves. It never stops on an empty queue,
so a loop gone silent is broken, not waiting: its process died or its next wake was never
scheduled. Relaunch it.

## Before you launch a loop: what is not installed by anything above

The installer provisions runtimes. **It cannot provision the three tools a loop takes from the
Claude Code session itself**, and none of the three failures announces itself:

- **`ScheduleWakeup`** — a loop's self-continuation is one call to it. Without it, the loop runs
  once and never wakes again, which looks exactly like a loop that finished its work.
- **`Workflow`** — wave execution dispatches through it, and so does the mapping phase of
  `/autodoc-init` and `/autodoc-update`. Without it, a build dies inside the orchestrator rather
  than at launch, and the autodoc skills stop at their Step 0 guard.
- **`CronCreate`** — each loop schedules a standing 5-hour **rate-limit recovery guard** with it.
  A firing that a quota or rate-limit window (the account's, or one model's) refuses before its
  first tool call never reaches its own
  `ScheduleWakeup`, so the loop cannot recover from inside itself; the guard is the second leg
  that fires later and re-enters the lane. **This one is recommended, not required.** Without it
  a loop still runs, wakes and builds exactly as before — what it loses is unattended recovery,
  so a rate-limit outage ends when a human relaunches rather than on its own. That is why the
  preflight prints `DEGRADED` for it and still exits 0.

These are session tools, not services: `CronCreate`'s jobs live in the session's memory, fire
only while it is idle, and die with it. Nothing here is the suite's one permitted daemon — that
budget is spent on the scheduled `wake_watch` that `provision.py` step 12 registers when you
opt in with `--wake-watch`.

**And on macOS or Linux, if you opted in to the missed-wake watchdog (`--wake-watch`), one
thing the installer deliberately left to you.** `provision.py` step 12 skipped it and printed
the crontab line to add. Without the flag there is nothing to add and you can skip this.
First `mkdir -p ~/.agentflow/watchdog`, since on this platform nothing created that folder
for you and a cron job whose redirect directory is missing never runs the command at all.
Then run `crontab -e` and add one line per project:

```
0 */1 * * * "$HOME/agentflow/.venv/bin/python" -m agentflow.wake_watch --project <name> >> "$HOME/.agentflow/watchdog/wake-watch-<name>.log" 2>&1
```

`$HOME` rather than `~`: cron runs the line through `sh`, which does **not** expand a tilde
inside double quotes, so the quoted `~` form would run a literal path that does not exist.
Use the paths the installer printed rather than these, which are the defaults; `crontab -l`
confirms it landed. The redirect is not optional: `wake_watch` exits 0 whether it
found a stalled lane or a healthy board, so a job wired to the exit code alone reports
permanent health forever. The findings are on stdout.

Ask Claude for its actual tool list, then pass that. The check compares the list *you* supply
against what the seat needs, so a list typed out of this page proves only that you can type:

```
python -m agentflow.preflight --tools "<your session's tool list, comma-separated>" --wave
```

It prints one line per tool it checks, required and recommended (not one per name you pass), and
exits 1 if a **required** one is missing. `ScheduleWakeup` is required by default; `--wave` is what
adds `Workflow`. Keep `--wave` for any seat that builds, since every builder loop passes it at
wake, and for any session that will run `/autodoc-init` or `/autodoc-update`; drop it only for
a seat that runs neither waves nor autodoc, such as `steward`. Add `--no-self-wake` for
a seat that does not self-continue.
`CronCreate` is reported at the recommended tier on every run — `--no-recovery` silences that
line for a seat that will schedule no guard, and it never changes the exit code either way.

**And if it is not there** — this is the part that was missing before, and the only reason to run
a check at all:

- **No `ScheduleWakeup`:** either enable the tool for the session, or drive the loop from an
  outside scheduler that re-invokes the same slash command (with its `--project` argument) on the
  cadence the loop declares in its `eta`. Do not launch and hope; a loop that cannot re-wake is
  a loop that has already stopped.
- **No `Workflow`:** run the work through the ordinary single-agent path rather than a wave.
  agentflow ships no fallback wave engine, deliberately — a silent substitution would be worse
  than the refusal. **That fallback does not extend to autodoc:** `/autodoc-init` and
  `/autodoc-update` have no single-agent path and forbid a sequential crawl, so without
  `Workflow` they do not run at all — enable the tool for the session first.
- **No `CronCreate`:** launch anyway. The loops read the guard as optional and say so where they
  schedule it; each still schedules its ordinary hourly `ScheduleWakeup` continuation. What you
  are accepting is that an account-limit blackout ends when you relaunch the lanes, so do not
  assume the fleet will come back by itself (the `wake_watch` ntfy reports it only if you
  opted in to the watchdog).

Whether these tools reach every Claude Code user is **not something this project claims to
know**. The check costs a second and the failure it catches is invisible, which is the whole
argument for running it.

## When something goes wrong

**Run the diagnostic first: `python -m agentflow.doctor --project <name>`.** It is the same
command as step 9, and step 9 is where the healthy output and the four statuses are set out. It
checks the whole resolved surface in one pass — config, paths, checkout, registry, globs, autodoc,
vault — and is built to run when nothing else does, so a broken config gets reported rather than
raised. Read the `fail` lines from the top: each carries its own remediation, and the first one is
usually the cause of the rest.

Copy the error text and paste it to Claude with "this failed, fix it." That is the actual
supported recovery path — it can read its own installer and config resolver, and the
error messages are written to name the field, the file, and the remedy.

Three specific things worth knowing:

- **A missing setting fails loudly and immediately.** Nothing silently falls back to a
  default, so a broken config stops the session at open rather than producing quiet
  nonsense later.
- **Nothing writes to the shared coordination file by hand** — all writes go through a
  command-line tool that checks ownership and refuses edits from the wrong loop. If you
  see a refusal, it's naming a rule, not malfunctioning.
- **A lane sitting stale on the board while its window is plainly alive** was almost
  certainly launched from the wrong directory. Its session came up without the fleet's
  settings and died at step 0, before the write that would have reported it, so the board
  shows the last row it managed instead of the failure. Check that window's `pwd` against
  the launch directory before you go looking for anything subtler.

## Honest limits

The install is straightforward. Knowing what to ask for is not.

Two of the interview questions in step 7 assume you can describe how your project proves
itself correct, and the system is only as good as that answer. And once it's running, the
quality of what comes out depends on the quality of the specs going in — the loops are
disciplined about *how* they work, not clairvoyant about *what* you want.

Expect to spend your first session in `dev-manager` learning what makes a good request,
not watching code appear.
