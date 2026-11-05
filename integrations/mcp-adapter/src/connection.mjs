/** One configured endpoint, explicit TLS trust, bounded requests, no redirects/OAuth. */
import { readFileSync } from 'node:fs';
import { AsyncLocalStorage } from 'node:async_hooks';
import { Agent, fetch, Response } from 'undici';

export const requestContext = new AsyncLocalStorage();
export const MAX_REQUEST = 32768;
export const MAX_RESPONSE = 131072;
export const DEADLINE_MS = 10000;

export class ConnectionError extends Error {
  constructor(code) { super(code); this.code = code; }
}

export function configuration(env) {
  let endpoint;
  try { endpoint = new URL(env.BAMDUDE_MCP_URL); }
  catch { throw new ConnectionError('invalid_endpoint'); }
  const loopback = endpoint.hostname === 'localhost' || endpoint.hostname === '[::1]'
    || /^127(?:\.\d{1,3}){3}$/.test(endpoint.hostname);
  // Exact opt-in only: unset/false/unexpanded host placeholders never permit HTTP.
  const allowHttp = env.BAMDUDE_MCP_ALLOW_HTTP === 'true';
  if ((endpoint.protocol !== 'https:' && !(endpoint.protocol === 'http:' && (loopback || allowHttp)))
    || endpoint.pathname !== '/mcp' || endpoint.username || endpoint.password
    || endpoint.search || endpoint.hash) throw new ConnectionError('invalid_endpoint');
  const key = env.BAMDUDE_MCP_KEY;
  if (!key || key.length > 4096 || /[\r\n]/.test(key)) throw new ConnectionError('invalid_key');
  // The connector has no TLS bypass, including inherited Node-wide settings.
  if (env.NODE_TLS_REJECT_UNAUTHORIZED === '0') throw new ConnectionError('tls_bypass_forbidden');
  let ca;
  // Some hosts leave an unfilled optional user-config field as its literal placeholder.
  // System trust still verifies TLS; only this exact optional placeholder means no custom CA.
  const caFile = env.BAMDUDE_MCP_CA_FILE;
  if (caFile && caFile !== '${user_config.ca_file}') {
    try {
      const bytes = readFileSync(caFile);
      if (!bytes.length || bytes.length > 1048576) throw new Error();
      ca = bytes.toString('utf8');
    } catch { throw new ConnectionError('invalid_ca_file'); }
  }
  return { endpoint, key, ca };
}

export function connection(config) {
  const lifetime = new AbortController();
  const dispatcher = new Agent({
    connections: 2, pipelining: 1,
    connect: { rejectUnauthorized: true, ...(config.ca ? { ca: config.ca } : {}) },
    headersTimeout: DEADLINE_MS, bodyTimeout: DEADLINE_MS,
  });
  return {
    async fetch(input, init = {}) {
      if (new URL(input).href !== config.endpoint.href) throw new ConnectionError('endpoint_changed');
      if (init.method !== 'POST' && init.method !== 'GET') throw new ConnectionError('method_not_allowed');
      if (init.body && (typeof init.body !== 'string' || Buffer.byteLength(init.body) > MAX_REQUEST)) {
        throw new ConnectionError('request_too_large');
      }
      const headers = new Headers(init.headers);
      headers.delete('cookie');
      headers.delete('authorization');
      headers.set('X-API-Key', config.key);
      const signals = [lifetime.signal, AbortSignal.timeout(DEADLINE_MS), init.signal, requestContext.getStore()?.signal]
        .filter(Boolean);
      let response;
      try {
        response = await fetch(config.endpoint, {
          ...init, headers, dispatcher, redirect: 'manual', signal: AbortSignal.any(signals),
        });
        if (response.status >= 300 && response.status < 400) throw new ConnectionError('redirect_forbidden');
        if (response.status === 401) throw new ConnectionError('unauthorized');
        if (response.status === 403) throw new ConnectionError('forbidden');
        if (response.status === 404) throw new ConnectionError('mcp_disabled_or_wrong_address');
        if (response.status === 429) throw new ConnectionError('rate_limited');
        if (response.status >= 500) throw new ConnectionError('server_unavailable');
        if (response.headers.has('mcp-session-id')) throw new ConnectionError('stateful_server_not_supported');
        const parts = [];
        let size = 0;
        for await (const part of response.body ?? []) {
          size += part.length;
          if (size > MAX_RESPONSE) throw new ConnectionError('response_too_large');
          parts.push(part);
        }
        return new Response([204, 205, 304].includes(response.status) ? null : Buffer.concat(parts), {
          status: response.status, headers: response.headers,
        });
      } catch (error) {
        await response?.body?.cancel().catch(() => {});
        if (error instanceof ConnectionError) throw error;
        throw new ConnectionError(signals.some(signal => signal.aborted) ? 'cancelled_or_timed_out' : 'network_or_tls_error');
      }
    },
    async close() {
      lifetime.abort();
      await dispatcher.destroy();
    },
  };
}
