// workflows/wave_group.js
// Wave-group engine. One Workflow per parallel-safe group. Each task runs its
// per-mode pipeline; terminal state is reported via the wave-event reporter
// BEFORE returning so status.md is durable even if this return is lost.
export const meta = {
  name: 'wave-group',
  description: 'Run one parallel-safe group of orchestrator tasks (single / best-of-N / adversarial).',
  phases: [{ title: 'Implement' }, { title: 'Review' }, { title: 'Land' }],
}

const A = args  // { wave_n, status_path, repo_path, base_branch, max_fix_iterations, ntfy, reporter, worktree_cli, tasks: [...] }

// --- helpers --------------------------------------------------------------
// POSIX single quotes: nothing inside them expands, in bash on Linux or Git Bash on Windows.
// Double quotes (JSON.stringify) still expand $(...), backticks and $VAR, and agent-supplied
// text reaches these words. A ' inside a word closes the quote, emits \', and reopens it.
function sh(parts) { return parts.map(p => `'${String(p).replace(/'/g, `'\\''`)}'`).join(' ') }

// A commit field comes from an agent's untyped return; only an abbreviated or full hex SHA
// may reach --commit. 64 is a full SHA in a sha256-format repository.
const COMMIT_SHA = /^[0-9a-fA-F]{7,64}$/
// Agents say "no commit" in several ways; each is absent, not malformed.
const NO_COMMIT = /^(none|null|n\/a)?$/i

// Returns {sha} for a usable commit, {absent: true} for no commit, {bad} for anything else.
// Raw `git rev-parse` output carries a trailing newline, so strings are trimmed first.
function parseCommit(v) {
  if (v == null) return { absent: true }
  if (typeof v !== 'string') return { bad: v }
  const c = v.trim()
  if (NO_COMMIT.test(c)) return { absent: true }
  return COMMIT_SHA.test(c) ? { sha: c } : { bad: v }
}

const REPORT_SCHEMA = { type: 'object', additionalProperties: false, properties: {
  exit_code: { type: 'number' }, stderr: { type: 'string' } }, required: ['exit_code'] }

// Runs one reporter command; returns null on exit 0, else a failure record. A reporter agent
// that returns nothing counts as a failure: an unconfirmed durable write is not a durable write.
async function runReporter(kind, taskId, cmd) {
  const r = await agent(`Run exactly this shell command. Return JSON {exit_code, stderr}: its exit code and the last lines of its stderr. ${sh(cmd)}`,
    { label: `report:${kind}:${taskId}`, phase: 'Implement', schema: REPORT_SCHEMA })
  if (r && r.exit_code === 0) return null
  return { kind, exit_code: r ? r.exit_code : null, stderr: (r && r.stderr) || '' }
}

// Returns the failed reporter writes (empty when every write landed). Callers put the list on
// the task's return as `report_failures`, so a lost status.md write reaches the governor.
async function report(taskId, fields) {
  // fields: {state?, verifier_verdict?, findings?, commit?, note?, tier?}
  // `--status` is a SUBCOMMAND option in agentflow.wave_event (note|task-state), so it
  // must follow the subcommand, never precede it (wave 1 of epic 823 lost every report to argparse).
  const base = A.reporter.slice()
  const failures = []
  if (fields.note) {
    const cmd = base.concat(['note', '--status', A.status_path, '--task', String(taskId), '--tier', String(fields.tier || 2), '--summary', fields.note])
    const f = await runReporter('note', taskId, cmd)
    if (f) failures.push(f)
  }
  if (fields.state) {
    const cmd = ['task-state', '--status', A.status_path, '--task', String(taskId), '--state', fields.state]
    if (fields.verifier_verdict) cmd.push('--verifier-verdict', fields.verifier_verdict)
    if (fields.findings) cmd.push('--findings', JSON.stringify(fields.findings))
    const commit = parseCommit(fields.commit)
    // Dropped, not refused: a bad optional field never blocks the state write.
    if (commit.sha) cmd.push('--commit', commit.sha)
    else if (!commit.absent) failures.push({ kind: 'dropped:commit', exit_code: null, stderr: `not a commit SHA: ${JSON.stringify(commit.bad)}` })
    const f = await runReporter('state', taskId, base.concat(cmd))
    if (f) failures.push(f)
  }
  return failures
}

async function ntfy(msg) {
  if (!A.ntfy) return
  await agent(`POST to ntfy. Run exactly this shell command and report its exit code only: ${sh(['curl', '-fsS', '-d', msg, A.ntfy.url + '/' + A.ntfy.topic])}`, { label: 'ntfy', phase: 'Implement' })
}

const IMPL_SCHEMA = { type: 'object', additionalProperties: true, properties: {
  outcome: { type: 'string' }, verifier_verdict: { type: 'string' },
  verifier_findings_count: { type: 'object' }, verifier_iterations: { type: 'number' },
  commit: { type: 'string' }, escalation_requests: { type: 'array' },
}, required: ['outcome'] }

// `tree` overrides the tree named in the "in repo …" sentence. That sentence is not
// decoration: the wave contract at `contract_path` resolves its worktree from it (slot 3 of
// the contract's resolution order) and then carries `-C <that path>` on every git call. So a
// caller that builds somewhere other than the epic tree — best-of-N candidates, each in its
// own worktree — MUST pass its tree here, or the contract will faithfully point every
// command at the shared epic tree instead.
function implementPrompt(t, tree) {
  const where = tree || A.repo_path
  const contract = t.contract_path
    ? `FIRST: Read ${t.contract_path} — it is the governing wave contract (verifier loop, DoD self-check, scoped staging before every commit, decision tiers, return JSON shape). Follow it; the fields below bind its placeholders.\n`
    : ''
  return `${contract}Implement orchestrator task ${t.id} in repo ${where} on branch ${A.base_branch}.
SCALE TO SIZE: if the change is small, just do it once. Do not over-engineer.
Task body:\n${t.body}\n
file_allowlist (only touch these): ${JSON.stringify(t.file_allowlist)}
regression_guard (NEVER touch): ${JSON.stringify(t.regression_guard)}
acceptance_criteria: ${JSON.stringify(t.acceptance_criteria)}
Run an in-wave verifier loop up to ${A.max_fix_iterations} iterations. Run tests: ${JSON.stringify(t.test_commands)}.
Commit your work. Return JSON: {outcome: "done"|"verifier_blocker_persistent"|"test_failure_persistent"|"blocked"|"escalation_required", verifier_verdict, verifier_findings_count:{low,medium,blocker}, verifier_iterations, commit, escalation_requests:[]}.`
}

// A non-done outcome is written as `blocked`, which cannot say WHICH outcome it was, so the
// outcome goes into a task note as well. The prefix is WAVE_OUTCOME_NOTE_PREFIX in
// agentflow.wave_dispatch, whose reported_wave_outcome() reads it back on a fallback wake.
// NOTE_OUTCOMES is that module's WAVE_NOTE_OUTCOMES (a test pins the two lists together). An
// outcome outside it is written as `unknown_outcome`, so the outcome token is always a fixed one;
// escalation text stays in the return. A failed merge-back note (best-of-N or adversarial) also
// carries the branch and git's stderr, which sh() single-quotes.
const NOTE_OUTCOMES = ['blocked', 'escalation_required', 'test_failure_persistent', 'verifier_blocker_persistent']
function outcomeNote(res) {
  const outcome = NOTE_OUTCOMES.includes(res.outcome) ? res.outcome : 'unknown_outcome'
  return {
    note: `wave outcome: ${outcome}`,
    tier: outcome === 'escalation_required' || outcome === 'unknown_outcome' ? 3 : 2,
  }
}

// The reporter validates these against status_writer's VerifierVerdict and Finding severities
// and refuses the WHOLE write on a bad value, which would lose the task state with it. So an
// implementer's verdict or findings count that the schema would refuse is dropped here, and the
// drop is recorded in report_failures; a bad optional field never blocks the state write.
const VERIFIER_VERDICTS = ['clean', 'warnings', 'blocker_persistent']
const FINDING_SEVERITIES = ['low', 'medium', 'blocker']
function verifierFields(res) {
  const dropped = []
  let verdict = res.verifier_verdict
  if (verdict != null && !VERIFIER_VERDICTS.includes(verdict)) {
    dropped.push({ kind: 'dropped:verifier_verdict', exit_code: null, stderr: `not a VerifierVerdict: ${JSON.stringify(verdict)}` })
    verdict = undefined
  }
  let findings = res.verifier_findings_count
  const validFindings = f => typeof f === 'object' && !Array.isArray(f) &&
    Object.entries(f).every(([k, v]) => FINDING_SEVERITIES.includes(k) && Number.isInteger(v) && v >= 0)
  if (findings != null && !validFindings(findings)) {
    dropped.push({ kind: 'dropped:findings', exit_code: null, stderr: `not a {low,medium,blocker} count mapping: ${JSON.stringify(findings)}` })
    findings = undefined
  }
  return { verdict, findings, dropped }
}

// --- single-shot ----------------------------------------------------------
async function runSingle(t) {
  const r = await agent(implementPrompt(t), { label: `impl:${t.id}`, phase: 'Implement', model: t.model, schema: IMPL_SCHEMA })
  const res = r || { outcome: 'blocked' }
  // Report terminal task state BEFORE returning (durable truth).
  const reviewable = res.outcome === 'done'
  const vf = verifierFields(res)
  const report_failures = vf.dropped.concat(await report(t.id, {
    state: reviewable ? 'code_review' : 'blocked',
    verifier_verdict: vf.verdict, findings: vf.findings, commit: res.commit,
    ...(reviewable ? {} : outcomeNote(res)),
  }))
  await ntfy(`task ${t.id}: ${res.outcome}`)
  return { id: t.id, mode: 'single', needs_orchestrator_review: reviewable, ...res, report_failures }
}

// --- best-of-N ------------------------------------------------------------
const JUDGE_SCHEMA = { type: 'object', additionalProperties: false, properties: {
  winner: { type: 'number' }, reason: { type: 'string' } }, required: ['winner'] }
const CAND_SCHEMA = { type: 'object', additionalProperties: true, properties: {
  branch: { type: 'string' }, worktree: { type: 'string' }, tests_pass: { type: 'boolean' },
  commit: { type: 'string' } }, required: ['branch', 'tests_pass'] }

async function runBestOfN(t) {
  const N = t.best_of_n
  // 1. N isolated candidates (each creates its own worktree via the CLI).
  const cands = await parallel(Array.from({ length: N }, (_, i) => () =>
    agent(`You are candidate ${i + 1} of ${N} for task ${t.id}. Create an isolated worktree:
  ${sh(A.worktree_cli.concat(['create', '--repo', A.repo_path, '--epic', A.epic_slug, '--wave', String(A.wave_n), '--task', t.id + '-c' + i, '--base', A.base_branch, '--json']))}
The path that command prints is YOUR worktree. It is the one tree every command below runs
against, and every git call names it explicitly as \`git -C <printed path>\` — a \`cd\` alone
sets a default your own fan-out agents do not inherit. It is NOT ${A.repo_path}: that is the
shared epic tree, and ${N} candidates committing there would defeat the isolation this mode
exists for. Read the path out of the command's JSON before your first action; if the command
printed none, STOP and return \`tests_pass:false\` rather than falling back to any other tree.
Ignore any \`## Worktree context\` block in the task body below. That block is prepended
unconditionally and was written before best-of-N candidates existed: it names the wave's single
SHARED tree and does not know about you. It does NOT win slot 1 of the contract's resolution
order for this dispatch — your worktree is the path the create command printed, and nothing in
the task body overrides it.
Implement the task there, commit, then run the task tests ${JSON.stringify(t.test_commands)}.
${implementPrompt(t, '<the worktree path printed by the create command above>')}
Return JSON {branch, worktree, tests_pass:boolean, commit}.`,
      { label: `cand:${t.id}:${i}`, phase: 'Implement', model: t.model, schema: CAND_SCHEMA })
  ))
  // 2. Test pre-filter — only test-passing candidates are eligible.
  const eligible = cands.filter(c => c && c.tests_pass)
  if (eligible.length === 0) {
    const failures = await report(t.id, { state: 'blocked', note: `wave outcome: verifier_blocker_persistent (best-of-${N}: all candidates failed tests)` })
    // No candidate is eligible, so none holds a commit anyone will land: every tree goes.
    const cleanup_failures = await cleanupCandidates(t, cands.filter(Boolean))
    const cleanup_report_failures = await reportCleanupFailures(t, `best-of-${N}`, cleanup_failures)
    await ntfy(`task ${t.id}: all ${N} candidates failed tests`)
    return { id: t.id, mode: 'best_of_n', candidates_run: N, eliminated_by_tests: N, cleanup_failures,
             needs_orchestrator_review: false, outcome: 'verifier_blocker_persistent',
             verifier_findings_count: { blocker: 1 }, report_failures: failures.concat(cleanup_report_failures) }
  }
  // 3. 3-judge panel over eligible candidates → majority winner.
  const votes = await parallel([0, 1, 2].map(j => () =>
    agent(`Judge (lens ${['spec-fit', 'simplicity', 'scope-adherence'][j]}) task ${t.id}. Eligible candidate branches: ${JSON.stringify(eligible.map(c => c.branch))} in repo ${A.repo_path}. Diff each against ${A.base_branch}, pick the best by your lens. Return {winner: <0-based index into the eligible list>, reason}.`,
      { label: `judge:${t.id}:${j}`, phase: 'Review', schema: JUDGE_SCHEMA })))
  // A judge is asked for a 0-based index, but its answer is untrusted: a 1-based or missing
  // vote would index past the eligible list and throw, and the harness turns a throw into a
  // task that silently drops out of the wave. Only an in-range integer is a vote.
  const valid = votes.filter(v => v && Number.isInteger(v.winner) && v.winner >= 0 && v.winner < eligible.length)
  if (valid.length === 0) {
    const failures = await report(t.id, { state: 'blocked', note: 'wave outcome: blocked (best-of-N: no valid judge vote)' })
    // No winner, so no candidate holds a commit anyone will land: every tree goes.
    const cleanup_failures = await cleanupCandidates(t, cands.filter(Boolean))
    const cleanup_report_failures = await reportCleanupFailures(t, `best-of-${N}`, cleanup_failures)
    await ntfy(`task ${t.id}: best-of-${N} had no valid judge vote`)
    return { id: t.id, mode: 'best_of_n', candidates_run: N, judge_votes: {}, cleanup_failures,
             needs_orchestrator_review: false, outcome: 'blocked',
             report_failures: failures.concat(cleanup_report_failures) }
  }
  const tally = {}
  valid.forEach(v => { tally[v.winner] = (tally[v.winner] || 0) + 1 })
  const winnerIdx = Number(Object.entries(tally).sort((a, b) => b[1] - a[1])[0][0])
  const winner = eligible[winnerIdx]
  const winner_report_failures = await report(t.id, { note: `best-of-${N}: winner ${winner.branch} (votes ${JSON.stringify(tally)})` })
  // 4. Land the winner. Group tasks land concurrently, so a refusal is often another task's land
  // holding the index or moving the epic branch: a refused land is retried once.
  let { land, landed } = await landBranch(t, winner.worktree, winner.branch)
  if (!landed) ({ land, landed } = await landBranch(t, winner.worktree, winner.branch, 'retry'))
  // 5. Remove every candidate tree. Losers and a landed winner lose their branch too. An unlanded
  // winner keeps its branch, which holds the only copy of the commit; the tree adds nothing to
  // it (merge-back fetches a branch from any tree of the repository), and a kept tree would leak
  // on every re-dispatch.
  const losers = cands.filter(c => c && c !== winner)
  const cleanup_failures = await cleanupCandidates(t, [landed ? winner : { ...winner, preserve_branch: true }, ...losers])
  const cleanup_report_failures = await reportCleanupFailures(t, `best-of-${N}`, cleanup_failures)
  const prior = winner_report_failures.concat(cleanup_report_failures)
  // Landed or not, the winner's tree is gone (or listed in cleanup_failures), so the return names
  // no tree: an unlanded winner keeps only its branch, and a landed one keeps nothing.
  const base = { mode: 'best_of_n', candidates_run: N, winner_index: winnerIdx, judge_votes: tally,
                 branch: winner.branch, worktree: null, cleanup_failures }
  if (!landed) return await blockUnlanded(t, `best-of-${N}`, land, winner.branch, winner.commit, base, prior)
  // 6. Adversarial gate on the winner if requested, else reviews-inside → done.
  return await reviewInside(t, { ...base, commit: winner.commit, merge_exit_code: 0 }, prior)
}

