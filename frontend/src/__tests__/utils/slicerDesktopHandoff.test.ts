/**
 * What each desktop slicer takes over its LINK handler (upstream e2493132).
 *
 * Not what the two applications can open — both import an STL from their File
 * menu. Bambu Studio routes every `bambustudio://` / `bambustudioopen://` URL
 * into an import path that refuses any filename that is not `.3mf`, before
 * fetching anything ("Download failed, unknown file format"). OrcaSlicer sends
 * `orcaslicer://open?file=` against our host to its general downloader, which
 * checks no extension — STL and STEP work there.
 */

import { describe, expect, it } from 'vitest';

import { desktopSlicerAccepts, desktopSlicersFor } from '../../utils/slicer';

describe('desktopSlicerAccepts', () => {
  it('Bambu Studio takes only a 3MF over a link', () => {
    expect(desktopSlicerAccepts('3mf', 'bambu_studio')).toBe(true);
    for (const type of ['stl', 'step', 'stp', 'obj']) {
      expect(desktopSlicerAccepts(type, 'bambu_studio')).toBe(false);
    }
  });

  it('OrcaSlicer takes 3MF, STL and STEP', () => {
    for (const type of ['3mf', 'stl', 'step', 'stp', 'STL']) {
      expect(desktopSlicerAccepts(type, 'orcaslicer')).toBe(true);
    }
    expect(desktopSlicerAccepts('obj', 'orcaslicer')).toBe(false);
  });

  it('an unknown slicer value answers as Bambu Studio — what openInSlicer sends it to', () => {
    expect(desktopSlicerAccepts('stl', 'something' as never)).toBe(false);
    expect(desktopSlicerAccepts('3mf', 'something' as never)).toBe(true);
  });
});

describe('desktopSlicersFor', () => {
  it('puts the preferred slicer first when it takes the file', () => {
    expect(desktopSlicersFor('3mf', 'orcaslicer')).toEqual(['orcaslicer', 'bambu_studio']);
  });

  it('leaves out a slicer that cannot take the file', () => {
    expect(desktopSlicersFor('stl', 'bambu_studio')).toEqual(['orcaslicer']);
  });

  it('is empty when neither can', () => {
    expect(desktopSlicersFor('obj', 'bambu_studio')).toEqual([]);
  });
});
