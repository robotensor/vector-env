import { CTRL, b64ToF64, replayPicks } from '@vector/sim';
import { SceneError, setLimit, restore, round, tryMetrics, multiPick, pickStart, sceneSummary, SCENE_DONE } from './env.js';

export const TRACE_FORMAT = 'vector-env-trace/1';

const r4 = (x) => round(x, 4);

export function matQuat(m) {
  const [a, b, c, d, e, f, g, h, i] = m;
  const tr = a + e + i;
  let w, x, y, z;
  if (tr > 0) {
    const s = 2 * Math.sqrt(tr + 1);
    w = s / 4; x = (h - f) / s; y = (c - g) / s; z = (d - b) / s;
  } else if (a > e && a > i) {
    const s = 2 * Math.sqrt(1 + a - e - i);
    w = (h - f) / s; x = s / 4; y = (b + d) / s; z = (c + g) / s;
  } else if (e > i) {
    const s = 2 * Math.sqrt(1 + e - a - i);
    w = (c - g) / s; x = (b + d) / s; y = s / 4; z = (f + h) / s;
  } else {
    const s = 2 * Math.sqrt(1 + i - a - e);
    w = (d - b) / s; x = (c + g) / s; y = (f + h) / s; z = s / 4;
  }
  if (w < 0) { w = -w; x = -x; y = -y; z = -z; }
  return [w, x, y, z];
}

function ranges(ticks) {
  const out = [];
  for (const k of ticks) {
    const last = out[out.length - 1];
    if (last && k === last[1] + 1) last[1] = k; else out.push([k, k]);
  }
  return out;
}

const span = (rs) => rs.map(([a, b]) => (a === b ? `${a}` : `${a}-${b}`)).join(', ');

function flatCtrl(ctrl, n) {
  const flat = typeof ctrl === 'string' ? b64ToF64(ctrl) : Float64Array.from(ctrl || []);
  if (flat.length % CTRL) throw new SceneError(`ctrl length ${flat.length} is not a multiple of ${CTRL}`);
  const rows = flat.length / CTRL;
  if (n !== undefined && n !== null && n !== rows) throw new SceneError(`ctrl holds ${rows} rows, not n = ${n}`);
  return { flat, rows };
}

// A multi-pick scene: one entry per tick (with its pick) and one reason line per pick.
function tracePicks(cell, ep, scene, flat, rows) {
  const tickS = cell.meta.tick_s, ticks = [];
  let k = 0;
  const look = (run, row, after) => {
    k++;
    const { held, touch } = cell.contacts(), tcp = cell.tcp(), p = cell.proprio();
    const H = [], X = [];
    for (let i = 0; i < cell.n; i++) { if (!cell.active[i]) continue; if (held[i]) H.push(i); if (touch[i]) X.push(i); }
    const e = { tick: k, t: r4(k * tickS), pick: run.ep.cur ? run.k : run.picks.length - 1, row: row.map(r4), q: p.q.map(r4),
                grip: r4(p.grip), tcp: tcp.pos.map(r4), tcp_quat: matQuat(tcp.mat).map(r4), held: H, touch: X };
    if (after) e.after_rows = true;
    ticks.push(e);
  };
  const R = replayPicks(ep, pickStart(cell, scene), flat, { hold: true, onTick: look });
  const S = sceneSummary(R.run, { partial: false }), last = R.run.picks[R.run.picks.length - 1];
  const outcome = R.run.done ? (last && last.outcome === 'physics_error' ? 'physics_error' : SCENE_DONE) : null;
  const why = [`${outcome ?? 'no end'} after ${R.run.picks.length} of ${S.picks_planned} picks: ${S.successes} picked, ${S.outs} outs`];
  for (const p of R.run.picks)
    why.push(`pick ${p.pick} (part ${p.target}, ${p.target_class}): ${p.outcome} at tick ${p.tick} (${r4(p.t)} s): ${p.detail}`);
  why.push(`the expert sent ${rows} rows` + (R.rows < rows ? `; the scene ended after ${R.rows} of them` : '')
           + (R.held ? `; the last one was held for ${R.held} more ticks` : ''));
  return { format: TRACE_FORMAT, scene_id: scene.scene_id ?? null, target: scene.target, time_limit_s: cell.cfg.rules.try_s,
           tick_s: tickS, outcome, success: S.successes === S.picks_planned, ...S, hash: cell.hash(),
           detail: last ? last.detail : null, rows: R.rows, sent: rows, held: R.held, reasons: why, ticks };
}

