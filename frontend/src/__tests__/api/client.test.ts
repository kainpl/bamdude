/**
 * Tests for the API client auth token handling.
 */

import { describe, it, expect, afterEach, vi } from 'vitest';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { setAuthToken, getAuthToken, api } from '../../api/client';

// Mock localStorage
const localStorageMock = {
  store: {} as Record<string, string>,
  getItem: vi.fn((key: string) => localStorageMock.store[key] || null),
  setItem: vi.fn((key: string, value: string) => {
    localStorageMock.store[key] = value;
  }),
  removeItem: vi.fn((key: string) => {
    delete localStorageMock.store[key];
  }),
  clear: vi.fn(() => {
    localStorageMock.store = {};
  }),
};

Object.defineProperty(window, 'localStorage', {
  value: localStorageMock,
});

// Create MSW server
const server = setupServer();

beforeAll(() => server.listen({ onUnhandledRequest: 'bypass' }));
afterEach(() => {
  server.resetHandlers();
  localStorageMock.clear();
  setAuthToken(null);
});
afterAll(() => server.close());

describe('Auth Token Management', () => {
  it('setAuthToken stores token in localStorage', () => {
    setAuthToken('test-token-123');
    expect(localStorageMock.setItem).toHaveBeenCalledWith('auth_token', 'test-token-123');
    expect(getAuthToken()).toBe('test-token-123');
  });

  it('setAuthToken removes token from localStorage when null', () => {
    setAuthToken('test-token-123');
    setAuthToken(null);
    expect(localStorageMock.removeItem).toHaveBeenCalledWith('auth_token');
    expect(getAuthToken()).toBeNull();
  });
});

describe('API Client Auth Header', () => {
  it('includes Authorization header when token is set', async () => {
    let capturedHeaders: Headers | null = null;

    server.use(
      http.get('/api/v1/settings/spoolman', ({ request }) => {
        capturedHeaders = request.headers;
        return HttpResponse.json({
          spoolman_enabled: 'false',
          spoolman_url: '',
          spoolman_sync_mode: 'auto',
        });
      })
    );

    setAuthToken('test-jwt-token');
    await api.getSpoolmanSettings();

    expect(capturedHeaders).not.toBeNull();
    expect(capturedHeaders!.get('Authorization')).toBe('Bearer test-jwt-token');
  });

  it('does not include Authorization header when token is not set', async () => {
    let capturedHeaders: Headers | null = null;

    server.use(
      http.get('/api/v1/settings/spoolman', ({ request }) => {
        capturedHeaders = request.headers;
        return HttpResponse.json({
          spoolman_enabled: 'false',
          spoolman_url: '',
          spoolman_sync_mode: 'auto',
        });
      })
    );

    setAuthToken(null);
    await api.getSpoolmanSettings();

    expect(capturedHeaders).not.toBeNull();
    expect(capturedHeaders!.get('Authorization')).toBeNull();
  });

  it('clears token on 401 with invalid token message', async () => {
    server.use(
      http.get('/api/v1/settings/spoolman', () => {
        return HttpResponse.json(
          { detail: 'Could not validate credentials' },
          { status: 401 }
        );
      })
    );

    setAuthToken('expired-token');
    expect(getAuthToken()).toBe('expired-token');

    try {
      await api.getSpoolmanSettings();
    } catch {
      // Expected to throw
    }

    expect(getAuthToken()).toBeNull();
    expect(localStorageMock.removeItem).toHaveBeenCalledWith('auth_token');
  });

  it('does not clear token on 401 with generic auth error', async () => {
    server.use(
      http.get('/api/v1/settings/spoolman', () => {
        return HttpResponse.json(
          { detail: 'Authentication required' },
          { status: 401 }
        );
      })
    );

    setAuthToken('valid-token');
    expect(getAuthToken()).toBe('valid-token');

    try {
      await api.getSpoolmanSettings();
    } catch {
      // Expected to throw
    }

    // Token should NOT be cleared for generic auth errors (might be timing issue)
    expect(getAuthToken()).toBe('valid-token');
  });
});

