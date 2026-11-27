import { spawn } from 'node:child_process';
import fs from 'node:fs';
import { CTRL, b64ToF64, b64encode, PickRun, Camera } from '@vector/sim';
import { FORMAT, FORMAT_PICKS, SCENE_DONE, SceneError, setLimit, limits, restore, observation, physState, round, appliedRows, tryMetrics,
         multiPick, pickStart, sceneSummary } from './env.js';

const STDERR_TAIL = 2000;
const STDERR_FILE_MAX = 16 << 20;
export const INFRA_OUTCOMES = ['harness_error', 'scene_error', 'docker_unavailable', 'image_missing', 'pull_failed', 'start_failed', 'policy_unavailable'];
const IO = { max_line_kib: 1024, max_queued: 4, max_read_mib: 64, max_unread_mib: 16 };

function ioLimits(io) {
  const o = { ...IO };
  for (const k of Object.keys(IO)) if (io && Number(io[k]) > 0) o[k] = Number(io[k]);
  return { line: 1024 * o.max_line_kib, queued: Math.floor(o.max_queued), read: 1048576 * o.max_read_mib,
           unread: 1048576 * o.max_unread_mib };
}

class Expert {
  constructor(argv, env, cwd, io, stderrFile) {
    this.lim = ioLimits(io);
    this.lines = [];
    this.waiter = null;
    this.closed = false;
    this.stderr = '';
    this.exit = null;
    this.broken = null;
    this.buf = Buffer.alloc(0);
    this.read = 0;
    this.p = spawn(argv[0], argv.slice(1), { stdio: ['pipe', 'pipe', 'pipe'], cwd: cwd || process.cwd(), env: { ...process.env, ...(env || {}) } });
    this.p.stdout.on('data', (b) => this._data(b));
    this.p.stderr.setEncoding('utf8');
    this.log = null;
    this.logged = 0;
    if (stderrFile) {
      try {
        this.log = fs.createWriteStream(stderrFile, { flags: 'a' });
        this.log.on('error', () => { this.log = null; });
      } catch { this.log = null; }
    }
    this.p.stderr.on('data', (s) => {
      this.stderr = (this.stderr + s).slice(-STDERR_TAIL);
      if (this.log && this.logged < STDERR_FILE_MAX) { this.log.write(s.slice(0, STDERR_FILE_MAX - this.logged)); this.logged += s.length; }
    });
    this.p.stderr.on('close', () => { if (this.log) { this.log.end(); this.log = null; } });
    this.p.stdin.on('error', () => {});
    this.spawned = false;
    this.spawnError = null;
    this.p.on('spawn', () => { this.spawned = true; });
    this.p.on('error', (e) => { if (!this.spawned) this.spawnError = e.message; this.stderr += `\n${e.message}`; this._close(); });
    this.p.on('close', (code, sig) => { this.exit = sig || code; this._close(); });
  }

  _data(b) {
    if (this.broken) return;
    const L = this.lim;
    this.read += b.length;
    if (this.read > L.read) return this._break(`the expert wrote more than ${L.read >> 20} MiB`);
    this.buf = this.buf.length ? Buffer.concat([this.buf, b]) : b;
    let i;
    while ((i = this.buf.indexOf(10)) >= 0) {
      if (i > L.line) return this._break(`a message is longer than ${L.line >> 10} KiB`);
      const line = this.buf.toString('utf8', 0, i);
      this.buf = this.buf.subarray(i + 1);
      if (line.trim()) this._push(line);
      if (this.lines.length > L.queued) return this._break(`the expert sent more than ${L.queued} messages without waiting for a state`);
    }
    if (this.buf.length > L.line) return this._break(`a message is longer than ${L.line >> 10} KiB`);
  }

  _break(why) {
    this.broken = why;
    this.buf = Buffer.alloc(0);
    this.lines = [];
    try { this.p.stdout.destroy(); } catch {}
    this._close();
  }

