import { afterEach, describe, expect, it } from 'vitest';
import { checkEmbeddedCamera, readStoredEmbeddedCamera } from '../../utils/embeddedCameraSelection';

const printers = [{ id: 1, name: 'X1 Carbon' }, { id: 2, name: 'P1S Backup' }];
const cameras = [{ id: 2, name: 'Door', enabled: true }, { id: 5, name: 'Retired', enabled: false }];

describe('readStoredEmbeddedCamera', () => {
  afterEach(() => localStorage.removeItem('openEmbeddedCameras'));

  it('reads the last valid entry, and an entry without a kind as a printer', () => {
    localStorage.setItem('openEmbeddedCameras', JSON.stringify([
      { id: 1, name: 'X1 Carbon' }, { id: 0, name: 'bad' }, { kind: 'camera', id: 2, name: 'Door' }, { id: 3 },
    ]));
    expect(readStoredEmbeddedCamera()).toEqual({ kind: 'camera', id: 2, name: 'Door' });

    localStorage.setItem('openEmbeddedCameras', JSON.stringify([{ id: 1, name: 'X1 Carbon' }]));
    expect(readStoredEmbeddedCamera()).toEqual({ kind: 'printer', id: 1, name: 'X1 Carbon' });
  });

  it('reads nothing from junk', () => {
    expect(readStoredEmbeddedCamera()).toBeNull();
    localStorage.setItem('openEmbeddedCameras', '{not json');
    expect(readStoredEmbeddedCamera()).toBeNull();
    localStorage.setItem('openEmbeddedCameras', '"text"');
    expect(readStoredEmbeddedCamera()).toBeNull();
  });
});

describe('checkEmbeddedCamera', () => {
  const printerOne = { kind: 'printer' as const, id: 1, name: 'X1 Carbon' };
  const cameraTwo = { kind: 'camera' as const, id: 2, name: 'Door' };

  it('drops the window of a user who may not view cameras, whatever is known', () => {
    expect(checkEmbeddedCamera(printerOne, { canView: false, printers, cameras })).toBe('drop');
    expect(checkEmbeddedCamera(printerOne, { canView: false, printers: undefined, cameras: undefined })).toBe('drop');
  });

  it('waits until the list that can vouch for the source has been fetched', () => {
    expect(checkEmbeddedCamera(printerOne, { canView: true, printers: undefined, cameras })).toBe('wait');
    expect(checkEmbeddedCamera(cameraTwo, { canView: true, printers, cameras: undefined })).toBe('wait');
  });

  it('shows a source that is in its list', () => {
    expect(checkEmbeddedCamera(printerOne, { canView: true, printers, cameras: undefined })).toBe('show');
    expect(checkEmbeddedCamera(cameraTwo, { canView: true, printers: undefined, cameras })).toBe('show');
  });

  it('drops a printer or camera that is gone', () => {
    expect(checkEmbeddedCamera({ kind: 'printer', id: 9, name: 'Sold' }, { canView: true, printers, cameras })).toBe('drop');
    expect(checkEmbeddedCamera({ kind: 'camera', id: 9, name: 'Gone' }, { canView: true, printers, cameras })).toBe('drop');
  });

  it('drops a camera that was switched off: its routes refuse', () => {
    expect(checkEmbeddedCamera({ kind: 'camera', id: 5, name: 'Retired' }, { canView: true, printers, cameras })).toBe('drop');
  });

  it('never answers for a printer with a camera of the same id, or the reverse', () => {
    expect(checkEmbeddedCamera({ kind: 'camera', id: 1, name: 'X' }, { canView: true, printers, cameras })).toBe('drop');
    expect(checkEmbeddedCamera({ kind: 'printer', id: 5, name: 'X' }, { canView: true, printers, cameras })).toBe('drop');
  });
});