export function traceCtrl(cell, scene, ctrl, n, timeLimitS) {
  setLimit(cell, timeLimitS);
  const ep = restore(cell, scene);
  const { flat, rows } = flatCtrl(ctrl, n);
  if (multiPick(cell)) return tracePicks(cell, ep, scene, flat, rows);
  const T = scene.target;
  ep.beginTry(T, scene.target_class ?? null);
  const o = ep.cur.mon.o;
  const tickS = cell.meta.tick_s;
  const tb = cell.meta.parts[T].body;
  const ticks = [];
  const S = { touch: null, held: [], other: new Map(), ask: null, shut: null, empty: null, maxAbove: -Infinity, maxAboveTick: 0,
              above: [], near: { mm: Infinity, tick: 0 }, left: new Map() };
  const look = (k, row, after) => {
    const { held, touch, padAny } = cell.contacts();
    const tcp = cell.tcp(), p = cell.proprio(), shut = cell.gripClosed();
    const tp = Array.from(cell.d.xpos.subarray(3 * tb, 3 * tb + 3));
    const dist = 1000 * Math.hypot(tcp.pos[0] - tp[0], tcp.pos[1] - tp[1], tcp.pos[2] - tp[2]);
    const aboveMm = 1000 * (cell.bottomZ(T) - o.liftZ);
    const inside = cell.inside(T);
    const H = [], X = [];
    for (let i = 0; i < cell.n; i++) {
      if (!cell.active[i]) continue;
      if (held[i]) H.push(i);
      if (touch[i]) X.push(i);
      if (k > 0 && !cell.inside(i) && !held[i] && !S.left.has(i)) S.left.set(i, k);
    }
    if (k > 0) {
      if (touch[T] && S.touch === null) S.touch = k;
      if (held[T]) S.held.push(k);
      for (const i of H) if (i !== T) {
        const e = S.other.get(i) || { first: k, ticks: 0 };
        e.ticks++;
        S.other.set(i, e);
      }
      if (row && row[6] >= 0.5 && S.ask === null) S.ask = k;
      if (shut && S.shut === null) S.shut = k;
      if (shut && !H.length && !padAny && S.empty === null) S.empty = k;
      if (aboveMm > S.maxAbove) { S.maxAbove = aboveMm; S.maxAboveTick = k; }
      if (aboveMm >= 0 && held[T]) S.above.push(k);
      if (dist < S.near.mm) S.near = { mm: dist, tick: k };
    }
    const e = { tick: k, t: r4(k * tickS), row: row ? row.map(r4) : null, q: p.q.map(r4), grip: r4(p.grip), shut,
                tcp: tcp.pos.map(r4), tcp_quat: matQuat(tcp.mat).map(r4), held: H, touch: X,
                target: { pos: tp.map(r4), above_line_mm: round(aboveMm, 1), inside, dist_mm: round(dist, 1) } };
    if (after) e.after_rows = true;
    ticks.push(e);
  };
  look(0, null, false);
  let v = null, k = 0, row = null;
  for (; k < rows && !v; k++) {
    row = Array.from(flat.subarray(CTRL * k, CTRL * (k + 1)));
    if (!row.every(Number.isFinite)) throw new SceneError('ctrl has a non-finite value');
    v = ep.stepTry(row);
    look(k + 1, row, false);
  }
  let held = 0;
  if (!v) {
    if (!row) { row = cell.ctrl().slice(); row[6] = 0; }
    while (!v) { v = ep.stepTry(row); held++; look(k + held, row, true); }
  }
  const verdict = ep.lastTry.verdict;
  const tick = verdict.tick;
  const summary = {
    target_first_touch: S.touch,
    target_held_ticks: S.held.length,
    target_held: ranges(S.held),
    other_held: Object.fromEntries([...S.other].map(([i, e]) => [String(i), e])),
    grip_first_asked: S.ask,
    grip_first_shut: S.shut,
    shut_on_nothing: S.empty,
    target_max_above_line_mm: Number.isFinite(S.maxAbove) ? round(S.maxAbove, 1) : null,
    target_max_above_tick: S.maxAboveTick,
    target_held_above_line_ticks: S.above.length,
    nearest_mm: Number.isFinite(S.near.mm) ? round(S.near.mm, 1) : null,
    nearest_tick: S.near.tick,
    left_bin: Object.fromEntries([...S.left].map(([i, t]) => [String(i), t])),
  };
  const why = [`${verdict.outcome} at tick ${tick} (${r4(tick * tickS)} s): ${verdict.detail}`];
  const kept = rows ? `the last one was held for ${held} more ticks` : `the start pose with the grip open was held for ${held} ticks`;
  why.push(`the expert sent ${rows} rows` + (k < rows ? `; the verdict came after ${k} of them` : '') + (held ? `; ${kept} until the verdict` : ''));
  if (S.touch === null) why.push(`the gripper never touched the target (part ${T}); the tcp came closest to its center at tick ${S.near.tick}: ${round(S.near.mm, 1)} mm`);
  else why.push(`the gripper first touched the target (part ${T}) at tick ${S.touch}`);
  if (S.ask === null) why.push('the rows never closed the grip (grip stayed below 0.5)');
  else why.push(`the rows close the grip from tick ${S.ask}` + (S.empty !== null ? `; at tick ${S.empty} the fingers shut on nothing` : ''));
  if (!S.held.length) {
    const others = [...S.other].map(([i, e]) => `part ${i} for ${e.ticks} ticks from tick ${e.first}`);
    why.push('the target was never held between both pads' + (others.length ? `; held instead: ${others.join(', ')}` : ''));
  } else {
    const last = S.held[S.held.length - 1];
    why.push(`the target was held for ${S.held.length} ticks (ticks ${span(ranges(S.held))})`
             + (last < tick ? `; it was not held from tick ${last + 1}` : ''));
    const mm = round(S.maxAbove, 1);
    if (mm < 0) why.push(`its lowest point got to ${-mm} mm below the lift line (the bin rim + ${r4(o.liftZ - cell.crate.rim)} m) at tick ${S.maxAboveTick}`);
    else why.push(`it was held above the lift line for ${S.above.length} ticks (${r4(S.above.length * tickS)} s; a success needs ${o.holdS} s in a row with no other part above it)`);
  }
  const gone = [...S.left].map(([i, t]) => `part ${i}${i === T ? ' (the target)' : ''} at tick ${t}`);
  if (gone.length) why.push(`left the bin without being held: ${gone.join(', ')}`);
  const M = tryMetrics(ep.lastTry.mon);
  if (M.outs) why.push(`outs (parts that ended outside the bin): ${M.outs}, parts ${M.out_parts.join(', ')}`);
  return { format: TRACE_FORMAT, scene_id: scene.scene_id ?? null, target: T, time_limit_s: cell.cfg.rules.try_s,
           tick_s: tickS, lift_line_z: r4(o.liftZ), hold_s: o.holdS,
           outcome: verdict.outcome, success: verdict.outcome === 'success', t: r4(tick * tickS), tick, hash: cell.hash(),
           detail: verdict.detail, part: verdict.part, rows: k, sent: rows, held, ...M, reasons: why, summary, ticks };
}
