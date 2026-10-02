// test/extract-edges.test.js — per-language, per-rel edge extraction (epic 702).
// extractFileEdges returns PER-FILE raw facts; cross-file resolution is emit-graph's job.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { loadParsers } from '../grammars.js';
import { extractFileEdges, ROUTE_PATTERNS } from '../extract-edges.js';
import { extractSymbols } from '../extract-symbols.js';

const here = dirname(fileURLToPath(import.meta.url));
// fixture repo lives OUTSIDE test/ — node --test executes .js/.ts files under test/ dirs
const read = (f) => readFileSync(join(here, '..', 'fixtures', 'edges-repo', f), 'utf8');

async function edges(lang, fixture, file) {
  const parsers = await loadParsers();
  return extractFileEdges({ lang, parsers, source: read(fixture), file });
}

// ---------------------------------------------------------------- python
test('python: plain and from-imports are collected with specs', async () => {
  const e = await edges('python', 'app/main.py', 'app/main.py');
  const specs = e.importDecls.map((d) => d.spec);
  assert.ok(specs.includes('app.service'), `missing app.service in ${specs}`);
  assert.ok(specs.includes('app.util'), `missing app.util in ${specs}`);
});

test('python: from-import records imported names with local aliases', async () => {
  const e = await edges('python', 'app/main.py', 'app/main.py');
  const svc = e.importDecls.find((d) => d.spec === 'app.service');
  const locals = svc.names.map((n) => n.local);
  assert.deepEqual(locals.sort(), ['Service', 'compute']);
});

test('python: import-as records the module alias', async () => {
  const e = await edges('python', 'app/main.py', 'app/main.py');
  const util = e.importDecls.find((d) => d.spec === 'app.util');
  assert.equal(util.moduleAlias, 'u');
});

test('python: defines links class to its direct methods', async () => {
  const e = await edges('python', 'app/service.py', 'app/service.py');
  const pairs = e.defines.map((d) => `${d.container.name}->${d.member.name}`);
  assert.ok(pairs.includes('Service->run'));
  assert.ok(pairs.includes('Service->helper'));
});

test('python: same-file identifier call is captured with its enclosing caller', async () => {
  const e = await edges('python', 'app/main.py', 'app/main.py');
  const c = e.calls.find((x) => x.calleeName === 'top');
  assert.ok(c, 'call to top() not captured');
  assert.equal(c.caller.name, 'local_helper');
});

test('python: module-level call has caller=null (module attribution)', async () => {
  const e = await edges('python', 'app/main.py', 'app/main.py');
  const c = e.calls.find((x) => x.calleeName === 'unknown_call');
  assert.ok(c, 'module-level call not captured');
  assert.equal(c.caller, null);
});

test('python: alias attribute call carries the object name', async () => {
  const e = await edges('python', 'app/main.py', 'app/main.py');
  const c = e.calls.find((x) => x.calleeName === 'util_fn');
  assert.equal(c.calleeObject, 'u');
});

test('python: self.method() call is captured with object self', async () => {
  const e = await edges('python', 'app/service.py', 'app/service.py');
  const c = e.calls.find((x) => x.calleeName === 'helper');
  assert.equal(c.calleeObject, 'self');
  assert.equal(c.caller.name, 'run');
});

test('python: route decorator yields a route fact naming the handler', async () => {
  const e = await edges('python', 'app/main.py', 'app/main.py');
  assert.equal(e.routes.length, 1);
  assert.equal(e.routes[0].handlerName, 'list_items');
  assert.equal(e.routes[0].pattern, 'py-decorator-route');
});

// ---------------------------------------------------------------- typescript
test('ts: relative import spec + named imports', async () => {
  const e = await edges('typescript', 'web/index.ts', 'web/index.ts');
  const d = e.importDecls.find((x) => x.spec === './util');
  assert.ok(d, 'relative import not captured');
  assert.deepEqual(d.names.map((n) => n.local), ['fmt']);
});

test('ts: defines links class to method (member not in extract-symbols output)', async () => {
  const e = await edges('typescript', 'web/index.ts', 'web/index.ts');
  const d = e.defines.find((x) => x.container.name === 'View');
  assert.equal(d.member.name, 'draw');
  // supplemental: the method must carry position info so emit-graph can mint an entity
  assert.ok(d.member.line > 0);
  assert.equal(d.member.kind, 'method');
});

test('ts: call inside a method attributes the method as caller', async () => {
  const e = await edges('typescript', 'web/index.ts', 'web/index.ts');
  const c = e.calls.find((x) => x.calleeName === 'render');
  assert.equal(c.caller.name, 'draw');
});

test('js/express: member route with identifier handler is a route fact', async () => {
  const e = await edges('typescript', 'srv/server.js', 'srv/server.js');
  const r = e.routes.find((x) => x.handlerName === 'handler');
  assert.ok(r, 'express route not captured');
  assert.equal(r.pattern, 'express-member-route');
});

test('js/express: inline-callback route is counted as unresolved, not guessed', async () => {
  const e = await edges('typescript', 'srv/server.js', 'srv/server.js');
  assert.equal(e.skipped.routes_unresolved, 1);
});

// ---------------------------------------------------------------- go
test('go: defines links receiver type to method', async () => {
  const e = await edges('go', 'pkg/pkg.go', 'pkg/pkg.go');
  const d = e.defines.find((x) => x.container.name === 'Store');
  assert.equal(d.member.name, 'Get');
});

test('go: same-file call from a method is captured', async () => {
  const e = await edges('go', 'pkg/pkg.go', 'pkg/pkg.go');
  const c = e.calls.find((x) => x.calleeName === 'Run');
  assert.equal(c.caller.name, 'Get');
});