// merge-back's CLI contract (agentflow.wave_worktree): it fetches the branch, fast-forwards the
// epic branch when it can and otherwise makes a --no-ff merge, aborting a merge that conflicts.
// It prints one JSON line {success, stderr} on stdout and exits 0 on success; a refused fetch or
// merge, a conflict, or a tip that did not reach HEAD prints success:false and exits 2.
const LAND_SCHEMA = { type: 'object', additionalProperties: false, properties: {
  merge_exit_code: { type: 'number' }, success: { type: 'boolean' }, stderr: { type: 'string' } },
  // All three required: a dropped `success` fails validation and is retried, not read as a block.
  required: ['merge_exit_code', 'success', 'stderr'] }
const MERGE_DETAIL_MAX = 300

// Lands `branch` from its wave tree onto the epic branch with the worktree CLI's merge-back.
// Group tasks run concurrently, so the epic branch moving under this merge is the normal case,
// not an edge. A moved branch rules out the fast-forward but still lands through the --no-ff
// merge unless the two trees conflict (the merge is aborted) or git refuses the merge before it
// starts (an index another land holds, a failing hook, no committer identity); either way the land
// is refused (exit 2), as it is when the fetch fails or the tip did not reach HEAD. The result is
// read, never assumed.
async function landBranch(t, worktree, branch, attempt = '') {
  const land = await agent(`Run exactly this shell command. It prints one JSON line {"success": <bool>, "stderr": <string>}. Return JSON {merge_exit_code, success, stderr}: the command's exit code, and the success and stderr fields of the JSON line it printed; if it printed no JSON line, success false and the last lines of its stderr. ${sh(A.worktree_cli.concat(['merge-back', '--wave-worktree', worktree, '--main', A.repo_path, '--branch', branch]))}`,
    { label: attempt ? `land:${t.id}:${attempt}` : `land:${t.id}`, phase: 'Land', schema: LAND_SCHEMA })
  // Landed only on exit 0 AND success:true; a land agent that returned nothing confirmed nothing.
  return { land, landed: !!land && land.merge_exit_code === 0 && land.success === true }
}

