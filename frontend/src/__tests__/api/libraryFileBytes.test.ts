/**
 * The bytes of a library file, as the PDF viewer asks for them: a refusal
 * carries its HTTP status, so the viewer can tell "not available" from a
 * request that never reached the server.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { ApiError, api } from '../../api/client';

describe('api.getLibraryFileBytes', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('returns the bytes of the download route', async () => {
    const fetchMock = vi.fn(async () => new Response(new Uint8Array([1, 2, 3]), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    const bytes = await api.getLibraryFileBytes(7);

    expect(new Uint8Array(bytes)).toEqual(new Uint8Array([1, 2, 3]));
    expect(String((fetchMock.mock.calls[0] as unknown[])[0])).toContain('/library/files/7/download');
  });

  it.each([403, 404, 500])('names the status of a refusal (%i)', async (status) => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status })));

    const error = await api.getLibraryFileBytes(7).then(
      () => null,
      (e: unknown) => e,
    );

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(status);
  });

  it('lets a request that never reached the server through as it is', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(new TypeError('Failed to fetch'))));

    await expect(api.getLibraryFileBytes(7)).rejects.toBeInstanceOf(TypeError);
  });
});