describe('FormData requests include auth header', () => {
  it('uploadProjectAttachment includes Authorization header', async () => {
    // Mock fetch directly for FormData requests (MSW can be flaky with
    // multipart in some environments).
    const originalFetch = global.fetch;
    let capturedHeaders: Headers | null = null;

    global.fetch = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      if (url.includes('/projects/1/attachments')) {
        capturedHeaders = new Headers(init?.headers);
        return Promise.resolve(
          new Response(
            JSON.stringify({ status: 'ok', filename: 'a.pdf', original_name: 'a.pdf', attachments: [] }),
            { status: 200 },
          ),
        );
      }
      return originalFetch(url, init);
    });

    try {
      setAuthToken('test-token');
      const file = new File(['test content'], 'a.pdf', { type: 'application/pdf' });
      await api.uploadProjectAttachment(1, file);

      expect(capturedHeaders).not.toBeNull();
      expect(capturedHeaders!.get('Authorization')).toBe('Bearer test-token');
    } finally {
      global.fetch = originalFetch;
    }
  });

  it('a blob download includes the Authorization header', async () => {
    // The blob paths build their own `fetch` rather than going through
    // `request()`, so the header is attached by hand in each one — which is
    // exactly the thing that can be forgotten. jsdom has no object-URL
    // implementation, so the download's anchor plumbing is stubbed.
    let capturedHeaders: Headers | null = null;
    const createObjectURL = vi.fn(() => 'blob:stub');
    const revokeObjectURL = vi.fn();
    const originalCreate = window.URL.createObjectURL;
    const originalRevoke = window.URL.revokeObjectURL;
    window.URL.createObjectURL = createObjectURL;
    window.URL.revokeObjectURL = revokeObjectURL;
    // jsdom answers a real anchor click with "Not implemented: navigation".
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});

    server.use(
      http.get('/api/v1/library/files/:fileId/download', ({ request }) => {
        capturedHeaders = request.headers;
        return new HttpResponse(new Uint8Array([0x50, 0x4b, 0x03, 0x04]), {
          status: 200,
          headers: {
            'Content-Type': 'application/zip',
            'Content-Disposition': 'attachment; filename="file.3mf"',
          },
        });
      }),
    );

    try {
      setAuthToken('test-token');
      await api.downloadLibraryFile(1);

      expect(capturedHeaders).not.toBeNull();
      expect(capturedHeaders!.get('Authorization')).toBe('Bearer test-token');
    } finally {
      window.URL.createObjectURL = originalCreate;
      window.URL.revokeObjectURL = originalRevoke;
      click.mockRestore();
    }
  });
});

/**
 * ⚠️ A multipart upload recovers from an expired access token exactly like a
 * JSON call does.
 *
 * `sendForm()` cannot go through `request()` — `request()` sets
 * `Content-Type: application/json` on every call, which would replace the
 * boundary header the browser writes for a `FormData` body and turn a perfectly
 * good file into a 422. What it CAN share is everything else, and for one
 * release it shared none of it: an upload started on a tab idle past the access
 * token's hour died with a bare "Not authenticated" and lost the file the
 * operator had picked, while every JSON call beside it refreshed and retried.
 *
 * The `FormData` survives the retry because `fetch` serialises it per call — it
 * is not a consumed stream.
 */
describe('sendForm recovers from an expired access token', () => {
  it('refreshes once and re-sends the multipart body', async () => {
    const originalFetch = global.fetch;
    const seen: string[] = [];
    let refreshes = 0;

    global.fetch = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const href = String(url);
      if (href.includes('/auth/refresh')) {
        refreshes += 1;
        return Promise.resolve(
          new Response(JSON.stringify({ access_token: 'fresh-token' }), { status: 200 }),
        );
      }
      if (href.includes('/products/import')) {
        seen.push(new Headers(init?.headers).get('Authorization') ?? '');
        if (seen.length === 1) {
          return Promise.resolve(
            new Response(JSON.stringify({ detail: 'Could not validate credentials' }), { status: 401 }),
          );
        }
        // The retry must still carry the body, not an empty request.
        expect(init?.body).toBeInstanceOf(FormData);
        return Promise.resolve(
          new Response(JSON.stringify({ product: { id: 3 }, warnings: [] }), { status: 200 }),
        );
      }
      return originalFetch(url, init);
    });

    try {
      setAuthToken('stale-token');
      const result = await api.importProduct(new File(['PK'], 'p.zip'), null);

      expect(refreshes).toBe(1);
      expect(seen).toEqual(['Bearer stale-token', 'Bearer fresh-token']);
      expect(result.product.id).toBe(3);
    } finally {
      global.fetch = originalFetch;
    }
  });

  it('throws an ApiError a caller can branch on, not a bare Error', async () => {
    const originalFetch = global.fetch;
    global.fetch = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      if (String(url).includes('/products/import')) {
        return Promise.resolve(
          new Response(JSON.stringify({ detail: 'An import may be at most 1 bytes' }), { status: 413 }),
        );
      }
      return originalFetch(url, init);
    });

    try {
      setAuthToken('test-token');
      await expect(api.importProduct(new File(['PK'], 'p.zip'), null)).rejects.toMatchObject({
        name: 'ApiError',
        status: 413,
        message: 'An import may be at most 1 bytes',
      });
    } finally {
      global.fetch = originalFetch;
    }
  });
});

