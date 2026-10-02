// test/coverage.test.js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { checkCoverage } from '../coverage.js';

const inv = {
  symbols: [
    { file: 'canonical/app.py', area: 'canonical', kind: 'endpoint', name: 'create_project', line: 4 },
    { file: 'shared/util.go', area: 'shared', kind: 'function', name: 'Merge', line: 2 },
  ],
};

test('all covered → ok', () => {
  const report = {
    pages: [{ source_paths: ['canonical/app.py'] }, { source_paths: ['shared/util.go'] }],
    covered: [{ area: 'canonical', name: 'create_project', line: 4 }, { area: 'shared', name: 'Merge', line: 2 }],
    excluded: [],
  };
  const r = checkCoverage(inv, report);
  assert.equal(r.ok, true);
  assert.equal(r.uncovered.length, 0);
});

test('missing symbol-level coverage → not ok', () => {
  const report = {
    pages: [{ source_paths: ['canonical/app.py'] }, { source_paths: ['shared/util.go'] }],
    covered: [{ area: 'canonical', name: 'create_project', line: 4 }],
    excluded: [],
  };
  const r = checkCoverage(inv, report);
  assert.equal(r.ok, false);
  assert.equal(r.uncovered[0].name, 'Merge');
});

test('excluded with reason counts as satisfied', () => {
  const report = {
    pages: [{ source_paths: ['canonical/app.py'] }, { source_paths: ['shared/util.go'] }],
    covered: [{ area: 'canonical', name: 'create_project', line: 4 }],
    excluded: [{ area: 'shared', name: 'Merge', line: 2, reason: 'trivial helper' }],
  };
  assert.equal(checkCoverage(inv, report).ok, true);
});

test('vendored-path symbols are exempt (taxonomy: vendored code not documented per-symbol)', () => {
  // epic 703: vendor detection is by path convention (vendor/, vendored/,
  // third_party/), like tests — the area layer that used to carry it is gone.
  const vendorInv = {
    symbols: [
      { file: 'vendor/enginekit/pkg/a.go', kind: 'function', name: 'Thing', line: 10 },
      { file: 'third_party/lib/b.go', kind: 'function', name: 'Other', line: 3 },
      { file: 'canonical/app.py', kind: 'endpoint', name: 'create_project', line: 4 },
    ],
  };
  const report = {
    pages: [{ source_paths: ['canonical/app.py'] }],
    covered: [{ name: 'create_project', line: 4 }],
    excluded: [],
  };
  const r = checkCoverage(vendorInv, report);
  assert.equal(r.ok, true, 'vendored-path symbols require no coverage evidence');
  assert.deepEqual(r.uncovered, []);
  assert.deepEqual(r.fileGaps, []);
});

test('near-miss vendor names are NOT exempt (vendor must be a directory segment)', () => {
  const inv2 = {
    symbols: [
      { file: 'shared/vendor_tools.py', kind: 'function', name: 'V', line: 1 },
    ],
  };
  const r = checkCoverage(inv2, { pages: [], covered: [], excluded: [] });
  assert.equal(r.ok, false, 'vendor_tools.py is product code, not vendored');
});

test('test-file symbols are exempt (test code is not a documented entity type)', () => {
  const inv = {
    symbols: [
      { file: 'canonical/tests/test_sync_endpoint.py', area: 'canonical', kind: 'function', name: 'test_push', line: 5 },
      { file: 'orchestrator/test_runner.py', area: 'orchestrator', kind: 'function', name: 'test_run', line: 9 },
      { file: 'canonical/tests/conftest.py', area: 'canonical', kind: 'function', name: 'fixture_db', line: 3 },
      { file: 'app-runtime/reaper_test.go', area: 'app-runtime', kind: 'function', name: 'TestReap', line: 12 },
      { file: 'app/frontend/src/components/RunCompare.test.tsx', area: 'app-frontend', kind: 'component', name: 'RunCompareTest', line: 7 },
      { file: 'canonical/app.py', area: 'canonical', kind: 'endpoint', name: 'create_project', line: 4 },
    ],
  };
  const report = {
    pages: [{ source_paths: ['canonical/app.py'] }],
    covered: [{ area: 'canonical', name: 'create_project', line: 4 }],
    excluded: [],
  };
  const r = checkCoverage(inv, report);
  assert.equal(r.ok, true, 'test-file symbols require no coverage evidence');
  assert.deepEqual(r.uncovered, []);
  assert.deepEqual(r.fileGaps, []);
});

