/**
 * A part's picture (spec part-thumbnails §11, §12.1): `v` and `size` go INTO the address before the
 * media token, and the path is a media path, never a camera one.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, isCameraMediaPath, setMediaToken } from '../../api/client';

afterEach(() => {
  setMediaToken(null);
  vi.unstubAllGlobals();
});

describe('part picture addresses', () => {
  it('puts v and size before the media token', () => {
    setMediaToken('tok');
    expect(api.partImageUrl(5, 'abc123', 'lg')).toBe('/api/v1/product-parts/5/image?v=abc123&size=lg&token=tok');
  });

  it('asks for the small picture without a version when the part has none yet', () => {
    expect(api.partImageUrl(5, null)).toBe('/api/v1/product-parts/5/image?size=sm');
  });

  it('builds a plate object picture under its product', () => {
    expect(api.partInstanceImageUrl(2, 9, 1, 4294967295, 'v1')).toBe(
      '/api/v1/products/2/files/9/plates/1/objects/4294967295/image?v=v1&size=sm',
    );
  });

  it('takes the media token, not the camera one', () => {
    expect(isCameraMediaPath(api.partImageUrl(5, 'v'))).toBe(false);
    expect(isCameraMediaPath(api.partInstanceImageUrl(2, 9, 1, 3, null))).toBe(false);
  });
});

describe('part picture doors', () => {
  it('pins through PUT and sends the photo as a form', async () => {
    const seen: Array<{ url: string; method?: string; body?: unknown }> = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        seen.push({ url, method: init?.method, body: init?.body });
        // `headers`: request() reads content-length before it parses the body
        return { ok: true, status: 200, headers: new Headers(), json: async () => ({ id: 7 }) } as unknown as Response;
      }),
    );
    await api.setPartImage(7, { source: 'instance', instance: { library_file_id: 9, plate_index: 1, identify_id: 3 } });
    await api.uploadPartPhoto(7, new File(['x'], 'p.png', { type: 'image/png' }));
    await api.deletePartPhoto(7);
    expect(seen.map((s) => `${s.method} ${s.url}`)).toEqual([
      'PUT /api/v1/product-parts/7/image',
      'POST /api/v1/product-parts/7/image/photo',
      'DELETE /api/v1/product-parts/7/image/photo',
    ]);
    expect(seen[1].body).toBeInstanceOf(FormData);
  });
});
