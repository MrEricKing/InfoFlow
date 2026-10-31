import {API_BASE_URL, buildApiUrl} from '@/api/client';

export {API_BASE_URL, buildApiUrl};

export function buildApiContentUrl(pathOrUrl: string | null) {
  if (!pathOrUrl) {
    return null;
  }
  if (/^https?:\/\//i.test(pathOrUrl)) {
    return pathOrUrl;
  }
  if (pathOrUrl.startsWith('/api/')) {
    return `${API_BASE_URL}${pathOrUrl.slice(4)}`;
  }
  return buildApiUrl(pathOrUrl.startsWith('/') ? pathOrUrl : `/${pathOrUrl}`);
}

export function buildKnowledgeAssetContentUrl(assetKey: string) {
  return buildApiContentUrl(`/api/knowledge/assets/${encodeURIComponent(assetKey)}/content`) ?? '';
}
