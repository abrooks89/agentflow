// engine.js — the prompt-template substitution engine (epic 704).
//
// Prompt bodies are language-neutral template STRINGS with a documented
// placeholder set (see each prompts/<id>.js contextSpec). This engine is the
// only rendering logic, so a Python driver (epic 707) renders prompts via
// `render-prompt.js` with no port, and the ultracode workflow scripts — which
// run in a sandbox without module imports — inline the exact same function
// between the markers below. A static test (prompt-modules.test.js) fails the
// suite if an inlined copy drifts from this file.
//
// Template language (all of it — keep this tiny):
//   {{key}}              substitute a string/number from the scope chain
//                        (each-item scope first, then outer scopes). A missing
//                        or non-scalar value is a loud error naming the key.
//   {{#each key}}...{{/each}}   repeat for each item of an array. Object items
//                        become the inner scope; scalar items bind to {{.}}.
//   {{#if key}}...{{/if}}       render when the value is truthy (non-empty
//                        string/array, non-zero number, true). A MISSING key
//                        is falsy here (and only here).
//   {{^key}}...{{/^key}}        inverted section: render when falsy/missing.
// Nesting is supported. No expressions, no filters, no partials.

// >>> template-engine (single-sourced: references/prompts/engine.js) >>>
function renderTemplate(template, context) {
  const OPEN = /\{\{(#each|#if|\^)?\s*([\w.]+)\s*\}\}/;
  const lookup = (scopes, key) => {
    for (let i = scopes.length - 1; i >= 0; i--) {
      const s = scopes[i];
      if (s !== null && typeof s === 'object' && key in s) return s[key];
      if (key === '.' && (typeof s !== 'object' || s === null)) return s;
    }
    return undefined;
  };
  const truthy = (v) => Array.isArray(v) ? v.length > 0
    : typeof v === 'string' ? v.length > 0
    : typeof v === 'number' ? v !== 0
    : Boolean(v);
  const render = (src, scopes) => {
    let out = '';
    let rest = src;
    for (;;) {
      const m = rest.match(OPEN);
      if (!m) return out + rest;
      out += rest.slice(0, m.index);
      rest = rest.slice(m.index + m[0].length);
      const [, kind, key] = m;
      if (!kind) {
        const v = lookup(scopes, key);
        if (v === undefined || v === null || (typeof v === 'object')) {
          throw new Error(`template placeholder {{${key}}} has no scalar value`);
        }
        out += String(v);
        continue;
      }
      const closeTag = kind === '^' ? `{{/^${key}}}` : `{{/${kind.slice(1)}}}`;
      // find the matching close, honoring nesting of the same kind
      const openRe = kind === '^'
        ? new RegExp(`\\{\\{\\^\\s*[\\w.]+\\s*\\}\\}`, 'g')
        : new RegExp(`\\{\\{${kind}\\s*[\\w.]+\\s*\\}\\}`, 'g');
      const closeRe = kind === '^' ? /\{\{\/\^[\w.]+\}\}/g : new RegExp(`\\{\\{/${kind.slice(1)}\\}\\}`, 'g');
      let depth = 1;
      let idx = 0;
      for (;;) {
        openRe.lastIndex = idx;
        closeRe.lastIndex = idx;
        const nextOpen = openRe.exec(rest);
        const nextClose = closeRe.exec(rest);
        if (!nextClose) throw new Error(`unclosed section for ${key} (expected ${closeTag})`);
        if (nextOpen && nextOpen.index < nextClose.index) {
          depth += 1;
          idx = nextOpen.index + nextOpen[0].length;
        } else {
          depth -= 1;
          idx = nextClose.index + nextClose[0].length;
          if (depth === 0) {
            var inner = rest.slice(0, nextClose.index);
            rest = rest.slice(idx);
            break;
          }
        }
      }
      const value = lookup(scopes, key);
      if (kind === '#each') {
        if (value === undefined) throw new Error(`template section {{#each ${key}}} has no value`);
        if (!Array.isArray(value)) throw new Error(`{{#each ${key}}} needs an array`);
        for (const item of value) out += render(inner, [...scopes, item]);
      } else if (kind === '#if') {
        if (truthy(value)) out += render(inner, scopes);
      } else {
        if (!truthy(value)) out += render(inner, scopes);
      }
    }
  };
  return render(template, [context]);
}
// <<< template-engine <<<

export { renderTemplate };
