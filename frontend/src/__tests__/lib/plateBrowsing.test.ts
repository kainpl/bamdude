import { describe, it, expect } from 'vitest';
import { figuresAt, formatMaterials, plateAt, plateSlices, plateThumbnailUrl, step } from '../../lib/plateBrowsing';
import type { LibraryFileListItem, PlateSummary } from '../../api/client';

const slice = (index: number, over: Partial<PlateSummary> = {}): PlateSummary => ({
  index,
  name: null,
  print_time_seconds: index * 600,
  filament_used_grams: index * 10,
  object_count: index,
  filament_types: index === 2 ? ['PETG', 'PLA'] : ['PLA'],
  has_thumbnail: index !== 3,
  ...over,
});

const file = (over: Partial<LibraryFileListItem> = {}): LibraryFileListItem =>
  ({
    id: 7,
    thumbnail_path: 'x.png',
    print_time_seconds: 100,
    filament_used_grams: 1,
    object_count: 9,
    filament_types: ['ASA'],
    ...over,
  }) as LibraryFileListItem;

describe('plateBrowsing', () => {
  it('a single-plate file is never a carousel and answers with its own figures', () => {
    const f = file({ plate_summaries: [slice(1)] });
    expect(plateSlices(f)).toEqual([]);
    expect(plateAt(f, 0)).toBeNull();
    expect(figuresAt(f, 3)).toEqual({
      print_time_seconds: 100,
      filament_used_grams: 1,
      object_count: 9,
      filament_types: ['ASA'],
    });
    expect(plateThumbnailUrl(f, 0, 5)).toBe('/api/v1/library/files/7/thumbnail?v=5');
  });

  it('a multi-plate file answers with the current slice, clamped', () => {
    const f = file({ plate_summaries: [slice(1), slice(2), slice(3)] });
    expect(figuresAt(f, 1)).toEqual({
      print_time_seconds: 1200,
      filament_used_grams: 20,
      object_count: 2,
      filament_types: ['PETG', 'PLA'],
    });
    expect(plateAt(f, 99)?.index).toBe(3);
    expect(plateAt(f, -1)?.index).toBe(1);
  });

  it('position 0 keeps the FILE thumbnail (and its regen version); other positions use the plate picture or nothing', () => {
    const f = file({ plate_summaries: [slice(1), slice(2), slice(3)] });
    expect(plateThumbnailUrl(f, 0, 5)).toBe('/api/v1/library/files/7/thumbnail?v=5');
    expect(plateThumbnailUrl(f, 1)).toBe('/api/v1/library/files/7/plate-thumbnail/2');
    expect(plateThumbnailUrl(f, 2)).toBeNull(); // plate 3 has no thumbnail
    expect(plateThumbnailUrl(file({ thumbnail_path: null }), 0)).toBeNull();
  });

  it('step wraps around and is inert on a single plate', () => {
    expect(step(0, 3, -1)).toBe(2);
    expect(step(2, 3, 1)).toBe(0);
    expect(step(1, 3, 1)).toBe(2);
    expect(step(0, 1, 1)).toBe(0);
  });

  it('materials read as one plus-joined token', () => {
    expect(formatMaterials(['PETG', 'PLA', 'TPU'])).toBe('PETG+PLA+TPU');
    expect(formatMaterials([])).toBe('');
  });
});
