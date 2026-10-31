export const OPERATION_POLL_INITIAL_DELAY_MS = 1000;
export const OPERATION_POLL_MAX_DELAY_MS = 5000;

export interface PollableOperation {
  id: number;
  status: string;
  updated_at?: string | null;
  completed_at?: string | null;
  message?: string | null;
  error_message?: string | null;
  progress?: unknown;
}

export function getNextOperationPollDelay(currentDelay: number, changed: boolean) {
  return changed ? OPERATION_POLL_INITIAL_DELAY_MS : Math.min(currentDelay * 2, OPERATION_POLL_MAX_DELAY_MS);
}

export function getOperationPollSignature(operation: PollableOperation) {
  return JSON.stringify({
    id: operation.id,
    status: operation.status,
    updated_at: operation.updated_at,
    completed_at: operation.completed_at,
    message: operation.message,
    error_message: operation.error_message,
    progress: operation.progress,
  });
}

export function getOperationListPollSignature(operations: PollableOperation[]) {
  return operations
    .map(getOperationPollSignature)
    .sort()
    .join('|');
}
