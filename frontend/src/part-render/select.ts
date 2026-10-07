import {
  ToolpathType,
  SEGMENT_TYPED,
  SEGMENT_AFTER_FIRST_LAYER,
  type ParsedToolpath,
  type ToolpathLayer,
} from '../lib/gcodeToolpath';
import type { Bounds } from './protocol';

/** Spec §5.2: travels and every service toolpath, the whole brim family included. */
export const HIDDEN_TYPES: ReadonlySet<number> = new Set([
  ToolpathType.travel,
  ToolpathType.skirt,
  ToolpathType.support,
  ToolpathType.raft,
  ToolpathType.primeTower,
]);

export type Target = { kind: 'object'; id: number } | { kind: 'model' };

export interface Selection {
  layers: ToolpathLayer[];
  bounds: Bounds | null;
  segments: number;
  /** Segment count per original T number. */
  tools: Record<number, number>;
}

const MODEL_FLAGS = SEGMENT_TYPED | SEGMENT_AFTER_FIRST_LAYER;

/**
 * The records one picture is made of. `object` keeps what the slicer marked
 * with that id; `model` -- only for a plate with one object and no markers --
 * keeps unowned records that carry an explicit feature and come after the
 * first layer marker, which drops start G-code and the purge line (spec §5.3).
 * The record type is re-keyed to `tool + 1`: vertices of different filaments
 * then never merge, and the colour pass reads the filament back from it.
 */
export function selectSegments(parsed: ParsedToolpath, target: Target): Selection {
  const min: [number, number, number] = [Infinity, Infinity, Infinity];
  const max: [number, number, number] = [-Infinity, -Infinity, -Infinity];
  const tools: Record<number, number> = {};
  let segments = 0;

  const layers = parsed.layers.map((layer): ToolpathLayer => {
    const paths: number[] = [];
    const widths: number[] = [];
    for (let i = 0; i < layer.paths.length; i += 8) {
      const k = i / 8;
      const type = layer.paths[i + 3];
      if (HIDDEN_TYPES.has(type)) continue;
      const owner = layer.objectIds?.[k] ?? -1;
      const flags = layer.flags?.[k] ?? 0;
      const wanted = target.kind === 'object'
        ? owner === target.id
        : owner === -1 && (flags & MODEL_FLAGS) === MODEL_FLAGS;
      if (!wanted) continue;

      const tool = layer.paths[i + 7];
      tools[tool] = (tools[tool] ?? 0) + 1;
      segments += 1;
      for (let j = 0; j < 8; j += 1) paths.push(layer.paths[i + j]);
      paths[paths.length - 5] = tool + 1;
      widths.push(layer.widths[k] ?? 0);
      for (const at of [i, i + 4]) {
        for (let axis = 0; axis < 3; axis += 1) {
          const v = layer.paths[at + axis];
          if (v < min[axis]) min[axis] = v;
          if (v > max[axis]) max[axis] = v;
        }
      }
    }
    return { z: layer.z, paths: new Float32Array(paths), widths };
  });

  return { layers, segments, tools, bounds: segments > 0 ? { min, max } : null };
}

const PALETTE_LINE = /^; filament_colour = (.+)$/m;
/** One palette slot: #RRGGBB, optionally #RRGGBBAA. Not global -- exec() keeps no state between slots. */
const SLOT_COLOUR = /^#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?/;
/** The header sits at the top; a palette line deep in the body would be someone's comment. */
const PALETTE_SCAN_BYTES = 200_000;

/** `filament_colour` from the G-code header, indexed by the original T number (gaps kept). */
export function paletteFromGcode(gcode: string): string[] {
  const match = PALETTE_LINE.exec(gcode.slice(0, PALETTE_SCAN_BYTES));
  if (!match) return [];
  // One slot per `;`-separated entry: an empty or malformed one stays '' in its
  // place, so every later T index keeps its own colour.
  return match[1].split(';').map((entry) => {
    const colour = SLOT_COLOUR.exec(entry.trim());
    return colour ? colour[0].slice(0, 7).toUpperCase() : '';
  });
}

/** Whether selected bounds lie inside an object box from plate_N.json, in mm, with tolerance. */
export function fitsBox(bounds: Bounds, box: [number, number, number, number], tolMm = 2): boolean {
  const [x0, y0, x1, y1] = box;
  return (
    bounds.min[0] >= x0 - tolMm &&
    bounds.min[1] >= y0 - tolMm &&
    bounds.max[0] <= x1 + tolMm &&
    bounds.max[1] <= y1 + tolMm
  );
}
