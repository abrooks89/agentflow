// write-manifest.js — deterministic .manifest.json writer for the autodoc code-wiki.
// Walks the built autodoc pages (reusing graph-check's parsePage/listPages — no second
// frontmatter parser) and emits the reverse-index /autodoc-update depends on:
// source file -> pages to refresh. See skills/autodoc-init/references/scaffold.md
// ("Step 4 — Write the manifest") for the shape this mirrors.
import { readFileSync, writeFileSync, existsSync, copyFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { parsePage, listPages } from './graph-check.js';

// buildManifest({autodocDir, sourceRepo, sourceBranch, sourcePath, project, lastSha, generatedAt})
//   -> { manifest, errors: [{page, field}] }
// The argument is an OPTIONS OBJECT, so the keys above are matched by name and their order
// is not load-bearing; they are listed in the manifest's own key order. Do NOT read this
// line as the CLI's positional order -- the CLI's is different and is stated at `CLI:` below.
// `manifest` always carries all seven top-level keys in a fixed order and a `pages` map
// with keys sorted lexicographically. `errors` lists every content page missing the
// required `type` frontmatter field (page+field named, per-page — never guessed); a
// non-empty `errors` means the caller must NOT persist this manifest (see CLI below).
// A page with empty/absent `source_paths` still gets an entry (source_paths: []) but
// prints a stderr warning — it is not itself a build-blocking error.
export function buildManifest({ autodocDir, sourceRepo, sourceBranch, sourcePath, project, lastSha, generatedAt }) {
  const pages = listPages(autodocDir, { excludeNav: true });
  const errors = [];
  const entries = {};

  for (const p of pages) {
    const text = readFileSync(p.abspath, 'utf8');
    const { frontmatter, source_paths } = parsePage(text);

    if (!frontmatter.type) {
      errors.push({ page: p.file, field: 'type' });
      continue;
    }

    if (source_paths.length === 0) {
      console.error(`write-manifest: warning: ${p.file} has no source_paths`);
    }

    const entry = { type: frontmatter.type, source_paths };
    if (frontmatter.status === 'deprecated') entry.status = 'deprecated';
    entries[p.file] = entry;
  }

  const pagesSorted = {};
  for (const key of Object.keys(entries).sort()) pagesSorted[key] = entries[key];

  // Object literal (not spread) so key order is guaranteed regardless of call-site
  // argument order: source_repo, source_branch, source_path, project, last_sha,
  // generated_at, pages.
  //
  // `project` is the agentflow project this autodoc documents. It is carried here
  // because /autodoc-update deliberately does not re-read the project profile, and it
  // needs the value for `graph build --project` -- the renderer files pages into a
  // per-project vault pillar by it. It is NOT derivable from source_path: a project
  // whose autodoc_root points at another project's corpus would silently stamp every
  // page with the wrong one, which is worse than the refusal it replaces.
  const manifest = {
    source_repo: sourceRepo,
    source_branch: sourceBranch,
    source_path: sourcePath,
    project,
    last_sha: lastSha,
    generated_at: generatedAt,
    pages: pagesSorted,
  };

  return { manifest, errors };
}

// serializeManifest(manifest) -> pretty JSON text with a single trailing newline.
export function serializeManifest(manifest) {
  return `${JSON.stringify(manifest, null, 2)}\n`;
}

// CLI: node write-manifest.js <autodocDir> <sourceRepo> <sourceBranch> <sourcePath> <lastSha> [generatedAt] --project <name> [--backup-to <path>]
//
// The six POSITIONAL slots are exactly the ones this CLI has always had, and `project` is a
// NAMED FLAG rather than a seventh. That is deliberate (gh#152). `project` was first added as
// a sixth positional, which is the slot the optional `generatedAt` already occupied: an
// untouched six-argument call site then exited 0 while landing its DATE in the project field,
// and `generatedAt` defaulted to today, so the manifest looked right on inspection while
// `graph build --project` filed every page into a pillar named after a date. That is the
// silent wrong attribution this file's own note above calls worse than the refusal it
// replaces. A flag cannot be filled by a positional, so the collision class is gone rather
// than detected one instance at a time -- and the legacy shape now refuses loudly, naming the
// flag that would fix it.
//
// `--project=<name>` is accepted too, and the flag may sit anywhere in the argument list.
//
// Writes <autodocDir>/.manifest.json. Exits 1 (no write at all — old manifest, if any, is
// left exactly as-is, and nothing is copied into a --backup-to path) when any page is missing
// `type` frontmatter, naming each page+field on stderr. Exits 2 on a usage error: a missing
// required positional, a missing or empty `--project`, an unrecognised `--flag`, a
// `--backup-to` that is not an existing EMPTY file, or more than six positionals -- that last
// one catches a caller that "upgraded" by appending the project positionally, which is the
// same collision arriving from the other side.
//
// BACKUPS: THIS FILE NEVER COMPOSES A BACKUP NAME (gh#184). It used to copy the old manifest
// to a hand-built `<manifest>` + marker name, which `agentflow.backups`' reaper does not
// recognise as canonical, so the manifest family grew without bound. The filename format lives
// in exactly one place, `skills/_shared/agentflow/backups.py`, and a Node process cannot reach
// it without a Python dependency this package (and CI's inventory job) does not carry. So the
// CALLER claims the name - `python -m agentflow.backups new <manifest> --firing <id>` creates
// the file empty and prints its path - and passes it here as `--backup-to`; this CLI only
// fills it, byte-for-byte, and only once the new manifest is known to be writable. It checks
// that the path already exists and is empty (a claim, not a name invented on the spot, and
// never an older backup about to be clobbered) before anything is written.
// Without `--backup-to` an existing manifest is overwritten WITHOUT a backup and stderr says
// so; that is the autodoc-init post-cutover path, where the tree is freshly rendered and no
// manifest exists to lose.
const USAGE =
  'usage: node write-manifest.js <autodocDir> <sourceRepo> <sourceBranch> <sourcePath> '
  + '<lastSha> [generatedAt] --project <name> [--backup-to <claimed-backup-path>]';
const MAX_POSITIONALS = 6;

const VALUE_FLAGS = { '--project': 'project', '--backup-to': 'backupTo' };

// parseCliArgs(argv) -> {positionals, project, backupTo} | {error}. Exported so the argument
// contract is testable as a pure function; the CLI behaviour is still covered by spawned runs.
// `backupTo` is undefined when the flag is absent.
export function parseCliArgs(argv) {
  const positionals = [];
  const named = {};
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (Object.hasOwn(VALUE_FLAGS, arg)) {
      const value = argv[i + 1];
      if (value === undefined || value.startsWith('--')) {
        return { error: `${arg} requires a value` };
      }
      named[VALUE_FLAGS[arg]] = value;
      i += 1;
      continue;
    }
    const eq = arg.indexOf('=');
    if (eq > 0 && Object.hasOwn(VALUE_FLAGS, arg.slice(0, eq))) {
      named[VALUE_FLAGS[arg.slice(0, eq)]] = arg.slice(eq + 1);
      continue;
    }
    if (arg.startsWith('--')) {
      return { error: `unrecognised option ${arg}` };
    }
    positionals.push(arg);
  }
  if (positionals.length > MAX_POSITIONALS) {
    return {
      error: `${positionals.length} positional arguments, at most ${MAX_POSITIONALS} accepted `
        + `(extra: ${positionals.slice(MAX_POSITIONALS).join(' ')}). `
        + '`project` is a named flag: pass --project <name>, not a positional.',
    };
  }
  if (!named.project) {
    return { error: 'missing required --project <name>' };
  }
  if (named.backupTo === '') {
    return { error: '--backup-to requires a value' };
  }
  return { positionals, project: named.project, backupTo: named.backupTo };
}

