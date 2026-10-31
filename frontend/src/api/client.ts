import {createApiError} from '@/api/errors';

export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000/api').replace(/\/$/, '');

export function buildApiUrl(path: string) {
  if (/^https?:\/\//i.test(path)) {
    return path;
  }
  return `${API_BASE_URL}${path.startsWith('/') ? path : `/${path}`}`;
}

export async function requestApi(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(buildApiUrl(path), {
    ...init,
    headers: mergeHeaders({Accept: 'application/json'}, init?.headers),
  });

  if (!response.ok) {
    throw await createApiError(response);
  }

  return response;
}

export async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await requestApi(path, init);
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

function mergeHeaders(defaults: HeadersInit, overrides?: HeadersInit) {
  const headers = new Headers(defaults);
  if (overrides) {
    new Headers(overrides).forEach((value, key) => headers.set(key, value));
  }
  return headers;
}
