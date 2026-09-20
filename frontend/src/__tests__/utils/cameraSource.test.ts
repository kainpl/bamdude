/**
 * One place builds every camera URL, so one place is where a wrong one shows up.
 *
 * The rule worth pinning is the asymmetry: a printer's frame carries `poll`
 * because a printer has a chamber light to hold on between frames, and a
 * standalone camera's does not, because a room has no light of ours to hold.
 */
import { describe, it, expect } from 'vitest';

import {
  printerSource,
  snapshotPath,
  sourceKey,
  standaloneSource,
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

  it('declares the poll cadence only for a printer, which is the only one with a light', () => {
    expect(snapshotPath(printerSource(7), { bust: 1, pollMs: 8000 })).toBe(
      '/api/v1/printers/7/camera/snapshot?t=1&poll=8000',
    );
    expect(snapshotPath(standaloneSource(7), { bust: 1, pollMs: 8000 })).toBe('/api/v1/cameras/7/snapshot?t=1');
  });

  it('stops and opens the right thing', () => {
    expect(stopPath(printerSource(3))).toBe('/api/v1/printers/3/camera/stop');
    expect(stopPath(standaloneSource(3))).toBe('/api/v1/cameras/3/stop');
    expect(viewerPath(printerSource(3))).toBe('/camera/3');
    expect(viewerPath(standaloneSource(3))).toBe('/camera/standalone/3');
  });
});