  _push(line) {
    if (this.waiter) { const w = this.waiter; this.waiter = null; w(line); } else this.lines.push(line);
  }

  _close() {
    if (this.closed) return;
    this.closed = true;
    if (this.waiter) { const w = this.waiter; this.waiter = null; w(null); }
  }

  send(obj) {
    if (this.closed || this.p.stdin.destroyed) return false;
    if (this.p.stdin.writableLength > this.lim.unread) {
      this._break(`the expert left more than ${this.lim.unread >> 20} MiB of its input unread`);
      return false;
    }
    this.p.stdin.write(JSON.stringify(obj) + '\n');
    return true;
  }

  next() {
    if (this.broken) return Promise.resolve(null);
    if (this.lines.length) return Promise.resolve(this.lines.shift());
    if (this.closed) return Promise.resolve(null);
    return new Promise((res) => { this.waiter = res; });
  }

  async finish(graceS, kill) {
    try { this.p.stdin.end(); } catch {}
    if (!this.closed) await Promise.race([new Promise((r) => this.p.once('close', r)), sleep(1000 * graceS)]);
    if (!this.closed) await this.kill(kill);
  }

  async kill(kill) {
    if (kill && kill.length) {
      await new Promise((r) => {
        const k = spawn(kill[0], kill.slice(1), { stdio: 'ignore' });
        k.on('close', r);
        k.on('error', r);
        setTimeout(r, 10000);
      });
    }
    try { this.p.kill('SIGKILL'); } catch {}
  }
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

class ProtocolError extends Error {}

export const MAX_ABS = 100;
export const SAFETY_S = 14400;

function parseAct(line, maxRows, maxAbs = MAX_ABS) {
  let m = line;
  if (typeof line === 'string') {
    try { m = JSON.parse(line); } catch { throw new ProtocolError(`not JSON: ${line.slice(0, 120)}`); }
  }
  if (!m || typeof m !== 'object') throw new ProtocolError('a message must be a JSON object');
  if (m.type === 'error') throw new ProtocolError(`the expert gave up: ${String(m.error || '').slice(0, 300)}`);
  if (m.type !== 'act') throw new ProtocolError(`expected an act message, got ${JSON.stringify(m.type)}`);
  const rows = m.rows;
  if (!Array.isArray(rows)) throw new ProtocolError('act.rows must be a list of rows');
  if (rows.length > maxRows) throw new ProtocolError(`act has ${rows.length} rows, more than the ${maxRows} left`);
  for (const r of rows) {
    if (!Array.isArray(r) || r.length !== CTRL || !r.every((x) => typeof x === 'number' && Number.isFinite(x)))
      throw new ProtocolError(`a row is ${CTRL} finite numbers (6 joint targets in rad, grip 0..1)`);
    for (const x of r) if (Math.abs(x) > maxAbs) throw new ProtocolError(`a row value ${x} is outside -${maxAbs}..${maxAbs}`);
  }
  return { rows, obs: m.obs === true };
}

function stats(xs) {
  if (!xs.length) return { n: 0, mean_ms: null, p50_ms: null, max_ms: null };
  const s = xs.slice().sort((a, b) => a - b);
  return { n: xs.length, mean_ms: round(xs.reduce((a, b) => a + b, 0) / xs.length, 4), p50_ms: round(s[s.length >> 1], 4),
           max_ms: round(s[s.length - 1], 4) };
}

const FNV_OFFSET = 0xcbf29ce484222325n;
const FNV_PRIME = 0x100000001b3n;
const M64 = (1n << 64n) - 1n;
export const EMPTY_ACTION_HASH = FNV_OFFSET.toString(16).padStart(16, '0');

export class ActionHash {
  constructor() { this.h = FNV_OFFSET; this.rows = 0; this.view = new DataView(new ArrayBuffer(8)); }