test('testfakes directories and generated protobuf code are exempt (test infra / codegen, not documented entities)', () => {
  const inv = {
    symbols: [
      { file: 'app-runtime/testfakes/fake_provider.go', area: 'app-runtime', kind: 'function', name: 'FakeProvider', line: 8 },
      { file: 'shared/rpc/service.pb.go', area: 'shared', kind: 'type', name: 'SyncRequest', line: 20 },
      { file: 'canonical/gen/sync_pb2.py', area: 'canonical', kind: 'class', name: 'SyncRequest', line: 5 },
      { file: 'canonical/gen/sync_pb2_grpc.py', area: 'canonical', kind: 'class', name: 'SyncStub', line: 11 },
      { file: 'canonical/app.py', area: 'canonical', kind: 'endpoint', name: 'create_project', line: 4 },
    ],
  };
  const report = {
    pages: [{ source_paths: ['canonical/app.py'] }],
    covered: [{ area: 'canonical', name: 'create_project', line: 4 }],
    excluded: [],
  };
  const r = checkCoverage(inv, report);
  assert.equal(r.ok, true, 'testfakes and protobuf codegen require no coverage evidence');
  assert.deepEqual(r.uncovered, []);
  assert.deepEqual(r.fileGaps, []);
});

test('suffix-style tests, e2e/testdrivers trees, migrations, and __init__ markers are exempt', () => {
  const inv = {
    symbols: [
      { file: 'packs/app-engine/harness/python/base_hand_test.py', area: 'packs', kind: 'function', name: 'test_run', line: 3 },
      { file: 'canonical/deploy/migration_merge_e2e.py', area: 'canonical', kind: 'function', name: 'e2e_flow', line: 5 },
      { file: 'scripts/e2e/plan_e2e.py', area: 'infra', kind: 'function', name: 'run', line: 2 },
      { file: 'shared/app-harness-go/testdrivers/heartbeat_idle.go', area: 'shared', kind: 'function', name: 'main', line: 1 },
      { file: 'alembic.legacy/versions/abc_add_col.py', area: 'infra', kind: 'function', name: 'upgrade', line: 9 },
      { file: 'canonical/__init__.py', area: 'canonical', kind: 'function', name: 'x', line: 1 },
      { file: 'canonical/app.py', area: 'canonical', kind: 'endpoint', name: 'create_project', line: 4 },
      // A validation CHECK named e2e_pipeline is product code (no /e2e/ dir, no _e2e suffix) — NOT exempt.
      { file: 'canonical/validation/checks/e2e_pipeline.py', area: 'canonical', kind: 'function', name: 'check', line: 7 },
    ],
  };
  const report = { pages: [{ source_paths: ['canonical/app.py'] }], covered: [{ area: 'canonical', name: 'create_project', line: 4 }], excluded: [] };
  const r = checkCoverage(inv, report);
  assert.deepEqual(r.uncovered.map((s) => s.file), ['canonical/validation/checks/e2e_pipeline.py'], 'only the product check remains uncovered');
});

