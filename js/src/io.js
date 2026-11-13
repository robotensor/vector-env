import readline from 'node:readline';
import { findBundle, MISSING } from '@vector/sim/bundle.js';

export function bundleDir() {
  const b = process.env.VECTOR_ENV_BUNDLE || process.env.VECTOR_SIM_BUNDLE || findBundle();
  if (!b) throw new Error(MISSING);
  return b;
}

export async function lines(handle) {
  const out = (o) => process.stdout.write(JSON.stringify(o) + '\n');
  for await (const line of readline.createInterface({ input: process.stdin, crlfDelay: Infinity })) {
    if (!line.trim()) continue;
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      out({ error: 'not JSON' });
      continue;
    }
    try {
      out(await handle(msg));
    } catch (e) {
      out({ id: msg.id ?? null, error: String(e.message || e), kind: e.name });
    }
  }
}
