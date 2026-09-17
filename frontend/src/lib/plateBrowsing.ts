/**
 * What "the current plate" of a library file means - shared by the grid card
 * and the list row so the two can never disagree about it
 * (vault 60-specs/library-multiplate-card-spec 7).
 *
 * A file is a carousel only when its list row carries two or more plate
 * slices; everything else reads the file's own top-level figures.
 */
import { api, type LibraryFileListItem, type PlateSummary } from '../api/client';

type Browsable = Pick<
  LibraryFileListItem,
  | 'id'
  | 'thumbnail_path'
  | 'print_time_seconds'
  | 'filament_used_grams'
  | 'object_count'
  | 'filament_types'
  | 'plate_summaries'
>;

export interface PlateFigures {
  print_time_seconds: number | null;
  filament_used_grams: number | null;
  object_count: number | null;
  filament_types: string[];
}

/** The slices the card pages through; empty means "not a carousel". */
export function plateSlices(file: Pick<Browsable, 'plate_summaries'>): PlateSummary[] {
  const slices = file.plate_summaries ?? [];
  return slices.length > 1 ? slices : [];
}

/** The slice at a position, clamped into range; null for a non-carousel. */
export function plateAt(file: Pick<Browsable, 'plate_summaries'>, current: number): PlateSummary | null {
  const slices = plateSlices(file);
  if (slices.length === 0) return null;
  return slices[Math.min(Math.max(current, 0), slices.length - 1)];
}

export function figuresAt(file: Browsable, current: number): PlateFigures {
  const plate = plateAt(file, current);
  if (plate) {
    return {
      print_time_seconds: plate.print_time_seconds,
      filament_used_grams: plate.filament_used_grams,
      object_count: plate.object_count,
      filament_types: plate.filament_types,
    };
  }
  return {
    print_time_seconds: file.print_time_seconds ?? null,
    filament_used_grams: file.filament_used_grams ?? null,
    object_count: file.object_count ?? null,
    filament_types: file.filament_types ?? [],
  };
}

/**
 * Position 0 keeps the FILE thumbnail - an STL's own render, or plate_1.png for
 * a slice - with its regeneration version; any other position is that plate's
 * own picture, or nothing when the 3MF carries none (plate thumbnails are read
 * straight from the ZIP and never regenerated).
 */
export function plateThumbnailUrl(
  file: Pick<Browsable, 'id' | 'thumbnail_path' | 'plate_summaries'>,
  current: number,
  version?: number | string | null,
): string | null {
  const plate = current > 0 ? plateAt(file, current) : null;
  if (plate) return plate.has_thumbnail ? api.getLibraryFilePlateThumbnail(file.id, plate.index) : null;
  if (!file.thumbnail_path) return null;
  return `${api.getLibraryFileThumbnailUrl(file.id)}${version ? `?v=${version}` : ''}`;
}

/** Next/previous position, wrapping; inert when there is nothing to page. */
export function step(current: number, count: number, delta: 1 | -1): number {
  if (count <= 1) return 0;
  return (current + delta + count) % count;
}

export function formatMaterials(types: string[]): string {
  return types.join('+');
}
