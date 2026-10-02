// coverage.js — verify every inventoried symbol is documented or explicitly excluded.
//
// Vendored third-party code is intentionally NOT documented symbol-by-symbol
// (taxonomy design decision), so vendored-path symbols are exempt here. With
// the hand-written area layer deleted (epic 703), vendoring is detected by
// path convention — a vendor/, vendored/, or third_party/ directory segment —
// exactly like the test-file exemption below.
//
// Evidence semantics:
//   - report.covered / report.excluded: symbol-level evidence. An inventory symbol
//     matches an entry by name+line or by name+file. Symbol-level evidence wins
//     outright — update runs cover symbols by EDITING existing pages, and edited
//     pages never appear in report.pages, so covered symbols must not require
//     file-level page evidence.
//   - report.pages[].source_paths: file-level evidence (CREATED pages only).
//   - uncovered: symbols with no symbol-level evidence — the strict per-symbol
//     gate for init/update workflow runs. Suppressed in fileLevelOnly mode
//     (graph-check: the wiki carries no per-symbol covered record, so the absence
//     of one is not a signal there).
//   - fileGaps: symbols with NEITHER symbol-level NOR file-level evidence.

const normPath = (p) => String(p).replace(/\\/g, '/');

// Matcher over covered/excluded entries: a symbol hits by name+line or name+file.
function buildMatcher(entries) {
  const byNameLine = new Set();
  const byNameFile = new Set();
  for (const e of entries || []) {
    if (!e || e.name == null) continue;
    if (e.line != null) byNameLine.add(`${e.name}::${e.line}`);
    if (e.file != null) byNameFile.add(`${e.name}::${normPath(e.file)}`);
  }
  return (s) =>
    byNameLine.has(`${s.name}::${s.line}`) || byNameFile.has(`${s.name}::${normPath(s.file)}`);
}

// Test code is not a documented entity type (no 'test' entity in the taxonomy) —
// like vendor, test-file symbols are exempt from coverage evidence. The same
// rationale extends to test fakes (/testfakes/ directories) and generated
// protobuf code (*.pb.go, *_pb2.py, *_pb2_grpc.py): test infrastructure and
// codegen output are not documented entities either.
// Directory conventions (tests/, __tests__/, testfakes/, testdrivers/, e2e/) and
// filename conventions (test_*.py, *_test.py|go, *_e2e.py, conftest, *.test|spec.[jt]sx,
// generated protobuf). Covers Python suffix-style tests and e2e trees the earlier
// prefix-only pattern missed.
const TEST_FILE_RE = /(?:^|\/)(?:tests?|__tests__|testfakes|testdrivers|e2e)\/|(?:^|\/)test_[^/]+\.py$|_test\.(?:py|go)$|_e2e\.py$|conftest\.py$|\.(?:test|spec)\.[jt]sx?$|\.pb\.go$|_pb2(?:_grpc)?\.py$/;

// Structural non-entities, exempt on the same rationale as tests/vendor/codegen:
// DB migrations (Alembic `env.py` + `versions/` revision scripts, any `migrations/`
// tree) are generated upgrade/downgrade boilerplate, not documented entities; and
// `__init__.py` package markers carry no entity of their own (any real re-export a
// caller depends on is documented on the module it re-exports).
const NOISE_FILE_RE = /(?:^|\/)alembic[.\w-]*\/|(?:^|\/)migrations?\/|(?:^|\/)__init__\.py$/;

// Vendored third-party trees, by directory convention (epic 703 — replaces
// the deleted area-map's 'vendor' bucket).
const VENDOR_FILE_RE = /(?:^|\/)(?:vendor|vendored|third_party)\//;

export function isTestFile(file) {
  return TEST_FILE_RE.test(normPath(file));
}

export function isVendorFile(file) {
  return VENDOR_FILE_RE.test(normPath(file));
}

export function isNoiseFile(file) {
  return NOISE_FILE_RE.test(normPath(file));
}

export function checkCoverage(inventory, report, { fileLevelOnly = false } = {}) {
  const isCovered = buildMatcher(report.covered);
  const isExcluded = buildMatcher(report.excluded);
  const fileSet = new Set();
  for (const pg of report.pages || []) for (const fp of pg.source_paths || []) fileSet.add(normPath(fp));

  const uncovered = [];
  const fileGaps = [];
  let considered = 0;
  for (const s of inventory.symbols || []) {
    if (isVendorFile(s.file)) continue; // vendored code exempt by taxonomy
    if (isTestFile(s.file)) continue; // test code exempt: not a documented entity type
    if (isNoiseFile(s.file)) continue; // migrations / __init__ markers: structural non-entities
    considered++;
    if (isCovered(s) || isExcluded(s)) continue; // symbol-level evidence satisfies outright
    if (!fileLevelOnly) uncovered.push(s);
    if (!fileSet.has(normPath(s.file))) fileGaps.push(s);
  }
  const ok = uncovered.length === 0 && fileGaps.length === 0;
  const summary = ok
    ? `coverage OK: ${considered} symbols all documented or excluded`
    : `coverage GAP: ${uncovered.length} undocumented symbol(s), ${fileGaps.length} file-level gap(s)`;
  return { ok, uncovered, fileGaps, summary };
}