  add(rows) {
    const v = this.view;
    let h = this.h;
    for (const r of rows) {
      for (const x of r) {
        v.setFloat64(0, x === 0 ? 0 : x, true);
        for (let k = 0; k < 8; k++) h = ((h ^ BigInt(v.getUint8(k))) * FNV_PRIME) & M64;
      }
      this.rows++;
    }
    this.h = h;
    return this;
  }

  hex() { return this.h.toString(16).padStart(16, '0'); }
}

export function actionHash(rows) { return new ActionHash().add(rows).hex(); }

export class Session {
  constructor(cells, job) {
    this.job = job;
    this.t0 = performance.now();
    this.sc = job.scene || {};
    this.res = { scene_id: this.sc.scene_id ?? null, outcome: null, success: false, t: 0, tick: 0, ctrl: '', n: 0, wall_s: 0,
                 error: null, exchanges: 0, action_hash: EMPTY_ACTION_HASH, rows_sent: 0 };
    this.hash = new ActionHash();
    this.wait = [];
    // the row count of every accepted act in order, the rows dropped after a verdict, the expert's time per pick (ms)
    this.actRows = [];
    this.dropped = [];
    this.expertMs = [];
    // vector-expert/3: the FNV-1a hash and the count of the rows the expert sent while each pick was current
    this.pickHash = [];
    this.pickRows = [];
    this.sim = [];
    this.verdict = null;
    this.ended = false;
    this.cells = cells;
    this.maxAbs = Number(job.max_abs) > 0 ? Number(job.max_abs) : MAX_ABS;
  }

  _done(outcome, error = null) {
    const res = this.res, ep = this.ep;
    if (this.run) {
      const S = sceneSummary(this.run);
      for (const p of S.picks) if (p.outcome === null) p.outcome = outcome;
      Object.assign(res, S, { target_class: this.sc.target_class ?? null });
      res.t = S.t;
      res.tick = S.tick;
    } else {
      Object.assign(res, tryMetrics(ep ? (ep.cur || ep.lastTry || {}).mon : null));
      res.target_class ??= this.sc.target_class ?? null;
    }
    res.outcome = outcome;
    res.success = this.run ? res.successes === res.picks_planned : outcome === 'success';
    res.error = error;
    res.infra = INFRA_OUTCOMES.includes(outcome);
    res.wall_s = round((performance.now() - this.t0) / 1000, 3);
    res.action_hash = this.hash.hex();
    res.rows_sent = this.hash.rows;
    res.act_rows = this.actRows.slice();
    res.dropped_ctrl = this.dropped.length ? b64encode(new Uint8Array(Float64Array.from(this.dropped.flat()).buffer)) : '';
    res.expert_s = round(this.expertMs.reduce((a, b) => a + (b || 0), 0) / 1000);
    if (this.run) {
      for (const p of res.picks) {
        p.expert_s = round((this.expertMs[p.pick] || 0) / 1000);
        p.action_hash = this.pickHash[p.pick] ? this.pickHash[p.pick].hex() : EMPTY_ACTION_HASH;
        p.rows_sent = this.pickRows[p.pick] || 0;
      }
    }
    this.ended = true;
    this.close();
    if (this.camera) { this.camera.dispose(); this.camera = null; }
    return res;
  }

