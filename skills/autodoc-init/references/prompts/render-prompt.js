// render-prompt.js — CLI over the prompt module (epic 704 scope 1).
//
// This is the seam an external docgen worker consumes: a Python (or any)
// driver renders the exact prompt text a workflow agent would receive, with
// no port of the prompt bodies. Same isMain guard pattern as graph-check.js.
//
//   node render-prompt.js --id map --context ctx.json    render one prompt
//   node render-prompt.js --id map --schema              print its return schema
//   node render-prompt.js --dump                         all {id: {template, schema, contextSpec}}
//   node render-prompt.js --list                         prompt ids, one per line
import { readFileSync } from 'node:fs';

import * as map from './map.js';
import * as parentReport from './parent-report.js';
import * as flows from './flows.js';
import * as diagram from './diagram.js';
import * as tours from './tours.js';
import * as indexPages from './index-pages.js';
import * as refresh from './refresh.js';

const MODULES = [map, parentReport, flows, diagram, tours, indexPages, refresh];

export const PROMPTS = Object.fromEntries(MODULES.map((m) => [m.id, m]));

// The payload a workflow (or 707 driver) receives as args.prompts.
export function dumpPrompts() {
  return Object.fromEntries(MODULES.map((m) => [
    m.id,
    { template: m.template, schema: m.schema, contextSpec: m.contextSpec },
  ]));
}

const isMain = process.argv[1] && process.argv[1].endsWith('render-prompt.js');
if (isMain) {
  const args = process.argv.slice(2);
  const flag = (name) => {
    const i = args.indexOf(name);
    return i === -1 ? null : (args[i + 1] ?? null);
  };
  if (args.includes('--list')) {
    for (const m of MODULES) console.log(m.id);
    process.exit(0);
  }
  if (args.includes('--dump')) {
    console.log(JSON.stringify(dumpPrompts(), null, 2));
    process.exit(0);
  }
  const id = flag('--id');
  const mod = id && PROMPTS[id];
  if (!mod) {
    console.error(`usage: node render-prompt.js --id <${Object.keys(PROMPTS).join('|')}> (--context ctx.json | --schema) | --dump | --list`);
    process.exit(2);
  }
  if (args.includes('--schema')) {
    console.log(JSON.stringify(mod.schema, null, 2));
    process.exit(0);
  }
  const ctxPath = flag('--context');
  if (!ctxPath) {
    console.error('render-prompt: --context ctx.json is required to render');
    process.exit(2);
  }
  let context;
  try {
    context = JSON.parse(readFileSync(ctxPath, 'utf8'));
  } catch (e) {
    console.error(`render-prompt: cannot read context ${ctxPath}: ${e.message}`);
    process.exit(1);
  }
  try {
    process.stdout.write(mod.render(context));
  } catch (e) {
    console.error(`render-prompt: ${id}: ${e.message}`);
    process.exit(1);
  }
}
