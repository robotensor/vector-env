import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import fs from 'node:fs';
import { Cells, settle, replayCtrl, actionHash, verifyRows, probeScene } from '../src/index.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
process.env.VECTOR_ENV_DIR ||= path.resolve(HERE, '..', '..');
const { compose, SETTINGS, BUNDLE, SKIP } = await import('@vector/sim/testing');
// A pick recorded on the settled fixture scene: rows that lift the target, for the checks below.
const PICK = JSON.parse(fs.readFileSync(path.join(HERE, 'fixtures', 'pick.json'), 'utf8'));
const cells = SKIP ? null : await new Cells(BUNDLE, 1).init();
const base = compose(SETTINGS, 11);

let cached = null;
function setup() {
  if (cached) return cached;
  const cell = cells.get(base);
  const s = settle(cell, 3);
  const sc0 = { scene_id: 'v11', xml: base.xml, meta: base.meta, checkpoint: s.checkpoint, state_hash: s.state_hash, target: s.target };
  const sc = { ...sc0, target: PICK.target };
  verifyRows(cell, sc, [], { timeLimitS: 30, every: 0 });
  const buf = Buffer.from(PICK.rows, 'base64'), flat = new Float64Array(buf.buffer, buf.byteOffset, buf.length / 8);
  const rows = [];
  for (let k = 0; k < flat.length; k += 7) rows.push(Array.from(flat.subarray(k, k + 7)));
  while (rows.length < 600) rows.push(rows.at(-1).slice());
  if (verifyRows(cell, sc, rows, { timeLimitS: 30, every: 0 }).outcome === 'success') return (cached = { cell, sc, rows });
  throw new Error('the recorded pick (fixtures/pick.json) does not lift its part');
}

test('a human log is replayed to its verdict and must end there', { skip: SKIP }, () => {
  const { cell, sc, rows } = setup();
  const all = verifyRows(cell, sc, rows, { timeLimitS: 30 });
  assert.equal(all.ok, false);
  assert.equal(all.reason, 'rows_after_verdict');
  const cut = rows.slice(0, all.tick);
  const r = verifyRows(cell, sc, cut, { timeLimitS: 30 });
  assert.equal(r.ok, true, r.detail);
  assert.equal(r.tick, cut.length);
  assert.equal(r.action_hash, actionHash(cut));
  const again = verifyRows(cell, sc, cut, { timeLimitS: 30, claim: { outcome: r.outcome, tick: r.tick, state_hash: r.state_hash,
                                                                     action_hash: r.action_hash } });
  assert.equal(again.ok, true);
  const short = verifyRows(cell, sc, cut.slice(0, 5), { timeLimitS: 30 });
  assert.equal(short.reason, 'no_verdict');
  assert.equal(r.outcome, 'success');
  const over = verifyRows(cell, sc, rows, { timeLimitS: 1 });
  assert.equal(over.reason, 'over_budget');
});

test('a forged claim or a tampered simulator fails the replay', { skip: SKIP }, () => {
  const { cell, sc, rows } = setup();
  const r = verifyRows(cell, sc, rows.slice(0, verifyRows(cell, sc, rows, { timeLimitS: 30 }).tick), { timeLimitS: 30 });
  const cut = rows.slice(0, r.tick);
  const other = r.outcome === 'success' ? 'missed' : 'success';
  assert.equal(verifyRows(cell, sc, cut, { timeLimitS: 30, claim: { outcome: other } }).reason, 'outcome_mismatch');
  assert.equal(verifyRows(cell, sc, cut, { timeLimitS: 30, claim: { state_hash: '0'.repeat(16) } }).reason, 'state_mismatch');
  assert.equal(verifyRows(cell, sc, cut, { timeLimitS: 30, claim: { tick: r.tick + 1 } }).reason, 'tick_mismatch');
  assert.equal(verifyRows(cell, sc, cut, { timeLimitS: 30, claim: { action_hash: '0'.repeat(16) } }).reason, 'action_mismatch');
  const honest = verifyRows(cell, sc, cut, { timeLimitS: 30, every: 5 });
  const marks = [[5, 'x'], [10, 'y']];
  const tampered = verifyRows(cell, sc, cut, { timeLimitS: 30, claim: { marks } });
  assert.equal(tampered.reason, 'state_mismatch');
  assert.equal(tampered.divergence.tick, 5);
  const bent = cut.map((x) => x.slice());
  bent[20][0] += 1e-9;
  const real = verifyRows(cell, sc, cut, { timeLimitS: 30 });
  const fromBent = verifyRows(cell, sc, bent, { timeLimitS: 30, claim: { state_hash: real.state_hash, tick: real.tick } });
  assert.equal(fromBent.ok, false);
  assert.ok(honest.checked === 0);
});

test('an expert result verifies the same way, holding its last row', { skip: SKIP }, () => {
  const { cell, sc, rows } = setup();
  const part = rows.slice(0, 120);
  const want = replayCtrl(cell, sc, part.flat(), part.length, 15);
  const r = verifyRows(cell, sc, part, { timeLimitS: 15, hold: true, claim: { outcome: want.outcome, tick: want.tick, state_hash: want.hash } });
  assert.equal(r.ok, true, r.detail);
  assert.equal(r.held, want.held);
});

test('the probe is the same every time and leaves the scene as it was', { skip: SKIP }, () => {
  const { cell, sc } = setup();
  const a = probeScene(cell, sc), b = probeScene(cell, sc);
  assert.deepEqual(a, b);
  assert.equal(a.start, sc.state_hash);
  assert.equal(cell.hash(), sc.state_hash);
  assert.equal(a.marks.length, 8);
});

test('a console claim with a row outside the actuator ranges is refused before the replay', { skip: SKIP }, () => {
  const { cell, sc, rows } = setup();
  const cut = rows.slice(0, verifyRows(cell, sc, rows, { timeLimitS: 30 }).tick);
  assert.equal(verifyRows(cell, sc, cut, { timeLimitS: 30, claim: { in_range: true } }).ok, true);
  const hid = new Float64Array(1);
  new BigUint64Array(hid.buffer)[0] = 0x4130000000000000n | 0x4e4f53494f50n;
  for (const [col, v] of [[6, -1e6], [6, hid[0]], [6, 1.5], [0, 7], [2, -3.2]]) {
    const bad = cut.map((x) => x.slice());
    bad[3][col] = v;
    const r = verifyRows(cell, sc, bad, { timeLimitS: 30, claim: { in_range: true } });
    assert.equal(r.reason, 'out_of_range', `${col} ${v}`);
    assert.equal(r.ok, false);
  }
  const edge = cut.map((x) => x.slice());
  edge[3][6] = 1 + 2 ** -52;
  assert.notEqual(verifyRows(cell, sc, edge, { timeLimitS: 30, claim: { in_range: true } }).reason, 'out_of_range');
  const poisoned = cut.map((x) => x.slice());
  poisoned[3][6] = -1e6;
  assert.notEqual(verifyRows(cell, sc, poisoned, { timeLimitS: 30 }).reason, 'out_of_range');
});
