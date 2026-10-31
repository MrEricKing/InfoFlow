import assert from 'node:assert/strict';

import {
  getNextOperationPollDelay,
  getOperationListPollSignature,
  getOperationPollSignature,
  OPERATION_POLL_INITIAL_DELAY_MS,
  OPERATION_POLL_MAX_DELAY_MS,
} from '../frontend/src/lib/operation-polling.ts';

assert.equal(getNextOperationPollDelay(1000, true), OPERATION_POLL_INITIAL_DELAY_MS);
assert.equal(getNextOperationPollDelay(1000, false), 2000);
assert.equal(getNextOperationPollDelay(2000, false), 4000);
assert.equal(getNextOperationPollDelay(4000, false), OPERATION_POLL_MAX_DELAY_MS);
assert.equal(getNextOperationPollDelay(OPERATION_POLL_MAX_DELAY_MS, false), OPERATION_POLL_MAX_DELAY_MS);

const queued = {id: 1, status: 'queued', updated_at: '2026-05-16T00:00:00Z', progress: {completed: 0}};
const running = {id: 1, status: 'running', updated_at: '2026-05-16T00:00:01Z', progress: {completed: 1}};

assert.notEqual(getOperationPollSignature(queued), getOperationPollSignature(running));
assert.equal(
  getOperationListPollSignature([running, queued]),
  getOperationListPollSignature([queued, running]),
);