// The task's result when its commit did not land. The commit is not on the epic branch, so it is
// not the task's commit: it is returned as winner_commit and kept off the state write. The
// caller keeps the branch, which holds the only copy of that commit (adversarial keeps its tree
// too). `what` names the mode in the note and the ntfy (`best-of-N` or `adversarial`).
async function blockUnlanded(t, what, land, branch, unlanded_commit, base, prior) {
  const merge_exit_code = land ? land.merge_exit_code : null
  const merge_stderr = land ? String(land.stderr || '') : 'land agent returned no result'
  const detail = merge_stderr.replace(/\s+/g, ' ').trim().slice(0, MERGE_DETAIL_MAX) || 'no stderr'
  const failures = await report(t.id, { state: 'blocked',
    note: `wave outcome: blocked (${what}: merge-back of ${branch} failed, exit ${merge_exit_code}: ${detail})` })
  await ntfy(`task ${t.id}: ${what} merge-back failed`)
  return { id: t.id, ...base, winner_commit: unlanded_commit, needs_orchestrator_review: false,
           outcome: 'blocked', merge_exit_code, merge_stderr, report_failures: prior.concat(failures) }
}

// A cleanup that failed is recorded in a task note; it never changes the outcome.
async function reportCleanupFailures(t, what, cleanup_failures) {
  if (cleanup_failures.length === 0) return []
  return await report(t.id, {
    note: `${what}: cleanup failed for ${cleanup_failures.map(f => f.branch || f.path || '?').join(', ')}; remove each with the worktree CLI cleanup command`,
  })
}

