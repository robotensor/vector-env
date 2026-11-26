#!/usr/bin/env node
import { Cells, settle, restore, replayCtrl, traceCtrl, lines, bundleDir } from '../src/index.js';

const cells = await new Cells(bundleDir()).init();

await lines(async (m) => {
  const sc = m.scene;
  if (m.op === 'settle') {
    const cell = cells.get(sc);
    return { id: m.id ?? null, ...settle(cell, m.seed ?? sc.meta?.seed ?? 0) };
  }
  if (m.op === 'verify') {
    const cell = cells.get(sc);
    try {
      restore(cell, sc);
      return { id: m.id ?? null, ok: true, state_hash: cell.hash() };
    } catch (e) {
      return { id: m.id ?? null, ok: false, reason: String(e.message || e) };
    }
  }
  if (m.op === 'replay') {
    const cell = cells.get(sc);
    return { id: m.id ?? null, ...replayCtrl(cell, sc, m.ctrl, m.n, m.time_limit_s) };
  }
  if (m.op === 'trace') {
    const cell = cells.get(sc);
    return { id: m.id ?? null, ...traceCtrl(cell, sc, m.ctrl, m.n, m.time_limit_s) };
  }
  if (m.op === 'ping') return { id: m.id ?? null, ok: true };
  throw new Error(`unknown op ${JSON.stringify(m.op)}`);
});
