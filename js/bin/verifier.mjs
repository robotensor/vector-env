#!/usr/bin/env node
import { simPin } from '@vector/sim/node.js';
import { Cells, SceneError, verifyRows, probeScene, lines, bundleDir } from '../src/index.js';

const cells = await new Cells(bundleDir(), Number(process.env.VECTOR_ENV_VERIFY_CELLS) || 3).init();

await lines(async (m) => {
  const id = m.id ?? null;
  if (m.op === 'ping') return { id, ok: true };
  if (m.op === 'pin') return { id, ...simPin() };
  const sc = m.scene;
  try {
    if (m.op === 'prepare') { cells.get(sc); return { id, ok: true }; }
    if (m.op === 'probe') return { id, ok: true, ...probeScene(cells.get(sc), sc, { ticks: m.ticks ?? 40, every: m.every ?? 5 }) };
    if (m.op === 'verify') {
      return { id, ...verifyRows(cells.get(sc), sc, m.rows, { timeLimitS: m.time_limit_s ?? null, hold: !!m.hold,
                                                                 claim: m.claim ?? null, every: m.every ?? 1,
                                                                 withMarks: !!m.marks }) };
    }
  } catch (e) {
    if (e instanceof SceneError) return { id, ok: false, reason: 'scene_error', detail: String(e.message || e) };
    throw e;
  }
  throw new Error(`unknown op ${JSON.stringify(m.op)}`);
});
