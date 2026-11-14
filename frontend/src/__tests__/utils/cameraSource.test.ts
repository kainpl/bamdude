/**
 * One place builds every camera URL, so one place is where a wrong one shows up.
 *
 * The rule worth pinning is the asymmetry: a printer's frame carries `poll`
 * because a printer has a chamber light to hold on between frames, and a
 * standalone camera's does not, because a room has no light of ours to hold.
 */
import { describe, it, expect } from 'vitest';

import {
  newViewerId,
  printerSource,
  snapshotPath,
  sourceKey,
  standaloneSource,
  statusApiPath,
  stopPath,
  streamPath,
  viewerPath,
} from '../../utils/cameraSource';

describe('camera source paths', () => {
  it('keys the two kinds apart', () => {
    expect(sourceKey(printerSource(7))).toBe('printer-7');
    expect(sourceKey(standaloneSource(7))).toBe('camera-7');
    expect(sourceKey(printerSource(7))).not.toBe(sourceKey(standaloneSource(7)));
  });

  it('streams from the printer camera route or the camera route', () => {
    expect(streamPath(printerSource(7), 15, 'abc')).toBe('/api/v1/printers/7/camera/stream?fps=15&t=abc');
    expect(streamPath(standaloneSource(7), 15, 'abc')).toBe('/api/v1/cameras/7/stream?fps=15&t=abc');
  });

  it('names the stream it opens inside the address, where a token is appended after it', () => {
    expect(streamPath(printerSource(7), 15, 3, 'abcDEF_-0123')).toBe(
      '/api/v1/printers/7/camera/stream?fps=15&t=3&viewer=abcDEF_-0123',
    );
    expect(streamPath(standaloneSource(7), 15, 3, 'abcDEF_-0123')).toBe(
      '/api/v1/cameras/7/stream?fps=15&t=3&viewer=abcDEF_-0123',
    );
    expect(statusApiPath(printerSource(3), 'abcDEF_-0123')).toBe('/printers/3/camera/status?viewer=abcDEF_-0123');
    expect(statusApiPath(standaloneSource(3), 'abcDEF_-0123')).toBe('/cameras/3/status?viewer=abcDEF_-0123');
  });

  it('makes a stream id the server accepts, and a new one each time', () => {
    const ids = new Set(Array.from({ length: 50 }, newViewerId));
    expect(ids.size).toBe(50);
    for (const id of ids) expect(id).toMatch(/^[A-Za-z0-9_-]{8,32}$/);
  });

  it('declares the poll cadence only for a printer, which is the only one with a light', () => {
    expect(snapshotPath(printerSource(7), { bust: 1, pollMs: 8000 })).toBe(
      '/api/v1/printers/7/camera/snapshot?t=1&poll=8000',
    );
    expect(snapshotPath(standaloneSource(7), { bust: 1, pollMs: 8000 })).toBe('/api/v1/cameras/7/snapshot?t=1');
  });

  it('asks each kind for ITS OWN stream status', () => {
    // a standalone camera asked the printers' route was told "not active" about a healthy stream
    expect(statusApiPath(printerSource(3))).toBe('/printers/3/camera/status');
    expect(statusApiPath(standaloneSource(3))).toBe('/cameras/3/status');
  });

  it('no viewer polls a printer route for whatever source it shows', async () => {
    const { readFileSync } = await import('node:fs');
    const { resolve } = await import('node:path');
    for (const file of ['components/EmbeddedCameraViewer.tsx', 'pages/CameraPage.tsx']) {
      const text = readFileSync(resolve(__dirname, '../..', file), 'utf8');
      expect(text, file).not.toMatch(/api\.getCameraStatus\(/);
      expect(text, file).toMatch(/api\.getCameraSourceStatus\(source[,)]/);
    }
  });

  it('stops and opens the right thing', () => {
    expect(stopPath(printerSource(3))).toBe('/api/v1/printers/3/camera/stop');
    expect(stopPath(standaloneSource(3))).toBe('/api/v1/cameras/3/stop');
    expect(viewerPath(printerSource(3))).toBe('/camera/3');
    expect(viewerPath(standaloneSource(3))).toBe('/camera/standalone/3');
  });
});
