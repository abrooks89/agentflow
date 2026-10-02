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
async function runWorkflow({ tasks, implReturn, cwd, statusPath = 'st.md' }) {
  const commands = []
  const agent = async (prompt, opts) => {
    const label = opts.label
    if (label.startsWith('impl:')) return implReturn
    let cmd = null
    if (label.startsWith('report:')) cmd = prompt.slice(prompt.indexOf(REPORT_PREFIX) + REPORT_PREFIX.length)
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
  const out = await compile()(args, agent, () => {}, fns => Promise.all(fns.map(f => f())))
  return { ...out, commands }
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
