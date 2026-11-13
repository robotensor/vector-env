import { mujoco, bundleFiles } from '@vector/sim/node.js';
import { Cell, Episode, Camera, chooseTarget, b64encode, b64ToF64, CTRL } from '@vector/sim';

export const FORMAT = 'vector-expert/2';

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
    const key = scene.meta.xml_sha256 || scene.xml;
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

export function setLimit(cell, timeLimitS) {
  if (timeLimitS !== undefined && timeLimitS !== null) {
    if (!(Number.isFinite(timeLimitS) && timeLimitS > 0)) throw new SceneError('time_limit_s must be a positive number');
    cell.cfg.rules.try_s = timeLimitS;
  }
  return cell.cfg.rules.try_s;
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
  const target = chooseTarget(cell, seed);
  return { checkpoint: ep.checkpoint(), state_hash: cell.hash(), target, parts: cell.activeParts().length };
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

export function observation(cell, { rgb = true } = {}) {
  const cam = new Camera(cell);
  try {
    const img = cam.render();
    const o = { width: img.width, height: img.height, camera: cell.meta.camera,
                depth: b64encode(new Uint8Array(img.depth.buffer)), seg: b64encode(img.seg) };
    if (rgb) o.rgb = b64encode(cam.renderRGB(img));
    return o;
  } finally {
    cam.dispose();
  }
}

export function physState(cell) {
  return { qpos: Array.from(cell.d.qpos), qvel: Array.from(cell.d.qvel) };
}

export function replayCtrl(cell, scene, ctrl, n, timeLimitS) {
  setLimit(cell, timeLimitS);
  const ep = restore(cell, scene);
  const flat = typeof ctrl === 'string' ? b64ToF64(ctrl) : Float64Array.from(ctrl || []);
  if (flat.length % CTRL) throw new SceneError(`ctrl length ${flat.length} is not a multiple of ${CTRL}`);
  const rows = flat.length / CTRL;
  if (n !== undefined && n !== null && n !== rows) throw new SceneError(`ctrl holds ${rows} rows, not n = ${n}`);
  ep.beginTry(scene.target);
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
           detail: v.detail, rows: k, held };
}

export function round(x, d = 6) {
  const f = 10 ** d;
  return Math.round(x * f) / f;
}
