/**
 * The File Manager's side of "Slice" in the "Assemble project" summary. What
 * the summary draws is tested with the dialog; this pins the wiring — the same
 * gate as the file rows' in-app slicing, and the same Slice dialog.
 */
import { describe, expect, it } from 'vitest';

import pageSource from '../../pages/FileManagerPage.tsx?raw';

const source = pageSource.replace(/\s+/g, ' ');
// `includes` on purpose: a failed `toContain` would print the whole page.
const has = (text: string) => source.includes(text.replace(/\s+/g, ' '));

describe('FileManagerPage — slicing a file "Assemble project" wrote', () => {
  it('offers it only where the file rows offer in-app slicing', () => {
    expect(has('<AssembleProjectModal')).toBe(true);
    expect(has('onSliceFile={')).toBe(true);
    // sidecar on, and the right to write the sliced file — else no action at all
    expect(
      has(`(settings?.use_slicer_api ?? false) && hasPermission('library:upload')
        ? (id, filename) => {
            setAssembleMode(null);
            setSliceFile({ id, filename });
          }
        : undefined`),
    ).toBe(true);
  });

  it('opens the one Slice dialog the page has, for that file', () => {
    expect(pageSource.match(/<SliceModal\b/g)).toHaveLength(1);
    expect(has("source={{ kind: 'libraryFile', id: sliceFile.id, filename: sliceFile.filename }}")).toBe(true);
  });
});
