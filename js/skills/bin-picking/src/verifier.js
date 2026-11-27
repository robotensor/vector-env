import { replayRows, replayPicks, rowsHash, flatRows, probe as simProbe, CTRL, pickCount } from '@vector/sim';
import { setLimit, restore, SceneError, outOfRange, multiPick, pickStart, sceneSummary, SCENE_DONE } from './env.js';

export const VERIFY_FORMAT = 'vector-verify/1';

const markMap = (marks) => {
  const m = new Map();
  for (const x of marks || []) {
    if (!Array.isArray(x) || x.length !== 2 || !Number.isInteger(x[0]) || typeof x[1] !== 'string') throw new Error('marks are [tick, hash] pairs');
    m.set(x[0], x[1]);
  }
  return m;
};

export function verifyRows(cell, scene, rows, { timeLimitS = null, hold = false, claim = null, every = 1, withMarks = false } = {}) {
  const lim = setLimit(cell, timeLimitS);
  const budget = Math.ceil(lim / cell.meta.tick_s - 1e-9) * pickCount(cell);
  const ep0 = restore(cell, scene);
  let flat;
  try { flat = flatRows(rows); } catch (e) { return refuse('malformed', e.message); }
  const n = flat.length / CTRL;
  if (n > budget) return refuse('over_budget', `the log has ${n} rows, the budget is ${budget} ticks (${lim} s)`);
  if (claim && claim.in_range) {
    const x = outOfRange(cell, flat);
    if (x) return refuse('out_of_range', `row ${x.tick} column ${x.col + 1} is outside the actuator range`);
  }
  const action_hash = rowsHash(flat);
  const r = multiPick(cell) ? scenePicks(ep0, cell, scene, flat, { every, hold })
    : replayRows(cell, { checkpoint: scene.checkpoint, target: scene.target, target_class: scene.target_class ?? null }, flat,
                 { every, hold });
  const out = { format: VERIFY_FORMAT, ok: true, reason: null, detail: null, outcome: r.outcome, success: r.outcome === 'success',
                tick: r.tick, t: r.t, rows: n, used: r.rows, extra: r.extra, held: r.held, budget,
                state_hash: r.hash, action_hash, divergence: null, checked: 0, outs: r.outs, out_parts: r.out_parts,
                tcp_path_m: r.tcp_path_m, joint_path_rad: r.joint_path_rad, crate_hits: r.crate_hits, target_class: r.target_class };
  if (r.picks) Object.assign(out, { picks: r.picks, picks_planned: r.picks_planned, successes: r.successes, out_parts: r.out_parts,
                                    success: r.successes === r.picks_planned });
  if (withMarks) out.marks = r.marks;
  const bad = (reason, detail) => Object.assign(out, { ok: false, reason, detail });
  if (!hold && r.outcome === null) bad('no_verdict', `the rules gave no verdict within the ${n} rows of the log`);
  if (!hold && r.extra > 0) bad('rows_after_verdict', `the verdict came at tick ${r.tick}, the log goes on to ${n}`);
  if (!claim) return out;
  let want;
  try { want = markMap(claim.marks); } catch (e) { return bad('malformed', e.message); }
  for (const [tick, hash] of r.marks) {
    if (!want.has(tick)) continue;
    out.checked++;
    if (want.get(tick) !== hash) {
      out.divergence = { tick, want: want.get(tick), got: hash };
      return bad('state_mismatch', `the state differs from tick ${tick} on`);
    }
  }
  if (claim.action_hash && claim.action_hash !== action_hash) return bad('action_mismatch', 'the rows are not the ones hashed');
  if (claim.outcome !== undefined && claim.outcome !== r.outcome) return bad('outcome_mismatch', `the replay gives ${r.outcome}, the log claims ${claim.outcome}`);
  if (claim.tick !== undefined && claim.tick !== r.tick) return bad('tick_mismatch', `the replay ends at tick ${r.tick}, the log claims ${claim.tick}`);
  if (claim.state_hash && claim.state_hash !== r.hash) return bad('state_mismatch', 'the final state differs');
  return out;
}

function scenePicks(ep, cell, scene, flat, { every, hold }) {
  const R = replayPicks(ep, pickStart(cell, scene), flat, { hold, every });
  const S = sceneSummary(R.run, { partial: false }), last = R.run.picks[R.run.picks.length - 1];
  const outcome = R.run.done ? (last && last.outcome === 'physics_error' ? 'physics_error' : SCENE_DONE) : null;
  return { outcome, part: null, detail: last ? last.detail : null, tick: S.tick, t: S.t, rows: R.rows, extra: R.extra,
           held: R.held, hash: R.hash, marks: R.marks, ...S, target_class: scene.target_class ?? null };
}

function refuse(reason, detail) {
  return { format: VERIFY_FORMAT, ok: false, reason, detail, outcome: null, success: false, tick: 0, t: 0, rows: 0, used: 0,
           extra: 0, held: 0, budget: null, state_hash: null, action_hash: null, divergence: null, checked: 0 };
}

export function probeScene(cell, scene, { ticks = 40, every = 5 } = {}) {
  restore(cell, scene);
  return simProbe(cell, scene.checkpoint, { ticks, every });
}
