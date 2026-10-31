export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(message: string, status: number, detail: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

export async function createApiError(response: Response) {
  const detail = await readErrorDetail(response);
  return new ApiError(buildApiErrorMessage(detail, response.statusText), response.status, detail);
}

export function buildApiErrorMessage(detail: unknown, fallback: string) {
  if (typeof detail === 'string') {
    return detail;
  }

  if (detail && typeof detail === 'object' && 'detail' in detail) {
    const nested = (detail as {detail: unknown}).detail;
    if (typeof nested === 'string') {
      return nested;
    }
    if (nested && typeof nested === 'object' && 'message' in nested && typeof nested.message === 'string') {
      return nested.message;
    }
  }

  return fallback;
}

async function readErrorDetail(response: Response) {
  try {
    const contentType = (response.headers.get('Content-Type') ?? '').toLowerCase();
    if (contentType.includes('application/json') || contentType.includes('+json')) {
      return await response.json();
    }
    const text = await response.text();
    return text || null;
  } catch {
    return null;
  }
}