  open() {
    const sc = this.sc, job = this.job;
    try {
      this.cell = this.cells.get(sc);
      setLimit(this.cell, job.time_limit_s);
      this.ep = restore(this.cell, sc);
    } catch (e) {
      return { result: this._done('scene_error', String(e.message || e)) };
    }
    const cell = this.cell;
    this.lim = limits(cell);
    this.tickS = cell.meta.tick_s;
    const multi = multiPick(cell);
    const start = { type: 'start', format: multi ? FORMAT_PICKS : FORMAT, scene: { scene_id: sc.scene_id ?? null, xml: sc.xml, meta: sc.meta },
                    checkpoint: sc.checkpoint, state_hash: sc.state_hash, target: sc.target,
                    tick: 0, t: 0, ...physState(cell), obs: observation(cell, { rgb: job.rgb !== false }), limits: this.lim };
    this.res.setup_s = round((performance.now() - this.t0) / 1000, 3);
    if (multi) {
      // several picks: the rows of the whole scene, in order; the run chooses each next target
      this.camera = new Camera(cell);
      this.run = new PickRun(this.ep, { ...pickStart(cell, sc), camera: this.camera });
      this.rows = [];
      Object.assign(start, { pick: 0, picks: this.run.planned });
      const keys = Object.keys(start);
      const order = ['type', 'format', 'scene', 'checkpoint', 'state_hash', 'target', 'pick', 'picks'];
      const sorted = {};
      for (const k of [...order, ...keys.filter((x) => !order.includes(x))]) sorted[k] = start[k];
      this.run.start();
      return { start: sorted };
    }
    this.ep.beginTry(sc.target, sc.target_class ?? null);
    return { start };
  }

  elapsedS() { return (performance.now() - this.t0) / 1000; }

  // the expert's own time left (ms): its pick's wall budget, and the scene's (wall_s x picks)
  expertLeftMs(wallS) {
    const k = this.run ? Math.max(0, this.run.k) : 0, P = this.run ? this.run.planned : 1;
    const total = this.expertMs.reduce((a, b) => a + (b || 0), 0);
    return { k, ms: Math.min(1000 * wallS - (this.expertMs[k] || 0), 1000 * wallS * P - total) };
  }

  // the expert's wait for a message it never answered (the scene ended first), counted in its pick's expert time
  addWait(ms) {
    if (!Number.isFinite(ms) || this.ended) return;
    const k = this.run ? Math.max(0, this.run.k) : 0;
    this.expertMs[k] = (this.expertMs[k] || 0) + Math.max(0, ms);
  }

  _ctrlSoFar() {
    const ep = this.ep, res = this.res;
    if (!ep) return;
    if (this.run) {
      const flat = Float64Array.from(this.rows.flat());
      res.ctrl = flat.length ? b64encode(new Uint8Array(flat.buffer)) : '';
      res.n = this.rows.length;
      res.ctrl_applied = flat.length ? b64encode(new Uint8Array(appliedRows(this.cell, flat).buffer)) : '';
      return;
    }
    if (ep.lastTry) { res.ctrl = ep.lastTry.seg.ctrl; res.n = ep.lastTry.ticks; }
    else if (ep.cur) { res.n = ep.cur.ticks; res.ctrl = Buffer.from(Float64Array.from(ep.cur.ctrl).buffer).toString('base64'); }
    res.ctrl_applied = res.ctrl ? b64encode(new Uint8Array(appliedRows(this.cell, b64ToF64(res.ctrl)).buffer)) : '';
    res.tick = ep.cur ? ep.cur.ticks : ep.lastTry ? ep.lastTry.ticks : 0;
    res.t = round(res.tick * this.tickS);
  }

  act(msg, waitMs) {
    if (this.ended) throw new Error('the session has ended');
    const ep = this.ep, cell = this.cell;
    if (Number.isFinite(waitMs)) {
      this.wait.push(waitMs);
      const k = this.run ? Math.max(0, this.run.k) : 0;
      this.expertMs[k] = (this.expertMs[k] || 0) + Math.max(0, waitMs);
    }
    this.res.exchanges++;
    if (this.run) return this._actPicks(msg);
    let act;
    try {
      act = parseAct(msg, this.lim.max_rows - ep.cur.ticks, this.maxAbs);
    } catch (e) {
      if (e instanceof ProtocolError) return { result: this.stop('expert_error', e.message) };
      throw e;
    }
    this.hash.add(act.rows);
    this.actRows.push(act.rows.length);
    const s0 = performance.now();
    let verdict = null;
    if (act.rows.length === 0) {
      let row = ep.cur.ctrl.length ? ep.cur.ctrl.slice(-CTRL) : null;
      if (!row) { row = cell.ctrl().slice(); row[6] = 0; }
      while (!verdict) verdict = ep.stepTry(row);
    } else {
      for (let j = 0; j < act.rows.length && !verdict; j++) {
        verdict = ep.stepTry(act.rows[j]);
        if (verdict) for (const r of act.rows.slice(j + 1)) this.dropped.push(r);
      }
    }
    if (!verdict) {
      const state = { type: 'state', tick: ep.cur.ticks, t: round(ep.cur.ticks * this.tickS), ...physState(cell) };
      if (act.obs) state.obs = observation(cell, { rgb: this.job.rgb !== false });
      this.sim.push(performance.now() - s0);
      return { state };
    }
    this.sim.push(performance.now() - s0);
    this._ctrlSoFar();
    const tick = ep.lastTry.verdict.tick;
    const end = { type: 'end', outcome: verdict.outcome, t: round(tick * this.tickS), tick };
    this.res.detail = verdict.detail;
    this._stats();
    return { end, result: this._done(verdict.outcome) };
  }

