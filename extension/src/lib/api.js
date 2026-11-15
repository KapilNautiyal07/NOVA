// Thin client for the NOVA backend. Works from the service worker and extension pages.
import { getState } from './storage.js';

export class ApiError extends Error {
  constructor(status, message, { offline = false } = {}) {
    super(message);
    this.status = status;
    this.offline = offline;
  }
}

const PUBLIC = new Set(['/api/health', '/api/auth/register']);

export async function api(path, { method = 'GET', body, form, timeoutMs = 30000, state } = {}) {
  const { backendUrl, token } = state || (await getState());
  if (!token && !PUBLIC.has(path)) throw new ApiError(401, 'NOVA is not connected to an account yet. Open NOVA settings to connect.');
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  let payload;
  if (form) payload = form;
  else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(backendUrl + path, { method, headers, body: payload, signal: ctrl.signal });
  } catch (err) {
    throw new ApiError(0, `Can't reach the NOVA server at ${backendUrl}. Is it running?`, { offline: true });
  } finally {
    clearTimeout(timer);
  }
  if (res.status === 204) return null;
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* non-JSON */
  }
  if (!res.ok) {
    let msg = data?.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => `${(d.loc || []).slice(1).join('.')}: ${d.msg}`).join('; ');
    throw new ApiError(res.status, msg || `Request failed (${res.status})`);
  }
  return data;
}

export const qs = (params) => {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue;
    if (Array.isArray(v)) v.forEach((x) => u.append(k, x));
    else u.append(k, v);
  }
  const s = u.toString();
  return s ? `?${s}` : '';
};
