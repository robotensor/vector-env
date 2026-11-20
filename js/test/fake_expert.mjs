import readline from 'node:readline';

const mode = process.argv[2] || 'hold';
if (mode === 'deaf') {
  for (let k = 0; k < 200; k++) {
    process.stdout.write(JSON.stringify({ type: 'act', rows: [[0, -1.5, 1.5, -1.5, -1.5, 0, 0]], obs: true }) + '\n');
    await new Promise((r) => setTimeout(r, 20));
  }
}
let q = null;
for await (const line of readline.createInterface({ input: process.stdin })) {
  const m = JSON.parse(line);
  if (m.type === 'end') break;
  if (m.type === 'start') q = [...m.scene.meta.robot.arm_joints_qpos.map((i) => m.qpos[i]), 0];
  if (mode === 'late' && m.type === 'start') await new Promise((r) => setTimeout(r, 60000));
  if (mode === 'long' && m.type === 'start') { process.stdout.write('z'.repeat(300000) + '\n'); continue; }
  if (mode === 'two' && m.type === 'start') { process.stdout.write(JSON.stringify({ type: 'act', rows: [q, q] }) + '\n'); continue; }
  process.stdout.write(JSON.stringify({ type: 'act', rows: mode === 'many' ? Array(500).fill(q) : [q] }) + '\n');
}
