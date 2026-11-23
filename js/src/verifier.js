import { replayRows, rowsHash, flatRows, probe as simProbe, CTRL } from '@vector/sim';
import { setLimit, restore, SceneError } from './env.js';

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
  const budget = Math.ceil(lim / cell.meta.tick_s - 1e-9);
  restore(cell, scene);
  let flat;
  try { flat = flatRows(rows); } catch (e) { return refuse('malformed', e.message); }
  const n = flat.length / CTRL;
  if (n > budget) return refuse('over_budget', `the log has ${n} rows, the budget is ${budget} ticks (${lim} s)`);
  const action_hash = rowsHash(flat);
  const r = replayRows(cell, { checkpoint: scene.checkpoint, target: scene.target }, flat, { every, hold });
  const out = { format: VERIFY_FORMAT, ok: true, reason: null, detail: null, outcome: r.outcome, success: r.outcome === 'success',
                tick: r.tick, t: r.t, rows: n, used: r.rows, extra: r.extra, held: r.held, budget,
                state_hash: r.hash, action_hash, divergence: null, checked: 0 };
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

function refuse(reason, detail) {
  return { format: VERIFY_FORMAT, ok: false, reason, detail, outcome: null, success: false, tick: 0, t: 0, rows: 0, used: 0,
           extra: 0, held: 0, budget: null, state_hash: null, action_hash: null, divergence: null, checked: 0 };
}

export function probeScene(cell, scene, { ticks = 40, every = 5 } = {}) {
  restore(cell, scene);
  return simProbe(cell, scene.checkpoint, { ticks, every });
}
