#!/usr/bin/env node
import { Cells, runJob, lines, bundleDir } from '../src/index.js';

const cells = await new Cells(bundleDir()).init();

await lines(async (job) => ({ id: job.id ?? null, ...(await runJob(cells, job)) }));
