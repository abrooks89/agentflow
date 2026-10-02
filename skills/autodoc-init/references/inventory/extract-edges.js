// extract-edges.js — per-file raw edge facts for the autodoc graph (epic 702).
//
// This module extracts PER-FILE facts only; cross-file resolution (imports →
// files, imported-name call targets) is emit-graph.js's job, because it needs
// the whole walked file set. Everything here is deterministic AST reading:
// an unresolvable or ambiguous site is SKIPPED AND COUNTED, never guessed.
//
// Returned shape (all positions 1-based):
//   {
//     importDecls: [{ spec, level, names: [{imported, local}], moduleAlias, line }],
//     defines:     [{ container: {name, line|null, kind|null},
//                     member: {name, line, end_line, kind}, line }],
//     calls:       [{ caller: {name, line}|null, calleeObject|null, calleeName,
//                     enclosingClass: {name, line}|undefined,  // self-calls only
//                     line }],
//     routes:      [{ handlerName|null, handlerLine|null, pattern, line }],
//     skipped:     { imports_unresolved, routes_unresolved, calls_shadowed }
//   }
//
// Shadowing honesty (adversarial-review finding 3b): a call whose callee name
// is bound as a PARAMETER or LOCAL ASSIGNMENT anywhere in an enclosing
// function scope is dropped here and counted in skipped.calls_shadowed —
// resolving it to the same-named module-level symbol would be a false edge at
// confidence 1.0. Detection is an over-approximation in the safe direction
// (an assignment AFTER the call site still counts as shadowed: we skip, never
// guess). Known residual false-positive classes that are NOT detected —
// module-level rebinding after a def, dynamic/monkey patching, `del` — are
// documented in emit-graph.js's header.
//
// `level` on importDecls: python relative-import dot count (0 = absolute).
// `caller: null` means the call happens at module level (attributed to the
// module entity by emit-graph). Go imports are package paths, not files — we
// do not attempt module-path resolution, so they are counted unresolved here.

