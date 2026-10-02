# Fleet Registry — <project>

<!-- Single coordination surface for all fleet agents. Schema, ownership rules, firing
     contract, and question lifecycle: <suite_root>/docs/REGISTRY.md (binding). Read the whole file at
     every wake (no lock); write only rows you own under the mkdir edit-lock
     <lock_dir>/<project>-registry.lock (stale >5 min = breakable). dev-manager is janitor
     and sole human surface. Ntfy is notification-only fleet-wide. -->

## Agents

| agent | status | work | phase | heartbeat | eta | waiting_on |
|---|---|---|---|---|---|---|
| lead-dev | idle | - | - | - | - | - |
| jr-dev-1 | idle | - | - | - | - | - |
| jr-dev-2 | idle | - | - | - | - | - |
| jr-dev-3 | idle | - | - | - | - | - |
| auditor | idle | - | - | - | - | - |
| steward | idle | - | - | - | - | - |
| dev-manager | idle | - | - | - | - | - |

## Queue

- **Deploy lock** — holder: none, taken-at: -
- **Notes** — (none)

| id | work item | lane | claimed_by | state | waiting_on | notes |
|---|---|---|---|---|---|---|

## Questions

<!-- ### Q-NNNN blocks appended here; see <suite_root>/docs/REGISTRY.md for the field list. -->

## Blockers

| id | raised_by | owner | work | state | description |
|---|---|---|---|---|---|

## Directives

<!-- Standing human instructions, transcribed by dev-manager only. -->