// Removes each candidate's worktree and branch; a candidate marked `preserve_branch` keeps its
// branch (--preserve-branch). Returns the failures, one {branch, path, exit_code, stderr} per
// candidate whose cleanup did not exit 0 or that returned no tree to clean.
async function cleanupCandidates(t, cands) {
  const failures = []
  for (const c of cands) {
    if (!c.worktree || !c.branch) {
      failures.push({ branch: c.branch || null, path: c.worktree || null, exit_code: null, stderr: 'candidate returned no worktree or branch' })
      continue
    }
    const cmd = A.worktree_cli.concat(['cleanup', '--path', c.worktree, '--branch', c.branch, '--main', A.repo_path])
    if (c.preserve_branch) cmd.push('--preserve-branch')
    const r = await agent(`Run exactly this shell command. Return JSON {exit_code, stderr}: its exit code and the last lines of its stderr. ${sh(cmd)}`,
      { label: `cleanup:${t.id}:${c.branch}`, phase: 'Land', schema: REPORT_SCHEMA })
    if (!r || r.exit_code !== 0) {
      failures.push({ branch: c.branch, path: c.worktree, exit_code: r ? r.exit_code : null, stderr: (r && r.stderr) || '' })
    }
  }
  return failures
}

// --- adversarial refuter panel + inside spec/code reviews -----------------
const REFUTE_SCHEMA = { type: 'object', additionalProperties: false, properties: {
  refuted: { type: 'boolean' }, reason: { type: 'string' } }, required: ['refuted'] }
