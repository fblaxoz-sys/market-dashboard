// Frontend guardrails for the single-file app (index.html). Run: node tests/test_frontend.mjs
//
// Catches the two classes of bug we actually hit:
//   1. A bad edit that breaks the inline JS (white-screens the app)  → syntax check
//   2. A stale-copy commit that silently drops a feature (the fdfced9 revert) → presence check
//   3. The UTC month off-by-one in date math → logic check on the real addMonthsYM
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const html = readFileSync(join(root, 'index.html'), 'utf8');
let passed = 0;
const ok = (name) => { console.log(`  ✓ ${name}`); passed++; };

// 1) Inline JS must be syntactically valid (node --check, syntax only — browser globals are fine)
const inline = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n;\n');
const tmp = join(mkdtempSync(join(tmpdir(), 'mdz-')), 'inline.js');
writeFileSync(tmp, inline);
execFileSync(process.execPath, ['--check', tmp]);   // throws on syntax error
ok('inline JS parses (node --check)');

// 2) Critical features must be present (guards accidental reverts / stale-copy commits)
const required = [
  'corr-win-btns', 'setCorrWin', 'renderCorrView',   // multi-window correlations
  'function addMonthsYM',                             // timezone-safe month math
  'function fetchJSON',                               // resilient fetch
  "switchTab('inflation')", "switchTab('gdp')",       // tabs wired
  'inf-scenarios', 'gdp-scenarios',                   // scenario ranges on both predictors
  'GDP SCENARIO RANGE',                               // GDP scenario strip populated
];
for (const marker of required) {
  assert.ok(html.includes(marker), `missing required feature marker: ${marker}`);
}
ok(`all ${required.length} required features present`);

// 3) The real shipped addMonthsYM must do timezone-safe month math
const src = html.match(/function addMonthsYM\([^)]*\)\s*\{[\s\S]*?\n\}/);
assert.ok(src, 'could not extract addMonthsYM');
const addMonthsYM = eval('(' + src[0].replace(/^function addMonthsYM/, 'function') + ')');
assert.equal(addMonthsYM('2026-05-01', 1), '2026-06');   // the bug case (was '2026-05' in US TZ)
assert.equal(addMonthsYM('2026-05-01', 2), '2026-07');
assert.equal(addMonthsYM('2026-12-01', 1), '2027-01');   // year rollover
assert.equal(addMonthsYM('2026-03-01', 3), '2026-06');   // GDP quarter offsets
assert.equal(addMonthsYM('2026-03-01', 6), '2026-09');
ok('addMonthsYM month math (incl. year rollover + GDP offsets)');

console.log(`OK — ${passed} frontend checks passed`);
