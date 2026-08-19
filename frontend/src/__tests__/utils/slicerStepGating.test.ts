/**
 * A STEP gets the desktop handoff, never the sidecar.
 *
 * Ported from upstream #57190b10. The Slice action appeared on `.step` / `.stp`
 * and the endpoint accepted the job, but neither slicer can load one from its
 * command line — both answer "Unknown file format. Input file must have .stl,
 * .obj, .amf(.xml) extension." So the file was read, converted and uploaded
 * before failing as "the input model file to the slicer can not be parsed",
 * which reads as a corrupt model rather than an unsupported format.
 *
 * ⚠️ "Open in Slicer" is unchanged and still hands a STEP to the desktop
 * application, which opens it fine. That was always the working path — the two
 * questions are kept as separate predicates so they cannot drift back
 * together.
 */

import { readFileSync } from 'node:fs';
import { describe, it, expect } from 'vitest';

import { API_SLICEABLE_FILE_TYPES, isApiSliceableFileType } from '../../utils/slicer';

describe('what the sidecar can slice', () => {
  it('accepts the two formats a slicer CLI actually loads', () => {
    expect(isApiSliceableFileType('stl')).toBe(true);
    expect(isApiSliceableFileType('3mf')).toBe(true);
  });

  it('refuses STEP under either extension', () => {
    expect(isApiSliceableFileType('step')).toBe(false);
    expect(isApiSliceableFileType('stp')).toBe(false);
  });

  it('is case-insensitive, because a file_type is whatever was uploaded', () => {
    expect(isApiSliceableFileType('STL')).toBe(true);
    expect(isApiSliceableFileType('STEP')).toBe(false);
  });

  it('refuses a missing or empty type rather than guessing', () => {
    expect(isApiSliceableFileType(undefined)).toBe(false);
    expect(isApiSliceableFileType(null)).toBe(false);
    expect(isApiSliceableFileType('')).toBe(false);
  });

  it('refuses an already-sliced file', () => {
    expect(isApiSliceableFileType('gcode')).toBe(false);
  });

  it('names exactly the two formats and no more', () => {
    // If STEP ever comes back into this list it should be because a slicer CLI
    // learned to read one, not because the list drifted.
    expect([...API_SLICEABLE_FILE_TYPES]).toEqual(['3mf', 'stl']);
  });
});

describe('wiring', () => {
  const MODAL = readFileSync('src/components/ModelViewerModal.tsx', 'utf8');

  it('the in-app slice gate asks the sidecar predicate', () => {
    expect(MODAL).toContain('const sliceableType = isApiSliceableFileType(fileType)');
  });

  it('no hand-rolled list decides it any more', () => {
    expect(MODAL).not.toContain("['3mf', 'stl', 'step', 'stp'].includes");
  });

  it('the desktop handoff is untouched', () => {
    // ⚠️ Open in Slicer must still reach a STEP — it is the path that works.
    expect(MODAL).toContain('const handleOpenInSlicer');
    const handler = MODAL.slice(MODAL.indexOf('const handleOpenInSlicer'));
    expect(handler.slice(0, 600)).not.toContain('isApiSliceableFileType');
  });
});