test('near-miss names for the testfakes/protobuf exemptions are NOT exempt', () => {
  const inv = {
    symbols: [
      // 'testfakes' must be a directory segment, not a filename substring
      { file: 'shared/testfakes.go', area: 'shared', kind: 'function', name: 'A', line: 1 },
      // '.pb.go' requires the literal dot — 'superb.go' is real code
      { file: 'shared/superb.go', area: 'shared', kind: 'function', name: 'B', line: 1 },
      // '_pb2.py' requires the underscore-suffix form
      { file: 'canonical/mypb2.py', area: 'canonical', kind: 'function', name: 'C', line: 1 },
    ],
  };
  const r = checkCoverage(inv, { pages: [], covered: [], excluded: [] });
  assert.equal(r.ok, false);
  assert.deepEqual(r.uncovered.map((s) => s.name).sort(), ['A', 'B', 'C'], 'all three still require coverage');
});

test('non-test file containing the word test is NOT exempt', () => {
  const inv = {
    symbols: [
      { file: 'canonical/latest_report.py', area: 'canonical', kind: 'function', name: 'latest', line: 2 },
    ],
  };
  const r = checkCoverage(inv, { pages: [], covered: [], excluded: [] });
  assert.equal(r.ok, false, 'latest_report.py must still require coverage');
});

test('covered entry matches by name+file when line is absent', () => {
  const report = {
    pages: [],
    covered: [
      { name: 'create_project', file: 'canonical/app.py' },
      { name: 'Merge', file: 'shared/util.go' },
    ],
    excluded: [],
  };
  const r = checkCoverage(inv, report);
  assert.deepEqual(r.uncovered, [], 'name+file matching covers both symbols');
});

test('update path: covered symbol needs no file-level page evidence (edited-page fix)', () => {
  // Symbols covered by EDITING existing pages: report.pages carries only CREATED
  // pages, so neither file appears there. covered must still fully satisfy —
  // no uncovered, and crucially no file-level gap.
  const report = {
    pages: [], // update run created no pages
    covered: [
      { area: 'canonical', name: 'create_project', line: 4 },
      { area: 'shared', name: 'Merge', line: 2 },
    ],
    excluded: [],
  };
  const r = checkCoverage(inv, report);
  assert.equal(r.ok, true, 'covered symbols satisfied without page evidence');
  assert.deepEqual(r.fileGaps, [], 'no file-level gap for explicitly covered symbols');
});

test('fileGap fires only for symbols in neither covered nor excluded whose file has no page', () => {
  const report = {
    pages: [{ source_paths: ['canonical/app.py'] }], // app.py documented at file level
    covered: [],
    excluded: [],
  };
  const r = checkCoverage(inv, report);
  assert.equal(r.ok, false);
  // create_project: uncovered (no symbol evidence) but NOT a fileGap (file documented)
  // Merge: uncovered AND fileGap (no evidence at all)
  assert.deepEqual(r.uncovered.map((s) => s.name).sort(), ['Merge', 'create_project']);
  assert.deepEqual(r.fileGaps.map((s) => s.name), ['Merge']);
});

test('fileLevelOnly mode suppresses uncovered but keeps fileGaps', () => {
  const report = { pages: [{ source_paths: ['canonical/app.py'] }], covered: [], excluded: [] };
  const r = checkCoverage(inv, report, { fileLevelOnly: true });
  assert.deepEqual(r.uncovered, [], 'no symbol-level signal in file-level mode');
  assert.deepEqual(r.fileGaps.map((s) => s.name), ['Merge'], 'file-level gap still reported');
  assert.equal(r.ok, false);
});

test('backslash paths normalize for both page evidence and covered file matching', () => {
  const report = {
    pages: [{ source_paths: ['canonical\\app.py'] }],
    covered: [{ name: 'Merge', file: 'shared\\util.go' }],
    excluded: [],
  };
  const r = checkCoverage(inv, report);
  assert.deepEqual(r.fileGaps, [], 'backslash page path matches');
  assert.deepEqual(r.uncovered.map((s) => s.name), ['create_project'], 'only the file-evidenced-but-unclaimed symbol remains uncovered');
});