describe('getLibraryFilesPaged (task 2, 2026-08-29 server-driven-lists)', () => {
  const emptyPage = { items: [], meta: { total: 0, current_page: 1, per_page: 50, last_page: 1 } };

  it('always sends `page` even with no params — the compat switch for the envelope', async () => {
    let query: URLSearchParams | null = null;
    server.use(
      http.get('/api/v1/library/files', ({ request }) => {
        query = new URL(request.url).searchParams;
        return HttpResponse.json(emptyPage);
      }),
    );

    await api.getLibraryFilesPaged();

    expect(query!.get('page')).toBe('1');
    // None of the optional filters are sent when omitted.
    expect(query!.has('folder_id')).toBe(false);
    expect(query!.has('q')).toBe(false);
    expect(query!.has('file_type')).toBe(false);
    expect(query!.has('unprinted_only')).toBe(false);
    expect(query!.has('username')).toBe(false);
    expect(query!.has('sort_by')).toBe(false);
    expect(query!.has('recursive')).toBe(false);
    expect(query!.getAll('tag_ids')).toEqual([]);
  });

  it('sends every filter, the sort and the page', async () => {
    let query: URLSearchParams | null = null;
    server.use(
      http.get('/api/v1/library/files', ({ request }) => {
        query = new URL(request.url).searchParams;
        return HttpResponse.json(emptyPage);
      }),
    );

    await api.getLibraryFilesPaged({
      folder_id: 7,
      product_id: 3,
      include_root: false,
      scope: 'external',
      tag_ids: [1, 2],
      recursive: true,
      q: 'benchy',
      file_type: 'gcode',
      unprinted_only: true,
      username: 'alice',
      sort_by: 'date_desc',
      page: 2,
      per_page: 25,
    });

    expect(query!.get('folder_id')).toBe('7');
    // `product_id`, the filter the route actually reads — `project_id` was
    // sent for months and dropped on the floor server-side.
    expect(query!.get('product_id')).toBe('3');
    expect(query!.has('project_id')).toBe(false);
    expect(query!.get('include_root')).toBe('false');
    expect(query!.get('external_only')).toBe('true');
    expect(query!.has('internal_only')).toBe(false);
    expect(query!.getAll('tag_ids')).toEqual(['1', '2']);
    expect(query!.get('recursive')).toBe('true');
    expect(query!.get('q')).toBe('benchy');
    expect(query!.get('file_type')).toBe('gcode');
    expect(query!.get('unprinted_only')).toBe('true');
    expect(query!.get('username')).toBe('alice');
    expect(query!.get('sort_by')).toBe('date_desc');
    expect(query!.get('page')).toBe('2');
    expect(query!.get('per_page')).toBe('25');
  });

  it('maps scope=internal to internal_only, not external_only', async () => {
    let query: URLSearchParams | null = null;
    server.use(
      http.get('/api/v1/library/files', ({ request }) => {
        query = new URL(request.url).searchParams;
        return HttpResponse.json(emptyPage);
      }),
    );

    await api.getLibraryFilesPaged({ scope: 'internal' });

    expect(query!.get('internal_only')).toBe('true');
    expect(query!.has('external_only')).toBe(false);
  });

  it('sends `all=true` and omits `per_page`, but still sends `page`', async () => {
    let query: URLSearchParams | null = null;
    server.use(
      http.get('/api/v1/library/files', ({ request }) => {
        query = new URL(request.url).searchParams;
        return HttpResponse.json(emptyPage);
      }),
    );

    await api.getLibraryFilesPaged({ all: true, per_page: 25, page: 3 });

    expect(query!.get('all')).toBe('true');
    expect(query!.has('per_page')).toBe(false);
    // ⚠️ Backend reads `page is not None` as the paginate switch even under
    // `all` (offset/limit come from the `all` branch instead) — dropping this
    // would silently fall back to the legacy flat-array response.
    expect(query!.get('page')).toBe('3');
  });

  it('returns the {items, meta} envelope untouched', async () => {
    const page = {
      items: [{ id: 1, filename: 'benchy.3mf' }],
      meta: { total: 1, current_page: 1, per_page: 50, last_page: 1 },
    };
    server.use(http.get('/api/v1/library/files', () => HttpResponse.json(page)));

    const result = await api.getLibraryFilesPaged({ page: 1 });

    expect(result).toEqual(page);
  });
});
