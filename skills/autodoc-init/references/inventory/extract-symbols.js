// extract-symbols.js — run tree-sitter queries per language, return Symbol[].
// Symbol = { file, lang, kind, name, line, end_line }
// (line = 1-based start row of the NAME node; end_line = 1-based end row of the
//  enclosing definition node — added for the graph epic's entity spans, additive)

// Per-language S-expression queries. Capture @name on the identifier node.
const QUERIES = {
  python: `
    (function_definition name: (identifier) @function)
    (class_definition name: (identifier) @class)
    (decorated_definition
      (decorator) @decorator
      definition: (function_definition name: (identifier) @decorated_fn)) @decorated
  `,
  go: `
    (function_declaration name: (identifier) @function)
    (method_declaration name: (field_identifier) @method)
    (type_declaration (type_spec name: (type_identifier) @type))
  `,
  typescript: `
    (function_declaration name: (identifier) @function)
    (class_declaration name: (type_identifier) @class)
    (export_statement (lexical_declaration (variable_declarator name: (identifier) @function value: [(arrow_function) (function_expression)])))
  `,
  tsx: `
    (function_declaration name: (identifier) @component)
    (export_statement (lexical_declaration (variable_declarator name: (identifier) @component value: (arrow_function))))
  `,
};

// capture name → Symbol.kind
const KIND_BY_CAPTURE = {
  function: 'function', class: 'class', method: 'method',
  type: 'type', component: 'component', endpoint: 'endpoint',
};

// Decorator text matching a route registration, e.g. @app.post("/x"), @router.get(...)
const ROUTE_RE = /\.(get|post|put|patch|delete|websocket|route)\s*\(/i;

// End row (1-based) of the definition enclosing a captured NAME node: climb to
// the nearest ancestor that is a definition/declaration/declarator, so the span
// covers the whole body, not just the identifier. Falls back to the name node.
function definitionEndLine(nameNode) {
  for (let p = nameNode.parent; p; p = p.parent) {
    if (/definition|declaration|declarator/.test(p.type)) return p.endPosition.row + 1;
  }
  return nameNode.endPosition.row + 1;
}

export function extractSymbols({ lang, parsers, source, file }) {
  const parser = parsers[lang];
  const language = parsers[`_lang_${lang}`];
  if (!parser || !language) return [];
  const tree = parser.parse(source);
  const out = [];
  const seen = new Set();
  const query = language.query(QUERIES[lang] || '');
  const captures = query.captures(tree.rootNode);

  // Python: collect decorated functions whose decorator is a route, keyed by name:line.
  const decorated = new Map(); // `${fnName}:${line}` -> decoratorText
  if (lang === 'python') {
    for (let i = 0; i < captures.length; i++) {
      if (captures[i].name !== 'decorated_fn') continue;
      const fnNode = captures[i].node;
      // nearest preceding @decorator capture belongs to the same decorated_definition
      let decoText = '';
      for (let j = i - 1; j >= 0; j--) {
        if (captures[j].name === 'decorator') { decoText = captures[j].node.text; break; }
      }
      if (ROUTE_RE.test(decoText)) {
        decorated.set(`${fnNode.text}:${fnNode.startPosition.row + 1}`, decoText.trim());
      }
    }
  }

  for (const cap of captures) {
    let kind = KIND_BY_CAPTURE[cap.name];
    if (!kind) continue;
    const name = cap.node.text;
    // tsx: only treat Capitalized names as components (lower-case = plain helpers)
    if (lang === 'tsx' && !/^[A-Z]/.test(name)) continue;
    const line = cap.node.startPosition.row + 1;
    const key = `${kind}:${name}:${line}`;
    if (seen.has(key)) continue;
    // python: a decorated route function becomes an endpoint, never a plain function
    if (lang === 'python' && kind === 'function') {
      const decoText = decorated.get(`${name}:${line}`);
      if (decoText) {
        seen.add(key);
        out.push({
          file, lang, kind: 'endpoint', name, line,
          end_line: definitionEndLine(cap.node), route_decorator: decoText,
        });
        continue;
      }
    }
    seen.add(key);
    out.push({ file, lang, kind, name, line, end_line: definitionEndLine(cap.node) });
  }
  return out;
}
