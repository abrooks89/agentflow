// Shell-safety of the commands wave_group.js hands its agents (gh#706).
//
// The workflow body runs with `args`, `agent`, `phase` and `parallel` supplied by the harness,
// so this file compiles it the same way with stubs. Every "Run exactly this shell command"
// the workflow emits is executed by a real bash with `curl` shadowed by a printing function,
// in a scratch directory, so an unquoted `$(...)` shows up as a file that should not exist.
//
// Run: node --test skills/orchestrator/tests/

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, mkdtempSync, readdirSync, rmSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const SRC = readFileSync(join(HERE, '..', 'workflows', 'wave_group.js'), 'utf8')
const FINAL_RETURN = /^return results\.filter\(Boolean\)\s*$/m
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor

// Compiles the workflow body; the final return also hands back `sh` for direct checks.
function compile() {
  assert.match(SRC, FINAL_RETURN, 'wave_group.js final return moved; update this harness')
  const body = SRC.replace(/^export const meta/m, 'const meta')
    .replace(FINAL_RETURN, 'return { results: results.filter(Boolean), sh }')
  return new AsyncFunction('args', 'agent', 'phase', 'parallel', body)
}

const REPORT_PREFIX = 'last lines of its stderr. '
const NTFY_PREFIX = 'Run exactly this shell command and report its exit code only: '

function runBash(cmd, cwd) {
  const r = spawnSync('bash', ['-c', `curl() { printf '%s\\n' "$@"; }\n${cmd}`], { cwd, encoding: 'utf8' })
  assert.equal(r.error, undefined, `bash did not start: ${r.error}`)
  return r
}

// Runs the workflow over `tasks`; returns what it returned plus every command it asked for,
// each already executed in `cwd`.
// `stubs` maps a label prefix to a function returning that agent's result; a stubbed call is
// recorded in `calls` but runs nothing. Every other prompt carrying the run-exactly marker is run.
async function runWorkflow({ tasks, implReturn, cwd, statusPath = 'st.md', stubs = {} }) {
  const commands = []
  const calls = []
  // The harness's parallel() resolves a thunk that throws to null, so a crashing task drops out
  // of the return instead of failing the wave. The error is kept here so a test can show it.
  const thrown = []
  const agent = async (prompt, opts) => {
    const label = opts.label
    calls.push({ label, prompt, schema: opts.schema })
    const stub = Object.keys(stubs).find(k => label.startsWith(k))
    if (stub) return stubs[stub](prompt, opts)
    if (label.startsWith('impl:')) return implReturn
    let cmd = null
    if (label.startsWith('report:') || label.startsWith('cleanup:')) cmd = prompt.slice(prompt.indexOf(REPORT_PREFIX) + REPORT_PREFIX.length)
    if (label === 'ntfy') {
      // A prompt without the marker is recorded unrun; the ntfy test asserts on it.
      if (!prompt.includes(NTFY_PREFIX)) { commands.push({ label, cmd: null, prompt }); return null }
      cmd = prompt.slice(prompt.indexOf(NTFY_PREFIX) + NTFY_PREFIX.length)
    }
    if (cmd === null) return null
    const r = runBash(cmd, cwd)
    commands.push({ label, cmd, stdout: r.stdout, status: r.status })
    return { exit_code: r.status, stderr: r.stderr }
  }
  const args = {
    wave_n: 1, status_path: statusPath, repo_path: 'repo', base_branch: 'main',
    max_fix_iterations: 1, reporter: ['echo'], worktree_cli: ['echo'],
    ntfy: { url: 'http://ntfy.invalid', topic: 'topic' }, tasks,
  }
  const parallel = fns => Promise.all(fns.map(f => Promise.resolve().then(f).catch(e => { thrown.push(e); return null })))
  const out = await compile()(args, agent, () => {}, parallel)
  return { ...out, commands, calls, thrown }
}

function scratch() { return mkdtempSync(join(tmpdir(), 'wave-group-')) }

