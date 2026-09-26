/**
 * Pictures take a media token, the camera takes the camera token (audit D9 a2,
 * upstream 816f073a).
 *
 * A browser cannot put an Authorization header on an `<img src>`, so every
 * picture URL carries a token. Two exist now, and they are not interchangeable:
 * the camera routes refuse a media token and every other picture refuses a
 * camera token. So a URL builder, the retrofit that stamps a token onto images
 * rendered before it arrived, and the recovery after a failed load all have to
 * pick by PATH.
 *
 * And the media token is for every signed-in user: the camera token costs
 * `camera:view`, and asking for it without that permission was a 403 on every
 * page load for everyone else.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import {
  api,
  isCameraMediaPath,
  setMediaToken,
  setStreamToken,
  withMediaToken,
} from '../../api/client';
import { rewriteMediaSrcWithToken, useStreamTokenSync } from '../../hooks/useCameraStreamToken';

const auth = vi.hoisted(() => ({ canViewCamera: false }));

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 7, username: 'viewer' },
    hasPermission: (p: string) => (p === 'camera:view' ? auth.canViewCamera : true),
  }),
}));

afterEach(() => {
  setMediaToken(null);
  setStreamToken(null);
  vi.restoreAllMocks();
});

describe('withMediaToken', () => {
  it('stamps a same-origin API path', () => {
    setMediaToken('M1');
    expect(withMediaToken('/api/v1/archives/5/plate-thumbnail/1')).toBe('/api/v1/archives/5/plate-thumbnail/1?token=M1');
    expect(withMediaToken('/api/v1/archives/5/thumbnail?v=2')).toBe('/api/v1/archives/5/thumbnail?v=2&token=M1');
  });

  it('leaves what is not an API path alone', () => {
    setMediaToken('M1');
    expect(withMediaToken('data:image/png;base64,AAAA')).toBe('data:image/png;base64,AAAA');
    expect(withMediaToken('blob:http://x/123')).toBe('blob:http://x/123');
    expect(withMediaToken('https://makerworld.bblmw.com/a.png')).toBe('https://makerworld.bblmw.com/a.png');
  });

  it('returns the URL bare until the token has arrived', () => {
    expect(withMediaToken('/api/v1/archives/5/thumbnail')).toBe('/api/v1/archives/5/thumbnail');
  });
});

describe('isCameraMediaPath', () => {
  it.each([
    '/api/v1/printers/3/camera/stream?fps=10',
    '/api/v1/printers/3/camera/snapshot',
    '/api/v1/cameras/2/stream',
    '/api/v1/cameras/2/snapshot?x=1',
    '/api/v1/printers/3/camera/plate-detection/references/0/thumbnail',
  ])('%s is the camera', (path) => {
    expect(isCameraMediaPath(path)).toBe(true);
  });

  it.each([
    '/api/v1/printers/3/camera-cover',
    '/api/v1/archives/5/thumbnail',
    '/api/v1/library/files/9/card-file/Auxiliaries/a.png',
    '/api/v1/projects/4/cover-image',
  ])('%s is not the camera', (path) => {
    expect(isCameraMediaPath(path)).toBe(false);
  });
});

describe('the URL builders pick the token by what they serve', () => {
  beforeEach(() => {
    setMediaToken('MEDIA');
    setStreamToken('CAMERA');
  });

  it.each([
    ['archive thumbnail', () => api.getArchiveThumbnail(5)],
    ['archive plate thumbnail', () => api.getArchivePlateThumbnail(5, 1)],
    ['archive plate preview', () => api.getArchivePlatePreview(5)],
    ['archive timelapse', () => api.getArchiveTimelapse(5)],
    ['archive QR code', () => api.getArchiveQRCodeUrl(5)],
    ['archive project image', () => api.getArchiveProjectImageUrl(5, 'Metadata/pick_1.png')],
    ['library thumbnail', () => api.getLibraryFileThumbnailUrl(9)],
    ['library plate thumbnail', () => api.getLibraryFilePlateThumbnail(9, 1)],
    ['MakerWorld import cover', () => api.getMakerworldImportCoverUrl(9)],
    ['product picture', () => api.getProductAttachmentImageUrl(4, 'a.png')],
    ['product cover', () => api.getProductCoverImageUrl(4)],
    ['order cover', () => api.getProjectCoverImageUrl(4)],
  ])('%s carries the media token', (_name, build) => {
    const url = build();
    expect(url).toContain('token=MEDIA');
    expect(url).not.toContain('CAMERA');
  });

  it('the camera keeps the camera token', () => {
    expect(api.getCameraSnapshotUrl(3)).toContain('token=CAMERA');
  });

  it('an operator photo needs none — notifications link it', () => {
    expect(api.getArchivePhotoUrl(5, 'finish_1.jpg')).not.toContain('token=');
  });
});

describe('rewriteMediaSrcWithToken, by path', () => {
  it('stamps only what the predicate accepts', () => {
    const root = document.createElement('div');
    const [camera, thumb] = ['/api/v1/printers/3/camera/snapshot', '/api/v1/archives/5/thumbnail'].map((src) => {
      const img = document.createElement('img');
      img.setAttribute('src', src);
      root.appendChild(img);
      return img;
    });

    rewriteMediaSrcWithToken(root, 'CAMERA', isCameraMediaPath);
    rewriteMediaSrcWithToken(root, 'MEDIA', (src) => !isCameraMediaPath(src));

    expect(camera.getAttribute('src')).toBe('/api/v1/printers/3/camera/snapshot?token=CAMERA');
    expect(thumb.getAttribute('src')).toBe('/api/v1/archives/5/thumbnail?token=MEDIA');
  });
});

describe('useStreamTokenSync', () => {
  function wrapper({ children }: { children: ReactNode }) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  }

  it('fetches a media token for a user who may not see the camera, and no camera token', async () => {
    auth.canViewCamera = false;
    const media = vi.spyOn(api, 'getMediaToken').mockResolvedValue({ token: 'MEDIA' });
    const camera = vi.spyOn(api, 'getCameraStreamToken').mockResolvedValue({ token: 'CAMERA' });

    renderHook(() => useStreamTokenSync(), { wrapper });

    await waitFor(() => expect(media).toHaveBeenCalled());
    expect(camera).not.toHaveBeenCalled();
    await waitFor(() => expect(api.getArchiveThumbnail(5)).toContain('token=MEDIA'));
  });

  it('fetches both for a user who may see the camera', async () => {
    auth.canViewCamera = true;
    const media = vi.spyOn(api, 'getMediaToken').mockResolvedValue({ token: 'MEDIA' });
    const camera = vi.spyOn(api, 'getCameraStreamToken').mockResolvedValue({ token: 'CAMERA' });

    renderHook(() => useStreamTokenSync(), { wrapper });

    await waitFor(() => expect(media).toHaveBeenCalled());
    await waitFor(() => expect(camera).toHaveBeenCalled());
  });

  it('stamps images already on the page with the token their path needs', async () => {
    auth.canViewCamera = true;
    vi.spyOn(api, 'getMediaToken').mockResolvedValue({ token: 'MEDIA' });
    vi.spyOn(api, 'getCameraStreamToken').mockResolvedValue({ token: 'CAMERA' });
    const thumb = document.createElement('img');
    thumb.setAttribute('src', '/api/v1/archives/5/thumbnail');
    const snap = document.createElement('img');
    snap.setAttribute('src', '/api/v1/printers/3/camera/snapshot');
    document.body.append(thumb, snap);

    try {
      renderHook(() => useStreamTokenSync(), { wrapper });
      await waitFor(() => expect(thumb.getAttribute('src')).toBe('/api/v1/archives/5/thumbnail?token=MEDIA'));
      await waitFor(() => expect(snap.getAttribute('src')).toBe('/api/v1/printers/3/camera/snapshot?token=CAMERA'));
    } finally {
      thumb.remove();
      snap.remove();
    }
  });
});