// Route-declaration pattern table. Per-language, extensible: add a pattern
// object and a matching branch; every pattern has a dedicated test.
export const ROUTE_PATTERNS = {
  python: [
    {
      name: 'py-decorator-route',
      // decorator text like @app.get("/x"), @router.post(...), @bp.route("/y")
      re: /\.(get|post|put|patch|delete|websocket|route)\s*\(/i,
    },
  ],
  typescript: [
    {
      name: 'express-member-route',
      methods: new Set(['get', 'post', 'put', 'patch', 'delete', 'all', 'use']),
    },
  ],
};
// tsx and js files parse with the typescript/tsx grammars; same route patterns.
ROUTE_PATTERNS.tsx = ROUTE_PATTERNS.typescript;

// Walk every node of a tree-sitter tree depth-first.
function* walkNodes(root) {
  const stack = [root];
  while (stack.length) {
    const node = stack.pop();
    yield node;
    for (let i = node.namedChildCount - 1; i >= 0; i--) {
      stack.push(node.namedChild(i));
    }
  }
}

const line = (node) => node.startPosition.row + 1;
const endLine = (node) => node.endPosition.row + 1;

// Nearest enclosing named function/method of `node`, or null (module level).
// Per-language definition node types that can act as a caller.
const CALLER_TYPES = {
  python: new Set(['function_definition']),
  go: new Set(['function_declaration', 'method_declaration']),
  typescript: new Set(['function_declaration', 'method_definition', 'variable_declarator']),
  tsx: new Set(['function_declaration', 'method_definition', 'variable_declarator']),
};

function enclosingCaller(node, lang) {
  const types = CALLER_TYPES[lang];
  for (let p = node.parent; p; p = p.parent) {
    if (!types.has(p.type)) continue;
    const nameNode = p.childForFieldName('name');
    if (nameNode) return { name: nameNode.text, line: line(nameNode) };
  }
  return null;
}

// Nearest enclosing python class of a node (for self-call scoping, finding 3a).
function enclosingClass(node) {
  for (let p = node.parent; p; p = p.parent) {
    if (p.type !== 'class_definition') continue;
    const nameNode = p.childForFieldName('name');
    if (nameNode) return { name: nameNode.text, line: line(nameNode) };
  }
  return null;
}

// ---------------------------------------------------------------- shadowing
// Function-scope node types per language (scopes whose params/locals shadow).
const FUNC_SCOPE_TYPES = {
  python: new Set(['function_definition']),
  typescript: new Set(['function_declaration', 'method_definition',
    'arrow_function', 'function_expression', 'generator_function_declaration']),
  go: new Set(['function_declaration', 'method_declaration', 'func_literal']),
};
FUNC_SCOPE_TYPES.tsx = FUNC_SCOPE_TYPES.typescript;

// Collect parameter names declared by a function-scope node.
function collectParamNames(fnNode, lang, out) {
  if (lang === 'typescript' || lang === 'tsx') {
    const single = fnNode.childForFieldName('parameter'); // arrow x => ...
    if (single && single.type === 'identifier') out.add(single.text);
  }
  const params = fnNode.childForFieldName('parameters');
  if (!params) return;
  for (const p of params.namedChildren) {
    if (p.type === 'identifier') {
      out.add(p.text);
    } else if (lang === 'python') {
      // default/typed/splat parameter shapes: the bound name is the name
      // field or the first identifier child
      const nameNode = p.childForFieldName('name');
      if (nameNode && nameNode.type === 'identifier') out.add(nameNode.text);
      else if (p.namedChildCount > 0 && p.namedChild(0).type === 'identifier') {
        out.add(p.namedChild(0).text);
      }
    } else if (lang === 'typescript' || lang === 'tsx') {
      const pattern = p.childForFieldName('pattern');
      if (pattern && pattern.type === 'identifier') out.add(pattern.text);
    } else if (lang === 'go' && p.type === 'parameter_declaration') {
      for (const c of p.namedChildren) {
        if (c.type === 'identifier') out.add(c.text);
      }
    }
  }
}

// Identifiers bound by assignment-like statements inside `scopeNode`, without
// descending into nested function/class scopes (their locals do not shadow
// here). Nested function/class definition NAMES are deliberately NOT treated
// as shadowing: unlike a parameter or assignment they bind to a resolvable
// definition that the extractor emits as an entity, and the path:kind:name
// identity model already collapses same-named defs in one file — so a call
// through that name lands on the right entity, not a guess.
function collectLocalBindings(scopeNode, lang, out) {
  const nested = new Set([...FUNC_SCOPE_TYPES[lang],
    'class_definition', 'class_declaration']);
  // LHS shapes that actually BIND names: a bare identifier or a destructuring
  // pattern. Attribute/member/subscript writes (self.x = 1, util.x = 1,
  // d[k] = v) bind NOTHING — treating them as rebinds silently dropped true
  // call edges, and a subscript's index identifier is a READ (finding R2-1).
  const PATTERN_TYPES = new Set([
    'pattern_list', 'tuple_pattern', 'list_pattern', // python
    'expression_list',                               // go
    'array_pattern', 'object_pattern',               // ts/tsx
  ]);
  const addLeft = (leftNode) => {
    if (!leftNode) return;
    if (leftNode.type === 'identifier') {
      out.add(leftNode.text);
      return;
    }
    if (!PATTERN_TYPES.has(leftNode.type)) return; // attribute/member/subscript: no binding
    for (const c of leftNode.namedChildren) {
      if (c.type === 'identifier' || c.type === 'shorthand_property_identifier_pattern') {
        out.add(c.text);
      }
    }
  };
  const stack = [];
  for (let i = 0; i < scopeNode.namedChildCount; i++) stack.push(scopeNode.namedChild(i));
  while (stack.length) {
    const n = stack.pop();
    if (nested.has(n.type)) {
      continue; // do not descend: inner locals are a different scope
    }
    if (n.type === 'assignment' || n.type === 'augmented_assignment'
        || n.type === 'assignment_expression' || n.type === 'assignment_statement'
        || n.type === 'short_var_declaration') {
      addLeft(n.childForFieldName('left'));
    } else if (n.type === 'named_expression') { // python walrus
      addLeft(n.childForFieldName('name'));
    } else if (n.type === 'for_statement' && lang === 'python') {
      addLeft(n.childForFieldName('left'));
    } else if (n.type === 'variable_declarator') { // ts let/const/var
      addLeft(n.childForFieldName('name'));
    }
    for (let i = 0; i < n.namedChildCount; i++) stack.push(n.namedChild(i));
  }
}

// True when `name` is rebound (parameter or local assignment) in ANY function
// scope enclosing `node`. Over-approximate in the skip direction on purpose.
function isLocallyShadowed(node, name, lang) {
  const scopeTypes = FUNC_SCOPE_TYPES[lang];
  for (let p = node.parent; p; p = p.parent) {
    if (!scopeTypes.has(p.type)) continue;
    const bound = new Set();
    collectParamNames(p, lang, bound);
    collectLocalBindings(p, lang, bound);
    if (bound.has(name)) return true;
  }
  return false;
}

// ---------------------------------------------------------------- python
function pythonImports(node, out) {
  if (node.type === 'import_statement') {
    // import a.b [as x][, c.d [as y]]
    for (const child of node.namedChildren) {
      if (child.type === 'dotted_name') {
        out.importDecls.push({
          spec: child.text, level: 0, names: [], moduleAlias: null, line: line(node),
        });
      } else if (child.type === 'aliased_import') {
        const name = child.childForFieldName('name');
        const alias = child.childForFieldName('alias');
        out.importDecls.push({
          spec: name.text, level: 0, names: [],
          moduleAlias: alias ? alias.text : null, line: line(node),
        });
      }
    }
  } else if (node.type === 'import_from_statement') {
    // from [.]*mod import a [as x], b — module_name field; names follow.
    const mod = node.childForFieldName('module_name');
    if (!mod) return;
    let spec = mod.text;
    let level = 0;
    if (mod.type === 'relative_import') {
      const m = mod.text.match(/^(\.+)(.*)$/);
      level = m ? m[1].length : 0;
      spec = m ? m[2] : '';
    }
    const names = [];
    for (const child of node.namedChildren) {
      if (child.id === mod.id) continue; // web-tree-sitter nodes are not reference-equal
      if (child.type === 'dotted_name') {
        names.push({ imported: child.text, local: child.text });
      } else if (child.type === 'aliased_import') {
        const name = child.childForFieldName('name');
        const alias = child.childForFieldName('alias');
        names.push({ imported: name.text, local: alias ? alias.text : name.text });
      } else if (child.type === 'wildcard_import') {
        // `from x import *`: target names are statically unknowable — skip, count.
        out.skipped.imports_unresolved += 1;
      }
    }
    out.importDecls.push({ spec, level, names, moduleAlias: null, line: line(node) });
  }
}

function pythonDefines(node, out) {
  if (node.type !== 'class_definition') return;
  const clsName = node.childForFieldName('name');
  const body = node.childForFieldName('body');
  if (!clsName || !body) return;
  for (const stmt of body.namedChildren) {
    let fn = null;
    if (stmt.type === 'function_definition') fn = stmt;
    else if (stmt.type === 'decorated_definition') {
      const def = stmt.childForFieldName('definition');
      if (def && def.type === 'function_definition') fn = def;
    }
    if (!fn) continue;
    const m = fn.childForFieldName('name');
    out.defines.push({
      container: { name: clsName.text, line: line(clsName), kind: 'class' },
      member: { name: m.text, line: line(m), end_line: endLine(fn), kind: 'function' },
      line: line(m),
    });
  }
}

function pythonCalls(node, out) {
  if (node.type !== 'call') return;
  const fn = node.childForFieldName('function');
  if (!fn) return;
  if (fn.type === 'identifier') {
    if (isLocallyShadowed(node, fn.text, 'python')) {
      out.skipped.calls_shadowed += 1; // param/local rebinding: never guess
      return;
    }
    out.calls.push({
      caller: enclosingCaller(node, 'python'),
      calleeObject: null, calleeName: fn.text, line: line(node),
    });
  } else if (fn.type === 'attribute') {
    const obj = fn.childForFieldName('object');
    const attr = fn.childForFieldName('attribute');
    if (obj && obj.type === 'identifier' && attr) {
      if (obj.text !== 'self' && isLocallyShadowed(node, obj.text, 'python')) {
        out.skipped.calls_shadowed += 1; // the alias object itself is rebound
        return;
      }
      out.calls.push({
        caller: enclosingCaller(node, 'python'),
        calleeObject: obj.text, calleeName: attr.text, line: line(node),
        // self-calls resolve ONLY within the enclosing class (finding 3a)
        ...(obj.text === 'self' ? { enclosingClass: enclosingClass(node) } : {}),
      });
    }
    // deeper attribute chains (a.b.c()) are not statically resolvable here: skip silently —
    // they are counted as unresolved by emit-graph only if surfaced; we do not record them.
  }
}

function pythonRoutes(node, out) {
  if (node.type !== 'decorated_definition') return;
  const def = node.childForFieldName('definition');
  if (!def || def.type !== 'function_definition') return;
  const nameNode = def.childForFieldName('name');
  for (const child of node.namedChildren) {
    if (child.type !== 'decorator') continue;
    for (const pat of ROUTE_PATTERNS.python) {
      if (pat.re.test(child.text)) {
        out.routes.push({
          handlerName: nameNode.text, handlerLine: line(nameNode),
          pattern: pat.name, line: line(child),
        });
        return; // one route fact per handler
      }
    }
  }
}

// ---------------------------------------------------------------- typescript / tsx
function tsImports(node, out) {
  const isImport = node.type === 'import_statement';
  const isExportFrom = node.type === 'export_statement' && node.childForFieldName('source');
  if (!isImport && !isExportFrom) return;
  const srcNode = node.childForFieldName('source');
  if (!srcNode) return;
  const spec = srcNode.text.replace(/^['"]|['"]$/g, '');
  const names = [];
  let moduleAlias = null;
  for (const n of walkNodes(node)) {
    if (n.type === 'import_specifier' || n.type === 'export_specifier') {
      const name = n.childForFieldName('name');
      const alias = n.childForFieldName('alias');
      if (name) names.push({ imported: name.text, local: alias ? alias.text : name.text });
    } else if (n.type === 'namespace_import') {
      // import * as m from './x'
      const id = n.namedChildren.find((c) => c.type === 'identifier');
      if (id) moduleAlias = id.text;
    }
  }
  out.importDecls.push({ spec, level: 0, names, moduleAlias, line: line(node) });
}

function tsDefines(node, out) {
  if (node.type !== 'class_declaration') return;
  const clsName = node.childForFieldName('name');
  const body = node.childForFieldName('body');
  if (!clsName || !body) return;
  for (const member of body.namedChildren) {
    if (member.type !== 'method_definition') continue;
    const m = member.childForFieldName('name');
    if (!m) continue;
    out.defines.push({
      container: { name: clsName.text, line: line(clsName), kind: 'class' },
      member: { name: m.text, line: line(m), end_line: endLine(member), kind: 'method' },
      line: line(m),
    });
  }
}

function tsCalls(node, lang, out) {
  if (node.type !== 'call_expression') return;
  const fn = node.childForFieldName('function');
  if (!fn) return;
  if (fn.type === 'identifier') {
    if (isLocallyShadowed(node, fn.text, lang)) {
      out.skipped.calls_shadowed += 1;
      return;
    }
    out.calls.push({
      caller: enclosingCaller(node, lang),
      calleeObject: null, calleeName: fn.text, line: line(node),
    });
  } else if (fn.type === 'member_expression') {
    const obj = fn.childForFieldName('object');
    const prop = fn.childForFieldName('property');
    if (obj && obj.type === 'identifier' && prop) {
      if (isLocallyShadowed(node, obj.text, lang)) {
        out.skipped.calls_shadowed += 1;
        return;
      }
      out.calls.push({
        caller: enclosingCaller(node, lang),
        calleeObject: obj.text, calleeName: prop.text, line: line(node),
      });
    }
  }
}

function tsRoutes(node, lang, out) {
  if (node.type !== 'call_expression') return;
  const fn = node.childForFieldName('function');
  if (!fn || fn.type !== 'member_expression') return;
  const prop = fn.childForFieldName('property');
  if (!prop) return;
  for (const pat of ROUTE_PATTERNS[lang] || []) {
    if (!pat.methods || !pat.methods.has(prop.text)) continue;
    const args = node.childForFieldName('arguments');
    if (!args) return;
    const argNodes = args.namedChildren;
    // express shape: first arg a string path, a later arg the handler
    if (argNodes.length < 2 || argNodes[0].type !== 'string') return;
    const handlerArg = argNodes[argNodes.length - 1];
    if (handlerArg.type === 'identifier') {
      out.routes.push({
        handlerName: handlerArg.text, handlerLine: null,
        pattern: pat.name, line: line(node),
      });
    } else {
      // inline callback: a real route, but the handler is anonymous — count, don't guess.
      out.skipped.routes_unresolved += 1;
    }
    return;
  }
}

// ---------------------------------------------------------------- go
function goImports(node, out) {
  // Go import paths are module paths, not repo files; resolving them requires
  // go.mod semantics we deliberately do not implement. Count them so coverage
  // stays honest.
  if (node.type === 'import_spec') out.skipped.imports_unresolved += 1;
}

function goDefines(node, out) {
  if (node.type !== 'method_declaration') return;
  const recv = node.childForFieldName('receiver');
  const nameNode = node.childForFieldName('name');
  if (!recv || !nameNode) return;
  // receiver: (parameter_list (parameter_declaration type: [*]type_identifier))
  let typeName = null;
  for (const n of walkNodes(recv)) {
    if (n.type === 'type_identifier') { typeName = n.text; break; }
  }
  if (!typeName) return;
  out.defines.push({
    container: { name: typeName, line: null, kind: 'type' },
    member: {
      name: nameNode.text, line: line(nameNode), end_line: endLine(node), kind: 'method',
    },
    line: line(nameNode),
  });
}

function goCalls(node, out) {
  if (node.type !== 'call_expression') return;
  const fn = node.childForFieldName('function');
  if (!fn) return;
  if (fn.type === 'identifier') {
    if (isLocallyShadowed(node, fn.text, 'go')) {
      out.skipped.calls_shadowed += 1;
      return;
    }
    out.calls.push({
      caller: enclosingCaller(node, 'go'),
      calleeObject: null, calleeName: fn.text, line: line(node),
    });
  } else if (fn.type === 'selector_expression') {
    const obj = fn.childForFieldName('operand');
    const field = fn.childForFieldName('field');
    if (obj && obj.type === 'identifier' && field) {
      if (isLocallyShadowed(node, obj.text, 'go')) {
        out.skipped.calls_shadowed += 1;
        return;
      }
      out.calls.push({
        caller: enclosingCaller(node, 'go'),
        calleeObject: obj.text, calleeName: field.text, line: line(node),
      });
    }
  }
}

// ---------------------------------------------------------------- entry point
export function extractFileEdges({ lang, parsers, source, file }) {
  const parser = parsers[lang];
  if (!parser) {
    throw new Error(`extract-edges: no parser loaded for language '${lang}' (file: ${file})`);
  }
  const tree = parser.parse(source);
  const out = {
    importDecls: [], defines: [], calls: [], routes: [],
    skipped: { imports_unresolved: 0, routes_unresolved: 0, calls_shadowed: 0 },
  };
  for (const node of walkNodes(tree.rootNode)) {
    if (lang === 'python') {
      pythonImports(node, out);
      pythonDefines(node, out);
      pythonCalls(node, out);
      pythonRoutes(node, out);
    } else if (lang === 'typescript' || lang === 'tsx') {
      tsImports(node, out);
      tsDefines(node, out);
      tsCalls(node, lang, out);
      tsRoutes(node, lang, out);
    } else if (lang === 'go') {
      goImports(node, out);
      goDefines(node, out);
      goCalls(node, out);
    } else {
      throw new Error(`extract-edges: unsupported language '${lang}' (file: ${file})`);
    }
  }
  return out;
}