const REVIEW_SCHEMA = { type: 'object', additionalProperties: false, properties: {
  verdict: { type: 'string' }, findings: { type: 'array' } }, required: ['verdict'] }

async function refuterPanel(t, commit) {
  const votes = await parallel([0, 1, 2].map(j => () =>
    agent(`Adversarially verify task ${t.id} (commit ${commit || A.base_branch}) in ${A.repo_path}. Try to find a REAL defect against acceptance_criteria ${JSON.stringify(t.acceptance_criteria)}. Default refuted=true if uncertain. Return {refuted, reason}.`,
      { label: `refute:${t.id}:${j}`, phase: 'Review', schema: REFUTE_SCHEMA })))
  const refutes = votes.filter(Boolean).filter(v => v.refuted).length
  return refutes < 2  // survives unless majority refute
}

// `prior_failures`: reporter failures from earlier steps, carried onto the same return.
// `vf`: the implementer's verifierFields(), when it reported any. Its verdict and findings ride
// the done write and the return, so coerce_wave_outcome at reconcile sees a self-reported
// blocker instead of a bare `done`.
async function reviewInside(t, base, prior_failures = [], vf = null) {
  // Optional adversarial gate.
  if (t.adversarial) {
    const ok = await refuterPanel(t, base.commit)
    if (!ok) {
      const failures = await report(t.id, { state: 'blocked', note: 'wave outcome: verifier_blocker_persistent (adversarial refuter panel: majority refute)' })
      await ntfy(`task ${t.id}: adversarial refute`)
      return { id: t.id, ...base, needs_orchestrator_review: false,
               outcome: 'verifier_blocker_persistent', verifier_findings_count: { blocker: 1 },
               report_failures: prior_failures.concat(failures) }
    }
  }
  // Spec + code review INSIDE (heavy path only reaches here).
  const spec = await agent(`Spec-review task ${t.id} (commit ${base.commit}) in ${A.repo_path} against the plan body and acceptance_criteria. Return {verdict:"approve"|"revise", findings}.`,
    { label: `spec:${t.id}`, phase: 'Review', schema: REVIEW_SCHEMA })
  const code = await agent(`Code-quality review task ${t.id} (commit ${base.commit}) in ${A.repo_path}. Return {verdict:"approve"|"revise", findings}.`,
    { label: `code:${t.id}`, phase: 'Review', schema: REVIEW_SCHEMA })
  const approved = spec?.verdict === 'approve' && code?.verdict === 'approve'
  const failures = await report(t.id, { state: approved ? 'done' : 'blocked', commit: base.commit,
    verifier_verdict: vf?.verdict, findings: vf?.findings,
    ...(approved ? {} : { note: 'wave outcome: blocked (inside review asked for revision)' }) })
  await ntfy(`task ${t.id}: ${approved ? 'done (reviewed inside)' : 'revise'}`)
  return { id: t.id, ...base, needs_orchestrator_review: false,
           outcome: approved ? 'done' : 'blocked',
           spec_verdict: spec?.verdict, code_verdict: code?.verdict, commit: base.commit,
           ...(vf ? { verifier_verdict: vf.verdict, verifier_findings_count: vf.findings } : {}),
           report_failures: prior_failures.concat(failures) }
}

