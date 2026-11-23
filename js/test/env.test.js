import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Cells, cellKey, settle, restore, replayCtrl, traceCtrl, matQuat, runJob, limits, setLimit, FORMAT, INFRA_OUTCOMES, TRACE_FORMAT, Session, actionHash, EMPTY_ACTION_HASH, ctrlRanges, appliedRows } from '../src/index.js';
import { b64ToF64 } from '@vector/sim';

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

test('the cell cache keys on the xml and meta it is given, not on a claimed hash', { skip: SKIP }, () => {
  const c = new Cells(BUNDLE, 2);
  c.mj = cells.mj;
  const claim = { ...base.meta, xml_sha256: 'same' };
  const a = c.get({ ...base, meta: claim });
  const cfg = JSON.parse(JSON.stringify(base.meta.config));
  cfg.rules.hold_s += 1;
  const b = c.get({ ...base, meta: { ...claim, config: cfg } });
  assert.notEqual(b, a);
  assert.equal(b.cfg.rules.hold_s, cfg.rules.hold_s);
  assert.notEqual(cellKey({ ...base, xml: base.xml + ' ', meta: claim }), cellKey({ ...base, meta: claim }));
  assert.equal(cellKey(JSON.parse(JSON.stringify(base))), cellKey(base));
});

test('start failures are infrastructure, a container exit is the expert\'s', { skip: SKIP }, async () => {
  const sc = scene();
  const nope = await runJob(cells, { scene: sc, expert: { argv: ['/nonexistent/expert'] }, time_limit_s: 2, wall_s: 10 });
  assert.deepEqual([nope.outcome, nope.infra], ['start_failed', true]);
  const dock = await runJob(cells, { scene: sc, expert: { argv: ['/nonexistent/docker'], docker: true, cidfile: '/nonexistent/c' }, time_limit_s: 2, wall_s: 10 });
  assert.deepEqual([dock.outcome, dock.infra], ['docker_unavailable', true]);
  const sh = (code) => ({ argv: ['/bin/sh', '-c', `exit ${code}`], docker: true, cidfile: path.join(HERE, `.nope-${process.pid}`) });
  const nocid = await runJob(cells, { scene: sc, expert: sh(125), time_limit_s: 2, wall_s: 10 });
  assert.deepEqual([nocid.outcome, nocid.infra], ['start_failed', true]);
  const cid = path.join(HERE, `.cid-${process.pid}`);
  const fs = await import('node:fs');
  fs.writeFileSync(cid, 'c');
  try {
    const ran = await runJob(cells, { scene: sc, expert: { ...sh(125), cidfile: cid }, time_limit_s: 2, wall_s: 10 });
    assert.deepEqual([ran.outcome, ran.infra], ['expert_error', false]);
  } finally { fs.rmSync(cid, { force: true }); }
  const held = await runJob(cells, { scene: sc, expert: fake('hold'), time_limit_s: 1, wall_s: 10 });
  assert.equal(held.infra, false);
  assert.ok(INFRA_OUTCOMES.includes('scene_error') && !INFRA_OUTCOMES.includes('expert_error'));
});

test('a trace replays the rows tick by tick and agrees with replay', { skip: SKIP }, async () => {
  const sc = scene();
  const r = await runJob(cells, { scene: sc, expert: fake('hold'), time_limit_s: 2, wall_s: 30 });
  const cell = cells.get(sc);
  const rp = replayCtrl(cell, sc, r.ctrl, r.n, 2);
  const tr = traceCtrl(cell, sc, r.ctrl, r.n, 2);
  assert.equal(tr.format, TRACE_FORMAT);
  assert.deepEqual([tr.outcome, tr.tick, tr.hash, tr.rows], [rp.outcome, rp.tick, rp.hash, rp.rows]);
  assert.equal(tr.ticks.length, tr.tick + 1);
  assert.equal(tr.ticks[0].row, null);
  const k = tr.ticks[5];
  assert.equal(k.row.length, 7);
  assert.equal(k.q.length, 6);
  assert.equal(k.tcp.length, 3);
  assert.equal(k.tcp_quat.length, 4);
  assert.ok(Array.isArray(k.held) && Array.isArray(k.touch) && typeof k.target.above_line_mm === 'number');
  assert.match(tr.reasons[0], /^timed_out at tick 40/);
  assert.ok(tr.reasons.some((x) => /never closed the grip/.test(x)));
  assert.ok(tr.reasons.some((x) => /never touched the target|first touched the target/.test(x)));
  const none = traceCtrl(cell, sc, '', 0, 1);
  assert.equal(none.rows, 0);
  assert.equal(none.held, 20);
  assert.ok(none.ticks.slice(1).every((x) => x.after_rows));
  assert.throws(() => traceCtrl(cell, sc, r.ctrl, r.n + 1, 2), /not n =/);
});

