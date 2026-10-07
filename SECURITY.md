# Security policy

## Reporting a vulnerability

Please report privately. Go to this repository's **Security** tab and use the
**Report a vulnerability** button, which opens a private report only the maintainers can see.

If the button isn't there, open a public issue asking for a private way to get in touch. Don't
include any details of the problem in that issue.

## Supported versions

Only the latest commit on the default branch is supported. Fixes go there. Older commits and
releases don't get backports.

## What install changes

Installing agentflow changes your Claude Code configuration, and the fleet is built to run
unattended. Read [*What install changes on your machine*](README.md#quickstart) in the README's
Quickstart section before you install.

The part most relevant to security: install writes **PreToolUse auto-allow hooks** into your
Claude settings file. They approve the `Edit`, `Write` and `Agent` tools, and a list of `Bash`
command patterns, without a permission prompt. The authoritative list is
`BASH_AUTO_ALLOW_PATTERNS` in
[`skills/orchestrator/bootstrap.py`](skills/orchestrator/bootstrap.py). Read it there. This page
doesn't copy it, so it can't go out of date. A deployment can widen that list at install time
through the `AGENTFLOW_EXTRA_AUTO_ALLOW` environment variable (`extra_patterns_from_env()` in the
same file).

## How secrets are handled

- **Config files store environment-variable names, not values.** A config field of type
  `env-name` holds only the name of a variable. The value is read from the environment when the
  config is resolved and is never written into the markdown config files. See
  [`skills/_shared/SCHEMA.md`](skills/_shared/SCHEMA.md), §0.
- **Session links are never published.** Claude Code session URLs are links to a private
  conversation. They don't go in commits, pull requests, issue or PR comments, or tracked files.
  The rule is in [`skills/_shared/PR-PROSE.md`](skills/_shared/PR-PROSE.md), and the installed loop
  skills follow it.
- **A CI gate keeps personal and machine-specific literals out.** The `sanitize-gate` job in
  [`.github/workflows/ci.yml`](.github/workflows/ci.yml) fails the build on absolute home paths
  with a real account name and similar machine-specific literals. A maintainer supplies patterns
  specific to their own install through a repository secret, which the CI gate appends to its
  pattern and the run logs mask, and a local file outside the repository, which the publish cut
  and the wiki residue check read. Neither ships, so a public copy's gate checks the generic
  shapes only. It checks every first-party text file in the tree and every pull request's title
  and body.
  [`CONTRIBUTING.md`](CONTRIBUTING.md) explains how to run it locally.

## Known trade-offs

Some behavior is risky by design and documented. See
[*Security & blast radius*](README.md#security--blast-radius) in the README. In short:

- Most loops run with permission prompts turned off.
- Promoting a spec for autonomous work authorizes the merge as well as the build.
- Issue text and other agents' output reach the loops as input.

These are known and documented, so on their own they are not vulnerabilities. A report showing
that one of them reaches further than the README says is in scope, and we want to hear about it.