test('go: imports are counted as unresolved (no module-path resolution)', async () => {
  const parsers = await loadParsers();
  const src = 'package x\n\nimport "fmt"\n\nfunc F() { fmt.Println(1) }\n';
  const e = extractFileEdges({ lang: 'go', parsers, source: src, file: 'x/x.go' });
  assert.equal(e.importDecls.length, 0);
  assert.equal(e.skipped.imports_unresolved, 1);
});

// ---------------------------------------------------------------- misc contract
test('route pattern table is per-language and extensible', () => {
  assert.ok(Array.isArray(ROUTE_PATTERNS.python));
  assert.ok(Array.isArray(ROUTE_PATTERNS.typescript));
  assert.ok(ROUTE_PATTERNS.python.every((p) => typeof p.name === 'string'));
});

test('extract-symbols now reports end_line covering the definition body', async () => {
  const parsers = await loadParsers();
  const syms = extractSymbols({
    lang: 'python', parsers, source: read('app/service.py'), file: 'app/service.py',
  });
  const cls = syms.find((s) => s.kind === 'class' && s.name === 'Service');
  assert.ok(cls.end_line > cls.line, `end_line ${cls.end_line} must be > line ${cls.line}`);
});

// ------------------------------------------- adversarial-review remediation
test('python: self-call records its enclosing class (finding 3a)', async () => {
  const e = await edges('python', 'app/cross_class.py', 'app/cross_class.py');
  const c = e.calls.find((x) => x.calleeObject === 'self' && x.calleeName === 'helper');
  assert.ok(c, 'self.helper() call not captured');
  assert.equal(c.enclosingClass.name, 'Beta');
  assert.ok(c.enclosingClass.line > 0);
});

test('python: parameter shadowing drops the call and counts it (finding 3b)', async () => {
  const e = await edges('python', 'app/shadow.py', 'app/shadow.py');
  assert.ok(!e.calls.some((x) => x.calleeName === 'shadowed_fn'),
    'shadowed call must not be recorded');
  assert.equal(e.skipped.calls_shadowed, 1);
});

test('python: local rebinding drops the call and counts it (finding 3b)', async () => {
  const e = await edges('python', 'app/local_shadow.py', 'app/local_shadow.py');
  assert.ok(!e.calls.some((x) => x.calleeName === 'rebound_fn'),
    'locally rebound call must not be recorded');
  assert.ok(e.skipped.calls_shadowed >= 1);
  // the factory call itself is still captured (it is not shadowed)
  assert.ok(e.calls.some((x) => x.calleeName === 'compute_factory'));
});

test('ts: parameter shadowing drops the call and counts it (finding 3b)', async () => {
  const parsers = await loadParsers();
  const src = 'export function fmt(s: string): string { return s; }\n'
    + 'export function outer(fmt: (x: string) => string): string { return fmt("x"); }\n';
  const e = extractFileEdges({ lang: 'typescript', parsers, source: src, file: 'w/s.ts' });
  assert.ok(!e.calls.some((x) => x.calleeName === 'fmt'));
  assert.equal(e.skipped.calls_shadowed, 1);
});

test('go: parameter shadowing drops the call and counts it (finding 3b)', async () => {
  const parsers = await loadParsers();
  const src = 'package x\n\nfunc Run() int { return 1 }\n\n'
    + 'func Outer(Run func() int) int { return Run() }\n';
  const e = extractFileEdges({ lang: 'go', parsers, source: src, file: 'x/x.go' });
  assert.ok(!e.calls.some((x) => x.calleeName === 'Run'));
  assert.equal(e.skipped.calls_shadowed, 1);
});

// -------------------------------------- round-2 remediation (finding R2-1)
test('attribute write does not shadow a same-named function', async () => {
  const parsers = await loadParsers();
  const src = 'def refresh():\n    return 1\n\n\nclass C:\n'
    + '    def m(self):\n        self.refresh = None\n        return refresh()\n';
  const e = extractFileEdges({ lang: 'python', parsers, source: src, file: 'a/r.py' });
  assert.ok(e.calls.some((x) => x.calleeName === 'refresh' && x.calleeObject === null),
    'true call to refresh() was dropped');
  assert.equal(e.skipped.calls_shadowed, 0);
});

test('subscript write binds nothing — the index identifier is a READ', async () => {
  const parsers = await loadParsers();
  const src = 'def key():\n    return 1\n\n\ndef f(cache):\n'
    + '    cache[key] = 1\n    return key()\n';
  const e = extractFileEdges({ lang: 'python', parsers, source: src, file: 'a/s.py' });
  assert.ok(e.calls.some((x) => x.calleeName === 'key' && x.calleeObject === null),
    'true call to key() was dropped');
  assert.equal(e.skipped.calls_shadowed, 0);
});

test('ts member write does not shadow the receiver', async () => {
  const parsers = await loadParsers();
  const src = "import * as util from './u';\nexport function f() {\n"
    + '  util.mutated = 1;\n  return util.helper();\n}\n';
  const e = extractFileEdges({ lang: 'typescript', parsers, source: src, file: 'w/m.ts' });
  assert.ok(e.calls.some((x) => x.calleeObject === 'util' && x.calleeName === 'helper'),
    'true call util.helper() was dropped');
  assert.equal(e.skipped.calls_shadowed, 0);
});

test('destructuring assignment still shadows (guard for the R2-1 narrowing)', async () => {
  const parsers = await loadParsers();
  const src = 'def pick():\n    return 1\n\n\ndef f():\n'
    + '    pick, other = get()\n    return pick()\n';
  const e = extractFileEdges({ lang: 'python', parsers, source: src, file: 'a/t.py' });
  assert.ok(!e.calls.some((x) => x.calleeName === 'pick'));
  assert.equal(e.skipped.calls_shadowed, 1);
});
