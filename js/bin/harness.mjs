#!/usr/bin/env node
import { Cells, Session, runJob, lines, bundleDir } from '../src/index.js';

const cells = await new Cells(bundleDir()).init();
let session = null;

function remote(m) {
  if (m.op === 'open') {
    if (session) session.close();
    session = new Session(cells, m);
    const out = session.open();
    if (out.result) session = null;
    return out;
  }
  if (!session) throw new Error('no open session');
  if (m.op === 'act') {
    const out = session.act(m.act, Number(m.wait_ms));
    if (out.result) session = null;
    return out;
  }
  if (m.op === 'stop') {
    const result = session.stop(m.outcome, m.error ?? null);
    session = null;
    return { result };
  }
  throw new Error(`unknown op ${JSON.stringify(m.op)}`);
}

await lines(async (job) => ({ id: job.id ?? null, ...(job.op ? remote(job) : await runJob(cells, job)) }));