  // vector-expert/3: the act's rows run in the current pick; at its verdict the rest of the act is dropped (it stays in
  // the action hash) and the expert gets a pick message, then the next pick's rows; end after the last pick.
  _actPicks(msg) {
    const run = this.run, ep = this.ep, cell = this.cell;
    let act;
    try {
      act = parseAct(msg, this.lim.max_rows - ep.cur.ticks, this.maxAbs);
    } catch (e) {
      if (e instanceof ProtocolError) return { result: this.stop('expert_error', e.message) };
      throw e;
    }
    this.hash.add(act.rows);
    this.actRows.push(act.rows.length);
    const k = run.k;
    (this.pickHash[k] ||= new ActionHash()).add(act.rows);
    this.pickRows[k] = (this.pickRows[k] || 0) + act.rows.length;
    const s0 = performance.now();
    let r = null;
    const step = (row) => { this.rows.push(row.slice()); return run.step(row); };
    if (act.rows.length === 0) {
      let row = this.rows.length ? this.rows[this.rows.length - 1] : null;
      if (!row) { row = cell.ctrl().slice(); row[6] = 0; }
      while (!r) r = step(row);
    } else {
      for (let j = 0; j < act.rows.length && !r; j++) {
        r = step(act.rows[j]);
        if (r) for (const x of act.rows.slice(j + 1)) this.dropped.push(x);
      }
    }
    this.sim.push(performance.now() - s0);
    if (!r) {
      const state = { type: 'state', pick: run.k, tick: ep.cur.ticks, t: round(ep.cur.ticks * this.tickS), ...physState(cell) };
      if (act.obs) state.obs = observation(cell, { rgb: this.job.rgb !== false });
      return { state };
    }
    const rec = r.record;
    const pick = { type: 'pick', pick: rec.pick, outcome: rec.outcome, t: rec.t, tick: rec.tick, outs: rec.outs,
                   next: r.next ? { pick: r.next.pick, target: r.next.target } : null, ...physState(cell) };
    if (r.next) {
      pick.obs = observation(cell, { rgb: this.job.rgb !== false, camera: this.camera, img: run.lastImage() });
      return { state: pick };
    }
    this._ctrlSoFar();
    this.res.detail = rec.detail;
    this._stats();
    const outcome = rec.outcome === 'physics_error' ? 'physics_error' : SCENE_DONE;
    const result = this._done(outcome, outcome === 'physics_error' ? rec.detail : null);
    const end = { type: 'end', outcome, t: result.t, tick: result.tick,
                  picks: result.picks.map((p) => ({ pick: p.pick, outcome: p.outcome, t: p.t, tick: p.tick, outs: p.outs })),
                  successes: result.successes, outs: result.outs };
    return { pick, end, result };
  }

  _stats() {
    this.res.exchange = stats(this.wait);
    this.res.sim_ms = round(this.sim.reduce((a, b) => a + b, 0), 3);
  }

