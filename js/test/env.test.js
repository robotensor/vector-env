import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Cells, settle, restore, replayCtrl, runJob, limits, setLimit, FORMAT } from '../src/index.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
process.env.VECTOR_ENV_DIR ||= path.resolve(HERE, '..', '..');
const { compose, SETTINGS, BUNDLE, SKIP } = await import('@vector/sim/testing');
const cells = await new Cells(BUNDLE, 1).init();
const base = compose(SETTINGS, 11);
const fake = (mode) => ({ argv: [process.execPath, path.join(HERE, 'fake_expert.mjs'), mode] });

function scene() {
  const cell = cells.get(base);
  const s = settle(cell, 3);
  return { scene_id: 'n11', xml: base.xml, meta: base.meta, checkpoint: s.checkpoint, state_hash: s.state_hash, target: s.target };
}

test('settling is reproducible and the checkpoint restores to the state hash', { skip: SKIP }, () => {
  const a = scene();
  const fresh = new Cells(BUNDLE, 1);
  fresh.mj = cells.mj;
  const b = settle(fresh.get(base), 3);
  assert.equal(b.state_hash, a.state_hash);
  assert.deepEqual(b.checkpoint, a.checkpoint);
  assert.equal(b.target, a.target);
  restore(fresh.get(base), a);
  assert.throws(() => restore(fresh.get(base), { ...a, state_hash: '0'.repeat(16) }), /state hash/);
  assert.throws(() => restore(fresh.get(base), { ...a, target: 99 }), /target/);
  for (const state_hash of [undefined, null, '', 7]) assert.throws(() => restore(fresh.get(base), { ...a, state_hash }), /no state hash/);
});

test('a run steps the rows the expert sends, and replay agrees', { skip: SKIP }, async () => {
  const sc = scene();
  const r = await runJob(cells, { scene: sc, expert: fake('hold'), time_limit_s: 2, wall_s: 30 });
  assert.equal(r.outcome, 'timed_out');
  assert.equal(r.n, 40);
  assert.equal(r.exchanges, 40);
  assert.equal(r.t, 2);
  const cell = cells.get(sc);
  const rp = replayCtrl(cell, sc, r.ctrl, r.n, 2);
  assert.deepEqual([rp.outcome, rp.t, rp.tick], [r.outcome, r.t, 40]);
  setLimit(cell, 15);
  assert.deepEqual(limits(cell), { time_limit_s: 15, control_hz: 20, tick_s: 0.05, max_rows: 300 });
  assert.equal(FORMAT, 'vector-expert/2');
});

test('protocol limits: too many rows, wall budget', { skip: SKIP }, async () => {
  const sc = scene();
  const many = await runJob(cells, { scene: sc, expert: fake('many'), time_limit_s: 15, wall_s: 30 });
  assert.equal(many.outcome, 'expert_error');
  assert.match(many.error, /more than the 300 left/);
  const late = await runJob(cells, { scene: sc, expert: fake('late'), time_limit_s: 15, wall_s: 2 });
  assert.equal(late.outcome, 'wall_timeout');
  assert.ok(late.wall_s >= 1.9 && late.wall_s < 5, String(late.wall_s));
});

test('what an expert writes is bounded', { skip: SKIP }, async () => {
  const sc = scene();
  const deaf = await runJob(cells, { scene: sc, expert: fake('deaf'), time_limit_s: 15, wall_s: 30, io: { max_unread_mib: 0.5 } });
  assert.equal(deaf.outcome, 'expert_error');
  assert.match(deaf.error, /input unread/);
  const long = await runJob(cells, { scene: sc, expert: fake('long'), time_limit_s: 15, wall_s: 30, io: { max_line_kib: 64 } });
  assert.deepEqual([long.outcome, long.error], ['expert_error', 'a message is longer than 64 KiB']);
  assert.ok(long.wall_s < 5, String(long.wall_s));
  const lost = await runJob(cells, { scene: { ...sc, state_hash: undefined }, expert: fake('hold'), time_limit_s: 2, wall_s: 30 });
  assert.deepEqual([lost.outcome, lost.error], ['scene_error', 'the scene has no state hash']);
});