// Under per_wave_worktrees the orchestrator hands the task its wave tree (`wave_worktree`,
// `wave_branch`), and the implementer commits on that branch only. The commit is landed before
// any inside review, because the refuters and reviewers read the epic tree: reviewing first would
// review code that is not there, and nothing after the workflow lands an adversarial task.
async function runAdversarial(t) {
  // Half a wave tree is a malformed dispatch, not a no-tree task: building anyway would commit on
  // a branch nothing lands while the task reads `done`. Refused before any work is spent.
  if (!t.wave_worktree !== !t.wave_branch) {
    const missing = t.wave_worktree ? 'wave_branch' : 'wave_worktree'
    const report_failures = await report(t.id, { state: 'blocked',
      note: `wave outcome: blocked (adversarial: args carry a wave tree without ${missing}; not dispatched)` })
    await ntfy(`task ${t.id}: adversarial args name half a wave tree`)
    return { id: t.id, mode: 'adversarial', needs_orchestrator_review: false, outcome: 'blocked', report_failures }
  }
  const r = await agent(implementPrompt(t), { label: `impl:${t.id}`, phase: 'Implement', model: t.model, schema: IMPL_SCHEMA })
  const res = r || { outcome: 'blocked' }
  const vf = verifierFields(res)
  if (res.outcome !== 'done') {
    const report_failures = vf.dropped.concat(await report(t.id, { state: 'blocked', verifier_verdict: vf.verdict, findings: vf.findings, ...outcomeNote(res) }))
    return { id: t.id, mode: 'adversarial', needs_orchestrator_review: false, ...res, report_failures }
  }
  if (!t.wave_worktree) {
    return await reviewInside(t, { mode: 'adversarial', commit: res.commit }, vf.dropped, vf)
  }
  const base = { mode: 'adversarial', branch: t.wave_branch, worktree: t.wave_worktree }
  // A `done` that counts its own blockers is verifier_blocker_persistent (coerce_wave_outcome
  // rewrites it so at reconcile), and that outcome is re-dispatched: landing it first would put
  // the blocked commit on the epic branch for the retry to build on. So it is not landed, the
  // tree that holds it is kept, and its commit is returned as winner_commit, never as commit.
  if ((vf.findings?.blocker || 0) > 0) {
    const { commit, ...rest } = res
    const outcome = 'verifier_blocker_persistent'
    const report_failures = vf.dropped.concat(await report(t.id, { state: 'blocked',
      verifier_verdict: vf.verdict, findings: vf.findings, ...outcomeNote({ outcome }) }))
    await ntfy(`task ${t.id}: done with ${vf.findings.blocker} self-reported blocker(s); not landed`)
    return { id: t.id, ...rest, ...base, needs_orchestrator_review: false, outcome,
             verifier_verdict: vf.verdict, verifier_findings_count: vf.findings,
             winner_commit: commit, cleanup_failures: [], report_failures }
  }
  // A refused land is retried once, as best-of-N's is: another group task's land is the usual cause.
  let { land, landed } = await landBranch(t, t.wave_worktree, t.wave_branch)
  if (!landed) ({ land, landed } = await landBranch(t, t.wave_worktree, t.wave_branch, 'retry'))
  if (!landed) {
    return await blockUnlanded(t, 'adversarial', land, t.wave_branch, res.commit,
      { ...base, cleanup_failures: [] }, vf.dropped)
  }
  const cleanup_failures = await cleanupCandidates(t, [{ worktree: t.wave_worktree, branch: t.wave_branch }])
  const prior = vf.dropped.concat(await reportCleanupFailures(t, 'adversarial', cleanup_failures))
  // The landed tree is removed (or listed in cleanup_failures), so the return names no tree.
  return await reviewInside(t, { ...base, worktree: null, cleanup_failures, commit: res.commit, merge_exit_code: 0 }, prior, vf)
}

// --- dispatch -------------------------------------------------------------
phase('Implement')
const results = await parallel(A.tasks.map(t => () => {
  if (t.mode === 'best_of_n') return runBestOfN(t)
  if (t.mode === 'adversarial') return runAdversarial(t)
  return runSingle(t)
}))

return results.filter(Boolean)
