import { spawn } from 'node:child_process';
import fs from 'node:fs';
import { CTRL, b64ToF64 } from '@vector/sim';
import { FORMAT, SceneError, setLimit, limits, restore, observation, physState, round } from './env.js';

const STDERR_TAIL = 2000;
const STDERR_FILE_MAX = 16 << 20;
export const INFRA_OUTCOMES = ['harness_error', 'scene_error', 'docker_unavailable', 'image_missing', 'pull_failed', 'start_failed'];
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

function parseAct(line, maxRows) {
  let m;
  try { m = JSON.parse(line); } catch { throw new ProtocolError(`not JSON: ${line.slice(0, 120)}`); }
  if (!m || typeof m !== 'object') throw new ProtocolError('a message must be a JSON object');
  if (m.type === 'error') throw new ProtocolError(`the expert gave up: ${String(m.error || '').slice(0, 300)}`);
  if (m.type !== 'act') throw new ProtocolError(`expected an act message, got ${JSON.stringify(m.type)}`);
  const rows = m.rows;
  if (!Array.isArray(rows)) throw new ProtocolError('act.rows must be a list of rows');
  if (rows.length > maxRows) throw new ProtocolError(`act has ${rows.length} rows, more than the ${maxRows} left`);
  for (const r of rows) {
    if (!Array.isArray(r) || r.length !== CTRL || !r.every((x) => typeof x === 'number' && Number.isFinite(x)))
      throw new ProtocolError(`a row is ${CTRL} finite numbers (6 joint targets in rad, grip 0..1)`);
  }
  return { rows, obs: m.obs === true };
}

function stats(xs) {
  if (!xs.length) return { n: 0, mean_ms: null, p50_ms: null, max_ms: null };
  const s = xs.slice().sort((a, b) => a - b);
  return { n: xs.length, mean_ms: round(xs.reduce((a, b) => a + b, 0) / xs.length, 4), p50_ms: round(s[s.length >> 1], 4),
           max_ms: round(s[s.length - 1], 4) };
}