  stop(outcome, error = null) {
    if (this.ended) return this.res;
    this._ctrlSoFar();
    this._stats();
    return this._done(outcome, error);
  }

  close() {
    if (this.ep && this.ep.cur) this.ep.abortTry();
  }
}

// job.wall_s is the budget of one pick; a multi-pick scene gets it once per pick.
// job.wall_s bounds the expert's own time (expert_s) in one pick, and wall_s x picks in the scene; the simulation does
// not count. job.safety_s bounds the scene's elapsed time: past it the scene is an infra failure (harness_error).
export async function runJob(cells, job) {
  const wallS = Number(job.wall_s) > 0 ? Number(job.wall_s) : 45;
  const safetyS = Number(job.safety_s) > 0 ? Number(job.safety_s) : SAFETY_S;
  const graceS = Number(job.grace_s) >= 0 ? Number(job.grace_s) : 2;
  const ss = new Session(cells, job);
  const tail = (ex) => { const t = ex.stderr.trim().split('\n').slice(-5).join(' | '); return t ? ': ' + t : ''; };
  const cid = job.expert && job.expert.cidfile;
  const neverStarted = () => !!cid && ss.res.exchanges === 0 && !fs.existsSync(cid);
  const opened = ss.open();
  if (opened.result) return opened.result;
  const ex = new Expert(job.expert.argv, job.expert.env, job.expert.cwd, job.io, job.expert.stderr_file);
  let timer;
  try {
    ex.send(opened.start);
    let sent = performance.now();
    for (;;) {
      const left = ss.expertLeftMs(wallS), safe = 1000 * safetyS - (performance.now() - ss.t0);
      let fired = null;
      const cap = new Promise((r) => {
        timer = setTimeout(() => { fired = safe <= left.ms ? 'safety' : 'wall'; r(null); }, Math.max(0, Math.min(left.ms, safe)));
      });
      const line = await Promise.race([ex.next(), cap]);
      clearTimeout(timer);
      const got = performance.now();
      if (fired) {
        ss.addWait(got - sent);
        await ex.kill(job.expert.kill);
        if (fired === 'safety') return ss.stop('harness_error', `the scene ran past the harness's safety cap of ${safetyS} s of elapsed time`);
        if (neverStarted()) return ss.stop('start_failed', `the container did not start within ${wallS} s${tail(ex)}`);
        return ss.stop('wall_timeout', `the expert used up its wall budget of ${wallS} s of its own time` +
                                       (ss.run ? ` in pick ${left.k}` : ''));
      }
      if (line === null) {
        await ex.kill(job.expert.kill);
        if (ex.broken) return ss.stop('expert_error', ex.broken);
        if (ex.spawnError) return ss.stop(job.expert.docker ? 'docker_unavailable' : 'start_failed', `could not start the expert: ${ex.spawnError}`);
        if (neverStarted()) return ss.stop('start_failed', `the container was not created (docker exited ${ex.exit})${tail(ex)}`);
        return Object.assign(ss.stop('expert_error', `the expert exited (${ex.exit})${tail(ex)}`), { exit_code: ex.exit });
      }
      const out = ss.act(line, got - sent);
      if (out.state) {
        sent = performance.now();
        ex.send(out.state);
        continue;
      }
      clearTimeout(timer);
      if (!out.end) { await ex.kill(job.expert.kill); return out.result; }
      if (out.pick) ex.send(out.pick);
      ex.send(out.end);
      await ex.finish(graceS, job.expert.kill);
      return out.result;
    }
  } catch (e) {
    clearTimeout(timer);
    await ex.kill(job.expert.kill);
    return ss.stop('harness_error', String(e.stack || e.message || e).slice(0, 500));
  } finally {
    clearTimeout(timer);
    ss.close();
  }
}

export function ctrlRows(b64) {
  const f = b64ToF64(b64), out = [];
  for (let k = 0; k < f.length; k += CTRL) out.push(Array.from(f.subarray(k, k + CTRL)));
  return out;
}