test('the action hash is FNV-1a 64 over little-endian float64 rows', () => {
  assert.equal(EMPTY_ACTION_HASH, 'cbf29ce484222325');
  assert.equal(actionHash([]), EMPTY_ACTION_HASH);
  assert.equal(actionHash([[0, 1, 2, 3, 4, 5, 0.5]]), actionHash([[-0, 1, 2, 3, 4, 5, 0.5]]));
  assert.notEqual(actionHash([[0, 1, 2, 3, 4, 5, 0.5]]), actionHash([[0, 1, 2, 3, 4, 5, 0.25]]));
  assert.equal(actionHash([[1, 2], [3]]), actionHash([[1], [2, 3]]));
  assert.equal(actionHash([[0, -1.5, 1.5, -1.5, -1.5, -0, 1]]), '6834ebbc8b4f0b58');
});

test('a session steps acts given one by one, like a run', { skip: SKIP }, async () => {
  const sc = scene();
  const r = await runJob(cells, { scene: sc, expert: fake('hold'), time_limit_s: 2, wall_s: 30 });
  const ss = new Session(cells, { scene: sc, time_limit_s: 2 });
  const { start } = ss.open();
  const q = [...sc.meta.robot.arm_joints_qpos.map((i) => start.qpos[i]), 0];
  let out, k = 0;
  do { out = ss.act({ type: 'act', rows: [q] }, 1); k++; } while (out.state);
  assert.equal(k, 40);
  assert.deepEqual([out.end.outcome, out.end.tick], ['timed_out', 40]);
  assert.deepEqual([out.result.outcome, out.result.ctrl, out.result.action_hash, out.result.rows_sent],
                   [r.outcome, r.ctrl, r.action_hash, 40]);
  assert.equal(r.action_hash, actionHash(Array(40).fill(q)));
  const bad = new Session(cells, { scene: sc, time_limit_s: 2 });
  bad.open();
  bad.act({ type: 'act', rows: [q, q] });
  const e = bad.act({ type: 'act', rows: [[1, 2]] });
  assert.deepEqual([e.result.outcome, e.result.rows_sent, e.result.n], ['expert_error', 2, 2]);
  const late = new Session(cells, { scene: sc, time_limit_s: 2 });
  late.open();
  const s = late.stop('step_timeout', 'late');
  assert.deepEqual([s.outcome, s.action_hash, s.n], ['step_timeout', EMPTY_ACTION_HASH, 0]);
});

test('matQuat turns a rotation matrix into w, x, y, z', () => {
  assert.deepEqual(matQuat([1, 0, 0, 0, 1, 0, 0, 0, 1]), [1, 0, 0, 0]);
  const q = matQuat([1, 0, 0, 0, -1, 0, 0, 0, -1]);
  assert.ok(Math.abs(q[1]) > 0.999 && Math.abs(q[0]) < 1e-9);
  const c = Math.SQRT1_2, z = matQuat([0, -1, 0, 1, 0, 0, 0, 0, 1]);
  assert.ok(Math.abs(z[0] - c) < 1e-9 && Math.abs(z[3] - c) < 1e-9);
});

test('a result carries the rows the simulator applied, clamped to the actuator ranges; the hash stays over the sent rows', { skip: SKIP }, () => {
  const sc = scene();
  const ss = new Session(cells, { scene: sc, time_limit_s: 2 });
  const { start } = ss.open();
  const q = [...sc.meta.robot.arm_joints_qpos.map((i) => start.qpos[i]), 0];
  const wild = [q[0], 9, q[2], q[3], -0, q[5], -1e6];
  const loud = [...q.slice(0, 6), 1e300];
  let out, k = 0;
  do { out = ss.act({ type: 'act', rows: [k % 2 ? loud : wild] }, 1); k++; } while (out.state);
  const res = out.result, lim = ctrlRanges(cells.get(sc));
  assert.equal(res.action_hash, actionHash(Array.from({ length: k }, (_, i) => (i % 2 ? loud : wild))));
  assert.deepEqual(Array.from(b64ToF64(res.ctrl).slice(0, 7)), wild);
  const ap = b64ToF64(res.ctrl_applied);
  assert.equal(ap.length, 7 * res.n);
  for (let i = 0; i < ap.length; i++) assert.ok(ap[i] >= lim[i % 7][0] && ap[i] <= lim[i % 7][1], `${i} ${ap[i]}`);
  assert.deepEqual(Array.from(ap.slice(0, 7)), [q[0], lim[1][1], q[2], q[3], 0, q[5], 0]);
  assert.equal(ap[13], 1);
  assert.ok(Object.is(ap[4], 0));
  assert.deepEqual(Array.from(appliedRows(cells.get(sc), Float64Array.from(q))), q);
});