// claimError(path) -> null when `path` is an existing, empty regular file (what
// `agentflow.backups new` leaves behind), else the reason it is not.
function claimError(path) {
  let st;
  try {
    st = statSync(path);
  } catch (err) {
    if (err.code === 'ENOENT') {
      return `--backup-to ${path} does not exist; claim it first with `
        + '`python -m agentflow.backups new <manifest> --firing <id>` (this CLI never invents a backup name)';
    }
    throw err;
  }
  if (!st.isFile()) return `--backup-to ${path} is not a regular file`;
  if (st.size !== 0) {
    return `--backup-to ${path} is not empty (${st.size} bytes); refusing to overwrite what may be an earlier backup`;
  }
  return null;
}

const isMain = process.argv[1] && process.argv[1].endsWith('write-manifest.js');
if (isMain) {
  const parsed = parseCliArgs(process.argv.slice(2));
  const [autodocDir, sourceRepo, sourceBranch, sourcePath, lastSha, generatedAtArg] = parsed.positionals || [];
  const error = parsed.error
    || (!autodocDir || !sourceRepo || !sourceBranch || !sourcePath || !lastSha
      ? 'missing a required positional argument'
      : null)
    || (parsed.backupTo !== undefined ? claimError(parsed.backupTo) : null);
  if (error) {
    console.error(`write-manifest: ${error}`);
    console.error(USAGE);
    process.exit(2);
  }
  const project = parsed.project;
  const generatedAt = generatedAtArg || new Date().toISOString().slice(0, 10);

  const { manifest, errors } = buildManifest({
    autodocDir, sourceRepo, sourceBranch, sourcePath, project, lastSha, generatedAt,
  });

  if (errors.length) {
    for (const e of errors) {
      console.error(`write-manifest: ${e.page}: missing required frontmatter field '${e.field}'`);
    }
    console.error(`write-manifest: ${errors.length} page(s) missing 'type' — manifest NOT written`);
    process.exit(1);
  }

  const manifestPath = join(autodocDir, '.manifest.json');
  if (existsSync(manifestPath)) {
    if (parsed.backupTo !== undefined) {
      copyFileSync(manifestPath, parsed.backupTo);
      console.error(`write-manifest: backed up the previous manifest to ${parsed.backupTo}`);
    } else {
      console.error(
        'write-manifest: warning: no --backup-to given; overwriting the existing manifest WITHOUT a backup',
      );
    }
  } else if (parsed.backupTo !== undefined) {
    console.error(`write-manifest: no previous manifest to back up; ${parsed.backupTo} left empty`);
  }
  writeFileSync(manifestPath, serializeManifest(manifest));

  console.error(`write-manifest: ${Object.keys(manifest.pages).length} page(s) → ${manifestPath}`);
}
