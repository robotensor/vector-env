import { createHash } from 'node:crypto';
import { mujoco, bundleFiles } from '@vector/sim/node.js';
import { Cell, Episode, Camera, pickTarget, classify, segBytes, b64encode, b64ToF64, CTRL, isPile, replayPicks, pickCount } from '@vector/sim';

export const FORMAT = 'vector-expert/2';
// pile scenes: several picks per scene
export const FORMAT_PICKS = 'vector-expert/3';
export const SCENE_DONE = 'done';

export function multiPick(cell) { return isPile(cell); }

export class SceneError extends Error {}

export class Cells {
  constructor(bundle, keep = 2) {
    this.bundle = bundle;
    this.keep = keep;
    this.cache = new Map();
    this.mj = null;
  }

  async init() { this.mj = await mujoco(); return this; }

  get(scene) {
    const key = cellKey(scene);
    let cell = this.cache.get(key);
    if (cell) {
      this.cache.delete(key);
      this.cache.set(key, cell);
      cell.scene = { ...cell.scene, scene_id: scene.scene_id };
      return cell;
    }
    cell = Cell.create(this.mj, bundleFiles(this.bundle, scene).files, { scene_id: scene.scene_id, xml: scene.xml, meta: scene.meta });
    this.cache.set(key, cell);
    while (this.cache.size > this.keep) {
      const [k, old] = this.cache.entries().next().value;
      this.cache.delete(k);
      old.dispose();
    }
    return cell;
  }
}

export function cellKey(scene) {
  const h = createHash('sha256');
  h.update(String(scene.xml));
  h.update('\0');
  h.update(JSON.stringify(scene.meta ?? null));
  return h.digest('hex');
}

export function setLimit(cell, timeLimitS) {
  if (timeLimitS !== undefined && timeLimitS !== null) {
    if (!(Number.isFinite(timeLimitS) && timeLimitS > 0)) throw new SceneError('time_limit_s must be a positive number');
    cell.cfg.rules.try_s = timeLimitS;
  }
  return cell.cfg.rules.try_s;
}

export function ctrlRanges(cell) {
  const m = cell.m, R = cell.R, cr = m.actuator_ctrlrange, on = m.actuator_ctrllimited;
  const out = R.arm_actuators.map((a) => (on[a] ? [cr[2 * a], cr[2 * a + 1]] : [-Infinity, Infinity]));
  const g = R.gripper_actuator, gmax = cell.cfg.control.gripper_ctrl_max;
  out.push(on[g] ? [Math.max(0, cr[2 * g] / gmax), Math.min(1, cr[2 * g + 1] / gmax)] : [0, 1]);
  return out;
}

export function appliedRows(cell, flat) {
  const lim = ctrlRanges(cell), out = Float64Array.from(flat);
  for (let k = 0; k < out.length; k++) {
    const [lo, hi] = lim[k % CTRL];
    const x = Math.min(hi, Math.max(lo, out[k]));
    out[k] = x === 0 ? 0 : x;
  }
  return out;
}

export function rowRanges(cell) {
  return [...cell.R.joint_range.map(([lo, hi]) => [lo, hi]), [0, 1]];
}

export function outOfRange(cell, flat, eps = 1e-9) {
  const lim = rowRanges(cell);
  for (let k = 0; k < flat.length; k++) {
    const [lo, hi] = lim[k % CTRL];
    if (!(flat[k] >= lo - eps && flat[k] <= hi + eps)) return { tick: Math.floor(k / CTRL), col: k % CTRL, value: flat[k] };
  }
  return null;
}

export function limits(cell) {
  const tickS = cell.meta.tick_s, t = cell.cfg.rules.try_s;
  return { time_limit_s: t, control_hz: Math.round(1 / tickS), tick_s: tickS, max_rows: Math.ceil(t / tickS - 1e-9) };
}

export function settle(cell, seed) {
  const ep = new Episode(cell);
  ep.beginFill();
  while (!ep.fillStep());
  ep.sealSetup();
  const rain = ep.fill?.F?.mode === 'rain', hid = cell.meta.fill?.hidden_target;
  const cover = rain ? ep.fill.cover ?? -1 : Number.isInteger(hid) ? hid : null;
  const { target, target_class, counts } = pickTarget(cell, seed, cover === null ? {} : { cover });
  const out = { checkpoint: ep.checkpoint(), state_hash: cell.hash(), target, parts: cell.activeParts().length };
  if (target_class !== null) Object.assign(out, { target_class, class_counts: counts });
  if (rain) out.fill = { level: round(ep.fill.level, 4), spawned: ep.fill.spawned, ticks: ep.fill.k, cover: ep.fill.cover ?? null, asleep: cell.partsAsleep() };
  if (cell.sleep && !rain) out.fill = { asleep: cell.partsAsleep(), ticks: ep.fill.k, cover };
  // pile scenes built with freeze_deep: the settled pose and pile depth of every active part, for the second stage
  if (rain && cell.cfg.rain?.freeze_deep) {
    const C = classify(cell), P = cell.meta.parts, d = cell.d, act = cell.activeParts();
    out.parts_state = act.map((i) => ({ i, depth: C.depth[i], cls: C.cls[i],
      pose: [...Array.from(d.qpos.subarray(P[i].qpos, P[i].qpos + 7))] }));
  }
  return out;
}