export async function runJob(cells, job) {
  const t0 = performance.now();
  const wallS = Number(job.wall_s) > 0 ? Number(job.wall_s) : 45;
  const graceS = Number(job.grace_s) >= 0 ? Number(job.grace_s) : 2;
  const sc = job.scene || {};
  const res = { scene_id: sc.scene_id ?? null, outcome: null, success: false, t: 0, tick: 0, ctrl: '', n: 0, wall_s: 0,
                error: null, exchanges: 0 };
  const done = (outcome, error = null) => {
    res.outcome = outcome;
    res.success = outcome === 'success';
    res.error = error;
    res.infra = INFRA_OUTCOMES.includes(outcome);
    res.wall_s = round((performance.now() - t0) / 1000, 3);
    return res;
  };
  const tail = (ex) => { const t = ex.stderr.trim().split('\n').slice(-5).join(' | '); return t ? ': ' + t : ''; };
  const cid = job.expert && job.expert.cidfile;
  const neverStarted = () => !!cid && res.exchanges === 0 && !fs.existsSync(cid);
  let cell, ep;
  try {
    cell = cells.get(sc);
    setLimit(cell, job.time_limit_s);
    ep = restore(cell, sc);
  } catch (e) {
    return done('scene_error', String(e.message || e));
  }
  const lim = limits(cell);
  const tickS = cell.meta.tick_s;
  const start = { type: 'start', format: FORMAT, scene: { scene_id: sc.scene_id ?? null, xml: sc.xml, meta: sc.meta },
                  checkpoint: sc.checkpoint, state_hash: sc.state_hash, target: sc.target,
                  tick: 0, t: 0, ...physState(cell), obs: observation(cell, { rgb: job.rgb !== false }), limits: lim };
  res.setup_s = round((performance.now() - t0) / 1000, 3);
  const ex = new Expert(job.expert.argv, job.expert.env, job.expert.cwd, job.io, job.expert.stderr_file);
  let timedOut = false;
  const wallMs = Math.max(0, 1000 * wallS - (performance.now() - t0));
  let timer;
  const wall = new Promise((r) => { timer = setTimeout(() => { timedOut = true; r(null); }, wallMs); });
  const wait = [], sim = [];
  let verdict = null;
  const ticksSoFar = () => (ep.cur ? ep.cur.ticks : ep.lastTry ? ep.lastTry.ticks : 0);
  const ctrlSoFar = () => {
    if (ep.lastTry) { res.ctrl = ep.lastTry.seg.ctrl; res.n = ep.lastTry.ticks; }
    else if (ep.cur) { res.n = ep.cur.ticks; res.ctrl = Buffer.from(Float64Array.from(ep.cur.ctrl).buffer).toString('base64'); }
    res.tick = ticksSoFar();
    res.t = round(res.tick * tickS);
  };
  try {
    ep.beginTry(sc.target);
    ex.send(start);
    let sent = performance.now();
    for (;;) {
      const line = await Promise.race([ex.next(), wall]);
      const got = performance.now();
      if (timedOut) {
        ctrlSoFar();
        await ex.kill(job.expert.kill);
        if (neverStarted()) return done('start_failed', `the container did not start within ${wallS} s${tail(ex)}`);
        return done('wall_timeout', `no verdict within the wall budget of ${wallS} s`);
      }
      if (line === null) {
        ctrlSoFar();
        await ex.kill(job.expert.kill);
        if (ex.broken) return done('expert_error', ex.broken);
        if (ex.spawnError) return done(job.expert.docker ? 'docker_unavailable' : 'start_failed', `could not start the expert: ${ex.spawnError}`);
        if (neverStarted()) return done('start_failed', `the container was not created (docker exited ${ex.exit})${tail(ex)}`);
        return done('expert_error', `the expert exited (${ex.exit})${tail(ex)}`);
      }
      wait.push(got - sent);
      res.exchanges++;
      const act = parseAct(line, lim.max_rows - ep.cur.ticks);
      const s0 = performance.now();
      if (act.rows.length === 0) {
        let row = ep.cur.ctrl.length ? ep.cur.ctrl.slice(-CTRL) : null;
        if (!row) { row = cell.ctrl().slice(); row[6] = 0; }
        while (!verdict) verdict = ep.stepTry(row);
      } else {
        for (const row of act.rows) { verdict = ep.stepTry(row); if (verdict) break; }
      }
      if (verdict) { sim.push(performance.now() - s0); break; }
      const msg = { type: 'state', tick: ep.cur.ticks, t: round(ep.cur.ticks * tickS), ...physState(cell) };
      if (act.obs) msg.obs = observation(cell, { rgb: job.rgb !== false });
      sim.push(performance.now() - s0);
      sent = performance.now();
      if (!ex.send(msg)) continue;
    }
    clearTimeout(timer);
    ctrlSoFar();
    const tick = ep.lastTry.verdict.tick;
    ex.send({ type: 'end', outcome: verdict.outcome, t: round(tick * tickS), tick });
    res.wall_s = round((performance.now() - t0) / 1000, 3);
    res.detail = verdict.detail;
    res.exchange = stats(wait);
    res.sim_ms = round(sim.reduce((a, b) => a + b, 0), 3);
    await ex.finish(graceS, job.expert.kill);
    const wallS2 = res.wall_s;
    done(verdict.outcome);
    res.wall_s = wallS2;
    return res;
  } catch (e) {
    clearTimeout(timer);
    ctrlSoFar();
    res.exchange = stats(wait);
    await ex.kill(job.expert.kill);
    if (e instanceof ProtocolError) return done('expert_error', e.message);
    return done('harness_error', String(e.stack || e.message || e).slice(0, 500));
  } finally {
    clearTimeout(timer);
    if (ep && ep.cur) ep.abortTry();
  }
}

export function ctrlRows(b64) {
  const f = b64ToF64(b64), out = [];
  for (let k = 0; k < f.length; k += CTRL) out.push(Array.from(f.subarray(k, k + CTRL)));
  return out;
}
