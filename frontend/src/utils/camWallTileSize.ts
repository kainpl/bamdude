/**
 * The Cam Wall's tile size: S · M · L · XL, the same 1–4 scale the card-size
 * switch uses, kept apart from the card size (a wall of big tiles and a page
 * of small cards are both reasonable at once).
 *
 * ⚠️ The size changes the GRID and nothing else. Which tiles are live is not
 * a matter of how many fit in a row: the wall runs one live stream at a time,
 * the one the operator picked, and every other visible tile is a snapshot.
 */

/** Where the last pick is remembered, per browser. */
export const CAM_WALL_TILE_SIZE_KEY = 'camWallTileSize';

export const DEFAULT_CAM_WALL_TILE_SIZE = 2;

/** Tiles per row at each size. M is the grid the wall always had. */
const GRID_BY_SIZE: Record<number, string> = {
  1: 'grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5',
  2: 'grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4',
  3: 'grid grid-cols-1 gap-3 lg:grid-cols-2',
  4: 'grid grid-cols-1 gap-3',
};

/** The wall's grid classes; anything that is not 1–4 is M. */
export function camWallGridClass(size: number | null | undefined): string {
  return GRID_BY_SIZE[size ?? DEFAULT_CAM_WALL_TILE_SIZE] ?? GRID_BY_SIZE[DEFAULT_CAM_WALL_TILE_SIZE];
}

// A Map, not an object: `?size=constructor` must find nothing.
const SIZE_BY_PARAM = new Map<string, number>([['s', 1], ['m', 2], ['l', 3], ['xl', 4]]);

/**
 * `?size=s|m|l|xl` of the standalone wall, in any case. Null for anything
 * else, so the wall falls back instead of drawing a grid nobody asked for.
 */
export function camWallTileSizeFromParam(raw: string | null | undefined): number | null {
  if (!raw) return null;
  return SIZE_BY_PARAM.get(raw.toLowerCase()) ?? null;
}