export function restore(cell, scene) {
  if (!scene.checkpoint) throw new SceneError('the scene has no checkpoint');
  if (typeof scene.state_hash !== 'string' || !scene.state_hash) throw new SceneError('the scene has no state hash');
  let ep;
  try {
    ep = Episode.restoreCheckpoint(cell, scene.checkpoint);
  } catch (e) {
    throw new SceneError(`checkpoint does not fit the scene: ${e.message}`);
  }
  const hash = cell.hash();
  if (hash !== scene.state_hash) throw new SceneError(`state hash ${hash} is not the scene's ${scene.state_hash}`);
  const t = scene.target;
  if (!(Number.isInteger(t) && t >= 0 && t < cell.n) || !cell.active[t]) throw new SceneError('the target is not a part in the bin');
  return ep;
}

// camera + img: an image that camera just rendered of this state (reused, not rendered again)
export function observation(cell, { rgb = true, camera = null, img = null } = {}) {
  const cam = camera && img ? camera : new Camera(cell);
  try {
    if (!(camera && img)) img = cam.render();
    const o = { width: img.width, height: img.height, camera: cell.meta.camera,
                depth: b64encode(new Uint8Array(img.depth.buffer)), seg: b64encode(segBytes(img.seg)) };
    if (img.seg_dtype) o.seg_dtype = img.seg_dtype;
    if (rgb) o.rgb = b64encode(cam.renderRGB(img));
    return o;
  } finally {
    if (cam !== camera) cam.dispose();
  }
}

export function physState(cell) {
  return { qpos: Array.from(cell.d.qpos), qvel: Array.from(cell.d.qvel) };
}

// A multi-pick scene's start for PickRun / replayPicks.
export function pickStart(cell, scene) {
  return { target: scene.target, target_class: scene.target_class ?? null, seed: cell.meta.seed ?? 0, picks: pickCount(cell) };
}

// The scene part of a multi-pick result: per-pick records (pick, target, target_class, outcome, t, tick, outs,
// out_parts, tcp_path_m, joint_path_rad, crate_hits) and the totals.
export function sceneSummary(run, { partial = true } = {}) {
  const recs = run.picks.map(({ part, detail, ...r }) => r);
  const p = partial ? run.partial() : null;
  if (p) { const { part, detail, ...r } = p; recs.push(r); }
  return { picks: recs, ...run.totals(p ? [...run.picks, p] : run.picks) };
}

export function replayCtrl(cell, scene, ctrl, n, timeLimitS) {
  setLimit(cell, timeLimitS);
  const ep = restore(cell, scene);
  if (multiPick(cell)) {
    const flat = typeof ctrl === 'string' ? b64ToF64(ctrl) : Float64Array.from(ctrl || []);
    if (flat.length % CTRL) throw new SceneError(`ctrl length ${flat.length} is not a multiple of ${CTRL}`);
    if (n !== undefined && n !== null && n !== flat.length / CTRL) throw new SceneError(`ctrl holds ${flat.length / CTRL} rows, not n = ${n}`);
    for (let k = 0; k < flat.length; k++) if (!Number.isFinite(flat[k])) throw new SceneError('ctrl has a non-finite value');
    const R = replayPicks(ep, pickStart(cell, scene), flat, { hold: true });
    const S = sceneSummary(R.run, { partial: false }), last = R.run.picks[R.run.picks.length - 1];
    const outcome = R.run.done ? (last && last.outcome === 'physics_error' ? 'physics_error' : SCENE_DONE) : null;
    return { outcome, success: S.successes === S.picks_planned, ...S, t: S.t, tick: S.tick, hash: cell.hash(),
             detail: last ? last.detail : null, rows: R.rows, held: R.held };
  }
  const flat = typeof ctrl === 'string' ? b64ToF64(ctrl) : Float64Array.from(ctrl || []);
  if (flat.length % CTRL) throw new SceneError(`ctrl length ${flat.length} is not a multiple of ${CTRL}`);
  const rows = flat.length / CTRL;
  if (n !== undefined && n !== null && n !== rows) throw new SceneError(`ctrl holds ${rows} rows, not n = ${n}`);
  ep.beginTry(scene.target, scene.target_class ?? null);
  let v = null, k = 0, row = null;
  for (; k < rows && !v; k++) {
    row = Array.from(flat.subarray(CTRL * k, CTRL * (k + 1)));
    if (!row.every(Number.isFinite)) throw new SceneError('ctrl has a non-finite value');
    v = ep.stepTry(row);
  }
  let held = 0;
  if (!v) {
    if (!row) { row = cell.ctrl().slice(); row[6] = 0; }
    while (!v) { v = ep.stepTry(row); held++; }
  }
  const tick = ep.lastTry.verdict.tick;
  return { outcome: v.outcome, success: v.outcome === 'success', t: round(tick * cell.meta.tick_s), tick, hash: cell.hash(),
           detail: v.detail, rows: k, held, ...tryMetrics(ep.lastTry.mon) };
}

// The per-try metrics of a monitor (every scene): outs, out_parts, tcp_path_m, joint_path_rad, crate_hits, target_class.
export function tryMetrics(mon) {
  return mon ? mon.metrics() : { outs: 0, out_parts: [], tcp_path_m: 0, joint_path_rad: 0, crate_hits: 0, target_class: null };
}

export function round(x, d = 6) {
  const f = 10 ** d;
  return Math.round(x * f) / f;
}
