import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PKG_DIR = path.resolve(HERE, '..');
const PKG = JSON.parse(fs.readFileSync(path.join(PKG_DIR, 'package.json'), 'utf8'));
const SRC = ['coordinator', 'env', 'index', 'io', 'trace', 'verifier'];

test('@vector/env is the bin-picking index', async () => {
  const root = await import('@vector/env');
  assert.equal(root, await import('@vector/env/skills/bin-picking/src/index.js'));
  assert.equal(root, await import('@vector/env/index.js'));
  assert.ok(typeof root.Cells === 'function' && typeof root.Session === 'function');
});

test('@vector/env/<file>.js is the same module as @vector/env/skills/bin-picking/src/<file>.js', async () => {
  for (const f of SRC) {
    const short = await import(`@vector/env/${f}.js`);
    const long = await import(`@vector/env/skills/bin-picking/src/${f}.js`);
    assert.equal(short, long, f);
    assert.ok(Object.keys(short).length > 0, f);
  }
});

test('each bin exists, is executable and runs with node', () => {
  assert.deepEqual(Object.keys(PKG.bin).sort(), ['vector-env-harness', 'vector-env-sim', 'vector-env-verify']);
  for (const [name, rel] of Object.entries(PKG.bin)) {
    const file = path.join(PKG_DIR, rel);
    assert.ok(rel.startsWith('./skills/bin-picking/bin/'), name);
    fs.accessSync(file, fs.constants.X_OK);
    assert.equal(fs.readFileSync(file, 'utf8').split('\n', 1)[0], '#!/usr/bin/env node', name);
  }
});