test('issue repro: a $(...) word is passed literally, not expanded', async () => {
  const dir = scratch()
  try {
    const { sh } = await runWorkflow({ tasks: [], cwd: dir })
    const cmd = sh(['echo', 'task-state', '--commit', 'abc123 $(touch INJECTED)'])
    const r = runBash(cmd, dir)
    assert.equal(r.status, 0)
    assert.equal(r.stdout, 'task-state --commit abc123 $(touch INJECTED)\n')
    assert.deepEqual(readdirSync(dir), [])
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('sh() keeps backticks, $VAR and embedded single quotes literal', async () => {
  const dir = scratch()
  try {
    const { sh } = await runWorkflow({ tasks: [], cwd: dir })
    const words = ["it's", '`touch TICK`', '$HOME', "a'b'c", '', 'x"y']
    const r = runBash(`printf '<%s>\\n' ${sh(words)}`, dir)
    assert.equal(r.stdout, words.map(w => `<${w}>\n`).join(''))
    assert.deepEqual(readdirSync(dir), [])
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('reporter command quotes an orchestrator-supplied path with shell syntax in it', async () => {
  const dir = scratch()
  try {
    const { commands } = await runWorkflow({
      tasks: [{ id: 1, mode: 'single' }], cwd: dir, statusPath: 'st $(touch INJECTED).md',
      implReturn: { outcome: 'done', commit: 'deadbeef' },
    })
    const state = commands.find(c => c.label === 'report:state:1')
    assert.ok(state, 'no state write was emitted')
    assert.match(state.stdout, /--status st \$\(touch INJECTED\)\.md /)
    assert.deepEqual(readdirSync(dir), [])
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('a non-SHA commit is dropped before --commit and recorded in report_failures', async () => {
  const dir = scratch()
  try {
    const { results, commands } = await runWorkflow({
      tasks: [{ id: 1, mode: 'single' }], cwd: dir,
      implReturn: { outcome: 'done', commit: 'abc123 $(touch INJECTED)' },
    })
    const state = commands.find(c => c.label === 'report:state:1')
    assert.ok(state, 'the state write must still happen')
    assert.doesNotMatch(state.cmd, /--commit/)
    const dropped = results[0].report_failures.filter(f => f.kind === 'dropped:commit')
    assert.equal(dropped.length, 1)
    assert.deepEqual(readdirSync(dir), [])
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('a real SHA reaches --commit and records no failure', async () => {
  const dir = scratch()
  // 64 hex characters is a full SHA in a sha256-format repository.
  for (const sha of ['abc1234', 'a'.repeat(40), 'ABCDEF1234', 'f'.repeat(64)]) {
    const { results, commands } = await runWorkflow({
      tasks: [{ id: 1, mode: 'single' }], cwd: dir, implReturn: { outcome: 'done', commit: sha },
    })
    const state = commands.find(c => c.label === 'report:state:1')
    assert.match(state.stdout, new RegExp(`--commit ${sha}\\n$`))
    assert.deepEqual(results[0].report_failures, [])
  }
  rmSync(dir, { recursive: true, force: true })
})

test('a too-short or too-long hex string is not a SHA', async () => {
  const dir = scratch()
  try {
    for (const bad of ['abc12', 'abc123', 'a'.repeat(65), 'zzzzzzz']) {
      const { results, commands } = await runWorkflow({
        tasks: [{ id: 1, mode: 'single' }], cwd: dir, implReturn: { outcome: 'done', commit: bad },
      })
      assert.doesNotMatch(commands.find(c => c.label === 'report:state:1').cmd, /--commit/)
      assert.equal(results[0].report_failures.filter(f => f.kind === 'dropped:commit').length, 1)
    }
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('a padded SHA is trimmed, then reaches --commit with no failure', async () => {
  const dir = scratch()
  try {
    for (const padded of ['abc1234\n', ' abc1234', '\tabc1234  ']) {
      const { results, commands } = await runWorkflow({
        tasks: [{ id: 1, mode: 'single' }], cwd: dir, implReturn: { outcome: 'done', commit: padded },
      })
      const state = commands.find(c => c.label === 'report:state:1')
      assert.match(state.stdout, /--commit abc1234\n$/)
      assert.deepEqual(results[0].report_failures, [])
    }
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('an empty or sentinel commit is absent: no --commit and no failure entry', async () => {
  const dir = scratch()
  try {
    for (const absent of ['', '   ', 'none', 'None', 'NULL', 'null', 'n/a', 'N/A', ' none\n', null, undefined]) {
      const { results, commands } = await runWorkflow({
        tasks: [{ id: 1, mode: 'single' }], cwd: dir, implReturn: { outcome: 'done', commit: absent },
      })
      const state = commands.find(c => c.label === 'report:state:1')
      assert.ok(state, 'the state write must still happen')
      assert.doesNotMatch(state.cmd, /--commit/)
      assert.deepEqual(results[0].report_failures, [], `commit ${JSON.stringify(absent)} was not treated as absent`)
    }
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('ntfy message goes through the same quoting', async () => {
  const dir = scratch()
  try {
    const { commands } = await runWorkflow({
      tasks: [{ id: 1, mode: 'single' }], cwd: dir,
      implReturn: { outcome: 'done $(touch NTFY_INJECTED)', commit: 'deadbeef' },
    })
    const ntfy = commands.find(c => c.label === 'ntfy')
    assert.ok(ntfy, 'no ntfy command was emitted')
    assert.ok(ntfy.cmd, `ntfy prompt carries no runnable command after its marker: ${ntfy.prompt}`)
    assert.match(ntfy.stdout, /^-fsS\n-d\ntask 1: done \$\(touch NTFY_INJECTED\)\nhttp:\/\/ntfy\.invalid\/topic\n$/)
    assert.deepEqual(readdirSync(dir), [])
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

// --- best-of-N landing ------------------------------------------------------
// Two candidates both pass tests; every judge picks candidate 0. The land agent's answer is
// what each test varies.
const CAND = i => ({ branch: `ep-w1-t3-c${i}`, worktree: `repo-ep-w1-t3-c${i}`, tests_pass: true, commit: `abc123${i}` })
const BEST_OF_N_STUBS = {
  'cand:': (_p, opts) => CAND(Number(opts.label.split(':')[2])),
  'judge:': () => ({ winner: 0, reason: 'r' }),
  'spec:': () => ({ verdict: 'approve' }),
  'code:': () => ({ verdict: 'approve' }),
}
const FF_REFUSED = 'fatal: Not possible to fast-forward, aborting.\n'

function cleanupOf(commands, branch) {
  return commands.filter(c => c.label.startsWith('cleanup:') && c.stdout.includes(`--branch ${branch} `))
}

test('best-of-N: a refused merge-back blocks the task with the stderr and skips review', async () => {
  const dir = scratch()
  try {
    const { results, commands, calls } = await runWorkflow({
      tasks: [{ id: 3, mode: 'best_of_n', best_of_n: 2 }], cwd: dir,
      stubs: { ...BEST_OF_N_STUBS, 'land:': () => ({ merge_exit_code: 2, success: false, stderr: FF_REFUSED }) },
    })
    const land = calls.find(c => c.label === 'land:3')
    assert.ok(land.schema, 'the land agent must be dispatched with a schema so its result can be read')
    // A schema that lets `success` drop out turns a real landing into a block with no retry.
    assert.deepEqual([...land.schema.required].sort(), ['merge_exit_code', 'stderr', 'success'])
    // The command handed over merges the winner's own branch from its tree into the main checkout.
    const landCmd = runBash(land.prompt.slice(land.prompt.indexOf(REPORT_PREFIX) + REPORT_PREFIX.length), dir)
    assert.equal(landCmd.stdout, 'merge-back --wave-worktree repo-ep-w1-t3-c0 --main repo --branch ep-w1-t3-c0\n')
    const r = results[0]
    assert.equal(r.outcome, 'blocked')
    assert.equal(r.needs_orchestrator_review, false)
    assert.equal(r.merge_exit_code, 2)
    assert.match(r.merge_stderr, /Not possible to fast-forward/)
    assert.equal(r.commit, undefined, 'an unlanded winner must not report a commit as the task commit')
    assert.ok(!calls.some(c => c.label.startsWith('spec:') || c.label.startsWith('code:')), 'review ran on an unlanded winner')
    const state = commands.find(c => c.label === 'report:state:3')
    assert.equal(state.stdout, 'task-state --status st.md --task 3 --state blocked\n', 'blocked, with no --commit for an unlanded winner')
    const note = commands.find(c => c.label === 'report:note:3' && c.stdout.includes('wave outcome: blocked ('))
    assert.ok(note, 'no blocked outcome note was written')
    assert.match(note.stdout, /Not possible to fast-forward/)
    const [winnerCleanup] = cleanupOf(commands, 'ep-w1-t3-c0')
    assert.ok(winnerCleanup, 'the unlanded winner tree was left behind')
    assert.equal(winnerCleanup.stdout, 'cleanup --path repo-ep-w1-t3-c0 --branch ep-w1-t3-c0 --main repo --preserve-branch\n',
      'the unlanded winner branch holds the only copy of its commit, so only its tree goes')
    assert.equal(cleanupOf(commands, 'ep-w1-t3-c1').length, 1, 'the losing candidate is still cleaned up')
    assert.doesNotMatch(cleanupOf(commands, 'ep-w1-t3-c1')[0].stdout, /--preserve-branch/)
    assert.equal(r.branch, 'ep-w1-t3-c0', 'the return names the preserved branch')
    assert.equal(r.worktree, null, 'the return must not name the removed winner tree')
    assert.equal(r.winner_commit, 'abc1230')
    assert.deepEqual(readdirSync(dir), [])
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

// The fake worktree model: a tree exists from the moment its candidate returns it until a
// cleanup command naming its --path exits 0. Every best-of-N exit must leave this set empty.
function liveTrees(cands, commands) {
  const removed = new Set(commands
    .filter(c => c.label.startsWith('cleanup:') && c.status === 0)
    .map(c => c.stdout.match(/--path (\S+)/)[1]))
  return cands.filter(Boolean).map(c => c.worktree).filter(p => p && !removed.has(p))
}

test('best-of-N: a refused land is retried once, then the winner tree goes and its branch stays', async () => {
  const dir = scratch()
  try {
    const { results, commands, calls } = await runWorkflow({
      tasks: [{ id: 3, mode: 'best_of_n', best_of_n: 2 }], cwd: dir,
      stubs: { ...BEST_OF_N_STUBS, 'land:': () => ({ merge_exit_code: 2, success: false, stderr: FF_REFUSED }) },
    })
    assert.equal(calls.filter(c => c.label.startsWith('land:3')).length, 2, 'a refused land was not retried')
    assert.equal(results[0].outcome, 'blocked')
    assert.deepEqual(results[0].cleanup_failures, [])
    assert.deepEqual(liveTrees([CAND(0), CAND(1)], commands), [], 'an unlanded best-of-N wave leaked a tree')
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('best-of-N: a land that succeeds on the retry is a landing', async () => {
  const dir = scratch()
  try {
    let n = 0
    const { results, commands, calls } = await runWorkflow({
      tasks: [{ id: 3, mode: 'best_of_n', best_of_n: 2 }], cwd: dir,
      stubs: { ...BEST_OF_N_STUBS, 'land:': () => (n++ === 0 ? { merge_exit_code: 2, success: false, stderr: FF_REFUSED } : LANDED()) },
    })
    assert.equal(calls.filter(c => c.label.startsWith('land:3')).length, 2)
    const r = results[0]
    assert.equal(r.outcome, 'done')
    assert.equal(r.commit, 'abc1230')
    assert.ok(calls.some(c => c.label === 'spec:3'), 'a landed winner skipped review')
    for (const i of [0, 1]) assert.doesNotMatch(cleanupOf(commands, `ep-w1-t3-c${i}`)[0].stdout, /--preserve-branch/)
    assert.deepEqual(liveTrees([CAND(0), CAND(1)], commands), [])
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('best-of-N: every candidate failing tests removes every candidate tree and branch', async () => {
  const dir = scratch()
  try {
    const failing = i => ({ ...CAND(i), tests_pass: false })
    const { results, commands, calls } = await runWorkflow({
      tasks: [{ id: 3, mode: 'best_of_n', best_of_n: 2 }], cwd: dir,
      stubs: { ...BEST_OF_N_STUBS, 'cand:': (_p, opts) => failing(Number(opts.label.split(':')[2])) },
    })
    const r = results[0]
    assert.equal(r.outcome, 'verifier_blocker_persistent')
    assert.deepEqual(r.cleanup_failures, [])
    assert.ok(!calls.some(c => /^(judge|land):/.test(c.label)))
    for (const i of [0, 1]) {
      const [c] = cleanupOf(commands, `ep-w1-t3-c${i}`)
      assert.ok(c, `failed candidate ${i} leaked its tree`)
      assert.equal(c.stdout, `cleanup --path repo-ep-w1-t3-c${i} --branch ep-w1-t3-c${i} --main repo\n`)
    }
    assert.deepEqual(liveTrees([failing(0), failing(1)], commands), [], 'an all-failed best-of-N wave leaked a tree')
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('best-of-N: all failed with one candidate returning nothing still cleans the rest and records the gap', async () => {
  const dir = scratch()
  try {
    const cands = [null, { ...CAND(1), tests_pass: false }, { branch: 'ep-w1-t3-c2', tests_pass: false }]
    const { results, commands } = await runWorkflow({
      tasks: [{ id: 3, mode: 'best_of_n', best_of_n: 3 }], cwd: dir,
      stubs: { ...BEST_OF_N_STUBS, 'cand:': (_p, opts) => cands[Number(opts.label.split(':')[2])] },
    })
    const r = results[0]
    assert.equal(r.outcome, 'verifier_blocker_persistent')
    assert.deepEqual(liveTrees(cands, commands), [])
    assert.deepEqual(r.cleanup_failures, [{ branch: 'ep-w1-t3-c2', path: null, exit_code: null, stderr: 'candidate returned no worktree or branch' }])
    assert.ok(commands.some(c => c.label === 'report:note:3' && c.stdout.includes('cleanup failed for ep-w1-t3-c2')))
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('best-of-N: a land agent that returns nothing, or exit 0 without success, is not a landing', async () => {
  const dir = scratch()
  try {
    for (const land of [null, { merge_exit_code: 0, success: false, stderr: 'x' }, { merge_exit_code: 0 }]) {
      const { results, calls } = await runWorkflow({
        tasks: [{ id: 3, mode: 'best_of_n', best_of_n: 2 }], cwd: dir,
        stubs: { ...BEST_OF_N_STUBS, 'land:': () => land },
      })
      assert.equal(results[0].outcome, 'blocked', `land result ${JSON.stringify(land)} counted as landed`)
      assert.ok(!calls.some(c => c.label.startsWith('spec:')))
    }
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('best-of-N: a landed winner has its worktree and branch removed, then is reviewed', async () => {
  const dir = scratch()
  try {
    const { results, commands } = await runWorkflow({
      tasks: [{ id: 3, mode: 'best_of_n', best_of_n: 2 }], cwd: dir,
      stubs: { ...BEST_OF_N_STUBS, 'land:': () => ({ merge_exit_code: 0, success: true, stderr: '' }) },
    })
    for (const i of [0, 1]) {
      const [c] = cleanupOf(commands, `ep-w1-t3-c${i}`)
      assert.ok(c, `candidate ${i} was not cleaned up`)
      assert.equal(c.stdout, `cleanup --path repo-ep-w1-t3-c${i} --branch ep-w1-t3-c${i} --main repo\n`)
    }
    const r = results[0]
    assert.equal(r.outcome, 'done')
    assert.equal(r.commit, 'abc1230')
    assert.deepEqual(r.cleanup_failures, [])
    assert.match(commands.find(c => c.label === 'report:state:3').stdout, /--state done --commit abc1230\n/)
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('best-of-N: a failed winner cleanup is recorded, not swallowed', async () => {
  const dir = scratch()
  try {
    const { results, commands } = await runWorkflow({
      tasks: [{ id: 3, mode: 'best_of_n', best_of_n: 2 }], cwd: dir,
      stubs: {
        ...BEST_OF_N_STUBS, 'land:': () => ({ merge_exit_code: 0, success: true, stderr: '' }),
        'cleanup:3:ep-w1-t3-c0': () => ({ exit_code: 2, stderr: 'wave-worktree error: busy' }),
      },
    })
    const r = results[0]
    assert.equal(r.outcome, 'done', 'the code landed; a leaked tree does not undo that')
    assert.deepEqual(r.cleanup_failures, [{ branch: 'ep-w1-t3-c0', path: 'repo-ep-w1-t3-c0', exit_code: 2, stderr: 'wave-worktree error: busy' }])
    const note = commands.find(c => c.label === 'report:note:3' && c.stdout.includes('cleanup failed'))
    assert.ok(note, 'no note records the failed cleanup')
    assert.match(note.stdout, /ep-w1-t3-c0/)
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

// --- adversarial landing under per-wave worktrees ----------------------------
// A task built in its own wave tree carries that tree and its branch; its commit is on the wave
// branch only, so the workflow must land it before reviewers read the epic tree.
const ADV = { id: 3, mode: 'adversarial', adversarial: true }
const ADV_TREE = { ...ADV, wave_worktree: 'repo-ep-w1-t3', wave_branch: 'ep-w1-t3' }
const ADV_IMPL = { outcome: 'done', commit: 'abcdef1' }
const REVIEW_STUBS = { 'spec:': () => ({ verdict: 'approve' }), 'code:': () => ({ verdict: 'approve' }) }
const isReview = c => /^(refute|spec|code):/.test(c.label)
const LANDED = () => ({ merge_exit_code: 0, success: true, stderr: '' })

test('adversarial + wave tree: merge-back runs before the inside reviews, then the tree is removed', async () => {
  const dir = scratch()
  try {
    const { results, commands, calls } = await runWorkflow({
      tasks: [ADV_TREE], cwd: dir, implReturn: ADV_IMPL, stubs: { ...REVIEW_STUBS, 'land:': LANDED },
    })
    const landAt = calls.findIndex(c => c.label === 'land:3')
    assert.ok(landAt >= 0, 'no merge-back was issued for the wave branch')
    const land = calls[landAt]
    const landCmd = runBash(land.prompt.slice(land.prompt.indexOf(REPORT_PREFIX) + REPORT_PREFIX.length), dir)
    assert.equal(landCmd.stdout, 'merge-back --wave-worktree repo-ep-w1-t3 --main repo --branch ep-w1-t3\n')
    const firstReview = calls.findIndex(isReview)
    assert.ok(firstReview > landAt, 'an inside review ran before the commit reached the epic tree')
    const r = results[0]
    assert.equal(r.outcome, 'done')
    assert.equal(r.commit, 'abcdef1')
    assert.equal(r.merge_exit_code, 0)
    assert.deepEqual(r.cleanup_failures, [])
    assert.match(commands.find(c => c.label === 'report:state:3').stdout, /--state done --commit abcdef1\n$/)
    const [cleanup] = cleanupOf(commands, 'ep-w1-t3')
    assert.ok(cleanup, 'the landed wave tree was not removed')
    assert.equal(cleanup.stdout, 'cleanup --path repo-ep-w1-t3 --branch ep-w1-t3 --main repo\n')
    assert.deepEqual(readdirSync(dir), [])
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('adversarial + wave tree: a refused merge-back blocks the task, keeps the tree, skips review', async () => {
  const dir = scratch()
  try {
    for (const land of [{ merge_exit_code: 2, success: false, stderr: 'CONFLICT (content): Merge conflict in a.py\n' }, null]) {
      const { results, commands, calls } = await runWorkflow({
        tasks: [ADV_TREE], cwd: dir, implReturn: ADV_IMPL,
        stubs: { ...REVIEW_STUBS, 'land:': () => land },
      })
      assert.ok(!calls.some(isReview), 'a review ran on an unlanded commit')
      const r = results[0]
      assert.equal(r.outcome, 'blocked')
      assert.equal(r.mode, 'adversarial')
      assert.equal(r.needs_orchestrator_review, false)
      assert.equal(r.merge_exit_code, land ? 2 : null)
      assert.equal(r.winner_commit, 'abcdef1')
      assert.equal(r.commit, undefined, 'an unlanded commit must not be reported as the task commit')
      assert.equal(r.branch, 'ep-w1-t3')
      assert.equal(r.worktree, 'repo-ep-w1-t3')
      if (land) assert.match(r.merge_stderr, /Merge conflict in a\.py/)
      else assert.equal(r.merge_stderr, 'land agent returned no result')
      const state = commands.find(c => c.label === 'report:state:3')
      assert.equal(state.stdout, 'task-state --status st.md --task 3 --state blocked\n')
      const note = commands.find(c => c.label === 'report:note:3' && c.stdout.includes('wave outcome: blocked ('))
      assert.ok(note, 'no blocked outcome note was written')
      assert.match(note.stdout, land ? /merge-back of ep-w1-t3 failed, exit 2: CONFLICT/ : /exit null: land agent returned no result/)
      assert.equal(cleanupOf(commands, 'ep-w1-t3').length, 0, 'the wave tree holds the only copy of the commit')
    }
    assert.deepEqual(readdirSync(dir), [])
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('adversarial with no wave tree: no merge-back, reviews run, done as before', async () => {
  const dir = scratch()
  try {
    const { results, commands, calls } = await runWorkflow({
      tasks: [ADV], cwd: dir, implReturn: ADV_IMPL, stubs: REVIEW_STUBS,
    })
    assert.ok(!calls.some(c => c.label.startsWith('land:') || c.label.startsWith('cleanup:')))
    assert.ok(calls.some(c => c.label === 'spec:3'))
    assert.equal(results[0].outcome, 'done')
    assert.equal(results[0].commit, 'abcdef1')
    assert.equal(commands.find(c => c.label === 'report:state:3').stdout, 'task-state --status st.md --task 3 --state done --commit abcdef1\n')
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('adversarial: the implementer verdict and findings reach the state write and the return', async () => {
  const dir = scratch()
  try {
    // With no wave tree the commit is already on the epic branch, so a self-reported blocker still
    // rides a done write and reconcile coerces it; a wave-tree task with blockers is not landed (below).
    for (const [task, verdict, findings] of [[ADV, 'blocker_persistent', { blocker: 2 }], [ADV_TREE, 'warnings', { medium: 1 }]]) {
      const impl = { outcome: 'done', verifier_verdict: verdict, verifier_findings_count: findings, commit: 'abcdef1' }
      const { results, commands } = await runWorkflow({
        tasks: [task], cwd: dir, implReturn: impl, stubs: { ...REVIEW_STUBS, 'land:': LANDED },
      })
      const state = commands.find(c => c.label === 'report:state:3')
      assert.equal(state.stdout, `task-state --status st.md --task 3 --state done --verifier-verdict ${verdict} --findings ${JSON.stringify(findings)} --commit abcdef1\n`)
      const r = results[0]
      assert.equal(r.verifier_verdict, verdict)
      assert.deepEqual(r.verifier_findings_count, findings)
      assert.deepEqual(r.report_failures, [])
    }
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('adversarial + wave tree: a done with self-reported blockers is not landed, reviewed or cleaned', async () => {
  const dir = scratch()
  try {
    const impl = { outcome: 'done', verifier_verdict: 'blocker_persistent', verifier_findings_count: { blocker: 2, low: 1 }, commit: 'abcdef1' }
    const { results, commands, calls } = await runWorkflow({
      tasks: [ADV_TREE], cwd: dir, implReturn: impl, stubs: { ...REVIEW_STUBS, 'land:': LANDED },
    })
    assert.ok(!calls.some(c => c.label.startsWith('land:')), 'a commit with its own blockers reached the epic branch')
    assert.ok(!calls.some(isReview), 'reviews ran on a self-blocked commit')
    assert.equal(cleanupOf(commands, 'ep-w1-t3').length, 0, 'the wave tree holds the only copy of the commit')
    const r = results[0]
    assert.equal(r.outcome, 'verifier_blocker_persistent')
    assert.equal(r.needs_orchestrator_review, false)
    assert.equal(r.commit, undefined, 'an unlanded commit must not be reported as the task commit')
    assert.equal(r.winner_commit, 'abcdef1')
    assert.equal(r.branch, 'ep-w1-t3')
    assert.equal(r.worktree, 'repo-ep-w1-t3')
    assert.equal(r.verifier_verdict, 'blocker_persistent')
    assert.deepEqual(r.verifier_findings_count, { blocker: 2, low: 1 })
    assert.deepEqual(r.report_failures, [])
    assert.equal(commands.find(c => c.label === 'report:state:3').stdout,
      'task-state --status st.md --task 3 --state blocked --verifier-verdict blocker_persistent --findings {"blocker":2,"low":1}\n')
    const note = commands.find(c => c.label === 'report:note:3')
    assert.ok(note && note.stdout.endsWith('--summary wave outcome: verifier_blocker_persistent\n'), `note was ${note && note.stdout}`)
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('adversarial: args naming half a wave tree are refused before the implementer runs', async () => {
  const dir = scratch()
  try {
    for (const [task, missing] of [[{ ...ADV, wave_worktree: 'repo-ep-w1-t3' }, 'wave_branch'], [{ ...ADV, wave_branch: 'ep-w1-t3' }, 'wave_worktree']]) {
      const { results, commands, calls, thrown } = await runWorkflow({
        tasks: [task], cwd: dir, implReturn: ADV_IMPL, stubs: { ...REVIEW_STUBS, 'land:': LANDED },
      })
      assert.deepEqual(thrown, [])
      assert.ok(!calls.some(c => /^(impl|land|cleanup):/.test(c.label) || isReview(c)), `${missing}: work ran on half a wave tree`)
      const r = results[0]
      assert.equal(r.outcome, 'blocked')
      assert.equal(r.needs_orchestrator_review, false)
      assert.equal(r.commit, undefined)
      assert.equal(commands.find(c => c.label === 'report:state:3').stdout, 'task-state --status st.md --task 3 --state blocked\n')
      const note = commands.find(c => c.label === 'report:note:3')
      assert.ok(note.stdout.endsWith(`--summary wave outcome: blocked (adversarial: args carry a wave tree without ${missing}; not dispatched)\n`), `note was ${note.stdout}`)
    }
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('adversarial + wave tree: a failed cleanup of the landed tree is recorded and carried to the return', async () => {
  const dir = scratch()
  try {
    const { results, commands } = await runWorkflow({
      tasks: [ADV_TREE], cwd: dir, implReturn: { ...ADV_IMPL, verifier_verdict: 'shrug' },
      stubs: {
        ...REVIEW_STUBS, 'land:': LANDED,
        'cleanup:3:ep-w1-t3': () => ({ exit_code: 2, stderr: 'wave-worktree error: busy' }),
        // The cleanup note's own write fails, so its failure must survive to the final return.
        'report:note:3': () => ({ exit_code: 1, stderr: 'disk full' }),
      },
    })
    const r = results[0]
    assert.equal(r.outcome, 'done', 'the code landed; a leaked tree does not undo that')
    assert.deepEqual(r.cleanup_failures, [{ branch: 'ep-w1-t3', path: 'repo-ep-w1-t3', exit_code: 2, stderr: 'wave-worktree error: busy' }])
    assert.deepEqual(r.report_failures.map(f => f.kind), ['dropped:verifier_verdict', 'note'])
    assert.equal(r.report_failures[1].stderr, 'disk full')
    assert.match(commands.find(c => c.label === 'report:state:3').stdout, /--state done --commit abcdef1\n$/)
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('adversarial + wave tree: the refuter panel counts votes after the land', async () => {
  const dir = scratch()
  try {
    for (const [refuted, outcome] of [[[true, true, false], 'verifier_blocker_persistent'], [[true, false, null], 'done']]) {
      const refute = (_p, opts) => {
        const v = refuted[Number(opts.label.split(':')[2])]
        return v === null ? null : { refuted: v, reason: 'r' }
      }
      const { results, commands, calls } = await runWorkflow({
        tasks: [ADV_TREE], cwd: dir, implReturn: ADV_IMPL, stubs: { ...REVIEW_STUBS, 'land:': LANDED, 'refute:': refute },
      })
      const tag = JSON.stringify(refuted)
      assert.equal(calls.filter(c => c.label.startsWith('refute:')).length, 3, `${tag}: the panel did not sit`)
      assert.ok(calls.findIndex(c => c.label.startsWith('refute:')) > calls.findIndex(c => c.label === 'land:3'), `${tag}: refuted before landing`)
      const r = results[0]
      assert.equal(r.outcome, outcome, tag)
      // Landed either way, so the commit is on the epic branch and is the task's commit.
      assert.equal(r.commit, 'abcdef1')
      assert.equal(r.merge_exit_code, 0)
      assert.equal(cleanupOf(commands, 'ep-w1-t3').length, 1)
      const state = commands.find(c => c.label === 'report:state:3')
      if (outcome === 'done') assert.match(state.stdout, /--state done --commit abcdef1\n$/)
      else {
        assert.equal(state.stdout, 'task-state --status st.md --task 3 --state blocked\n', `${tag}: a refuted state write carried --commit`)
        assert.ok(!calls.some(c => /^(spec|code):/.test(c.label)), `${tag}: inside reviews ran after a majority refute`)
      }
    }
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('adversarial: a verdict the schema would refuse is dropped and recorded, not written', async () => {
  const dir = scratch()
  try {
    const { results, commands } = await runWorkflow({
      tasks: [ADV], cwd: dir, implReturn: { outcome: 'done', verifier_verdict: 'shrug', commit: 'abcdef1' }, stubs: REVIEW_STUBS,
    })
    assert.doesNotMatch(commands.find(c => c.label === 'report:state:3').cmd, /--verifier-verdict/)
    assert.equal(results[0].report_failures.filter(f => f.kind === 'dropped:verifier_verdict').length, 1)
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

// --- best-of-N judge votes ---------------------------------------------------
const NO_VALID_VOTE_SUMMARY = '--summary wave outcome: blocked (best-of-N: no valid judge vote)\n'
const judgeStub = votes => (_p, opts) => votes[Number(opts.label.split(':')[2])]

test('best-of-N: no valid judge vote blocks the task, cleans every candidate, spares the others', async () => {
  const dir = scratch()
  try {
    const cases = [
      [{ winner: 2 }, { winner: 2 }, { winner: 2 }],   // a 1-based answer with 2 eligible
      [null, null, null],
      [{ winner: -1 }, { winner: 0.5 }, { winner: '0' }],
    ]
    for (const votes of cases) {
      const { results, commands, calls, thrown } = await runWorkflow({
        tasks: [{ id: 3, mode: 'best_of_n', best_of_n: 2 }, { id: 4, mode: 'single' }], cwd: dir,
        implReturn: { outcome: 'done', commit: 'abc1234' },
        stubs: { ...BEST_OF_N_STUBS, 'judge:': judgeStub(votes) },
      })
      const tag = JSON.stringify(votes)
      assert.deepEqual(thrown, [], `${tag}: the task threw`)
      const r = results.find(x => x.id === 3)
      assert.ok(r, `${tag}: the best-of-N task vanished from the return`)
      assert.equal(r.outcome, 'blocked')
      assert.equal(r.needs_orchestrator_review, false)
      assert.equal(r.commit, undefined)
      assert.deepEqual(r.cleanup_failures, [])
      assert.ok(!calls.some(c => c.label.startsWith('land:')), `${tag}: something was landed`)
      const note = commands.find(c => c.label === 'report:note:3' && c.stdout.includes('wave outcome:'))
      assert.ok(note, `${tag}: no outcome note`)
      assert.ok(note.stdout.endsWith(NO_VALID_VOTE_SUMMARY), `${tag}: note was ${note.stdout}`)
      assert.equal(commands.find(c => c.label === 'report:state:3').stdout, 'task-state --status st.md --task 3 --state blocked\n')
      for (const i of [0, 1]) assert.equal(cleanupOf(commands, `ep-w1-t3-c${i}`).length, 1, `${tag}: candidate ${i} leaked`)
      assert.equal(results.find(x => x.id === 4)?.outcome, 'done', `${tag}: the sibling task was affected`)
    }
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('best-of-N: only valid judge votes are tallied', async () => {
  const dir = scratch()
  try {
    // Two invalid votes for 2 would outvote the single valid vote for 0 if they were counted.
    for (const [votes, idx, tally] of [
      [[{ winner: 0 }, { winner: 2 }, { winner: 2 }], 0, { 0: 1 }],
      [[{ winner: 1 }, null, { winner: 1.5 }], 1, { 1: 1 }],
    ]) {
      const { results, calls, thrown } = await runWorkflow({
        tasks: [{ id: 3, mode: 'best_of_n', best_of_n: 2 }], cwd: dir,
        stubs: { ...BEST_OF_N_STUBS, 'judge:': judgeStub(votes), 'land:': LANDED },
      })
      assert.deepEqual(thrown, [])
      const r = results[0]
      assert.ok(r, `${JSON.stringify(votes)}: the task vanished`)
      assert.equal(r.winner_index, idx)
      assert.deepEqual(r.judge_votes, tally)
      assert.equal(r.branch, `ep-w1-t3-c${idx}`)
      assert.ok(calls.some(c => c.label === 'land:3'))
    }
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

// --- a landed return names only trees that still exist -----------------------
// A landed task's tree is removed before the return is built, so the return's `worktree` is null.
// A tree whose removal failed is still named, in cleanup_failures, where its path is actionable.

test('best-of-N: a landed winner return does not name the removed winner tree', async () => {
  const dir = scratch()
  try {
    for (const adversarial of [false, true]) {
      const refute = () => ({ refuted: true, reason: 'r' })
      const { results } = await runWorkflow({
        tasks: [{ id: 3, mode: 'best_of_n', best_of_n: 2, adversarial }], cwd: dir,
        stubs: { ...BEST_OF_N_STUBS, 'land:': LANDED, 'refute:': refute },
      })
      const r = results[0]
      assert.equal(r.merge_exit_code, 0, `adversarial=${adversarial}: the winner did not land`)
      assert.equal(r.outcome, adversarial ? 'verifier_blocker_persistent' : 'done')
      assert.equal(r.branch, 'ep-w1-t3-c0')
      assert.equal(r.worktree, null, `adversarial=${adversarial}: the return names the removed winner tree`)
    }
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('best-of-N: a landed winner whose tree removal failed is named in cleanup_failures only', async () => {
  const dir = scratch()
  try {
    const { results } = await runWorkflow({
      tasks: [{ id: 3, mode: 'best_of_n', best_of_n: 2 }], cwd: dir,
      stubs: { ...BEST_OF_N_STUBS, 'land:': LANDED, 'cleanup:3:ep-w1-t3-c0': () => ({ exit_code: 2, stderr: 'busy' }) },
    })
    const r = results[0]
    assert.equal(r.worktree, null)
    assert.deepEqual(r.cleanup_failures.map(f => f.path), ['repo-ep-w1-t3-c0'])
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('adversarial + wave tree: a landed return does not name the removed wave tree', async () => {
  const dir = scratch()
  try {
    for (const [refuted, outcome] of [[false, 'done'], [true, 'verifier_blocker_persistent']]) {
      const { results } = await runWorkflow({
        tasks: [ADV_TREE], cwd: dir, implReturn: ADV_IMPL,
        stubs: { ...REVIEW_STUBS, 'land:': LANDED, 'refute:': () => ({ refuted, reason: 'r' }) },
      })
      const r = results[0]
      assert.equal(r.outcome, outcome)
      assert.equal(r.merge_exit_code, 0)
      assert.equal(r.branch, 'ep-w1-t3')
      assert.equal(r.worktree, null, `${outcome}: the return names the removed wave tree`)
    }
    // A failed removal keeps naming the tree, in cleanup_failures.
    const { results } = await runWorkflow({
      tasks: [ADV_TREE], cwd: dir, implReturn: ADV_IMPL,
      stubs: { ...REVIEW_STUBS, 'land:': LANDED, 'cleanup:3:ep-w1-t3': () => ({ exit_code: 2, stderr: 'busy' }) },
    })
    assert.equal(results[0].worktree, null)
    assert.deepEqual(results[0].cleanup_failures.map(f => f.path), ['repo-ep-w1-t3'])
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

// --- adversarial land retry -------------------------------------------------
// Group tasks land concurrently, so an adversarial land is retried once, as best-of-N's is.

test('adversarial + wave tree: a land refused once and accepted on the retry is a landing', async () => {
  const dir = scratch()
  try {
    let n = 0
    const { results, commands, calls } = await runWorkflow({
      tasks: [ADV_TREE], cwd: dir, implReturn: ADV_IMPL,
      stubs: { ...REVIEW_STUBS, 'land:': () => (n++ === 0 ? { merge_exit_code: 2, success: false, stderr: FF_REFUSED } : LANDED()) },
    })
    assert.deepEqual(calls.filter(c => c.label.startsWith('land:')).map(c => c.label), ['land:3', 'land:3:retry'])
    const r = results[0]
    assert.equal(r.outcome, 'done')
    assert.equal(r.commit, 'abcdef1')
    assert.equal(r.merge_exit_code, 0)
    assert.ok(calls.some(c => c.label === 'spec:3'), 'a landed commit skipped review')
    assert.equal(cleanupOf(commands, 'ep-w1-t3').length, 1, 'the landed wave tree was not removed')
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('adversarial + wave tree: a land refused twice blocks after exactly one retry', async () => {
  const dir = scratch()
  try {
    const { results, commands, calls } = await runWorkflow({
      tasks: [ADV_TREE], cwd: dir, implReturn: ADV_IMPL,
      stubs: { ...REVIEW_STUBS, 'land:': () => ({ merge_exit_code: 2, success: false, stderr: FF_REFUSED }) },
    })
    assert.deepEqual(calls.filter(c => c.label.startsWith('land:')).map(c => c.label), ['land:3', 'land:3:retry'])
    assert.equal(results[0].outcome, 'blocked')
    assert.equal(results[0].worktree, 'repo-ep-w1-t3', 'an unlanded tree is kept and still named')
    assert.equal(cleanupOf(commands, 'ep-w1-t3').length, 0)
  } finally { rmSync(dir, { recursive: true, force: true }) }
})
