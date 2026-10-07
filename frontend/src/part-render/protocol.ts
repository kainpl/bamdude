/**
 * Wire contract between the part-render script and its Python child
 * (backend/app/services/part_render_node.py). Spec §5.2, §5.5, §5.6.
 */

/** Bump when anything that reaches the pixels or the instance choice changes (spec §8.5). Mirrored in part_render_protocol.py. */
export const RENDERER_VERSION = 2;

/** Largest manifest / error payload (spec §5.6). */
export const MANIFEST_MAX = 1024 * 1024;
/** Every frame's header: kind (1) + length (u32). */
export const FRAME_HEADER = 5;
/** Frame header and the object id in front of each PNG. */
export const PNG_FRAME_OVERHEAD = FRAME_HEADER + 4;
/** What the PNG budget leaves of `outputBytes` for the closing frame -- Python counts every stdout byte. */
export const CONTROL_FRAME_MAX = FRAME_HEADER + MANIFEST_MAX;

export interface Bounds {
  min: [number, number, number];
  max: [number, number, number];
}

export interface JobObject {
  id: number;
  mode: 'toolpath' | 'model';
  /** Object box from Metadata/plate_N.json `bbox_objects` -- [x0, y0, x1, y1] in mm; required for `model`. */
  bbox?: [number, number, number, number];
}

export interface RenderJob {
  size: number;
  objects: JobObject[];
  /** Samples per pixel side; 2 by default (spec §5.2). */
  supersample?: number;
  /** Ceiling on ALL stdout bytes of the attempt -- PNG frames, manifest and framing (spec §5.6); Python enforces it too. */
  outputBytes: number;
}

export interface RenderedObject {
  id: number;
  method: 'toolpath' | 'model';
  width: number;
  height: number;
  tools: number[];
  bounds: Bounds;
  segments: number;
  sha256: string;
  bytes: number;
}

export interface MissingObject {
  id: number;
  method: 'missing';
  reason: 'empty_selection' | 'model_unproven' | 'empty_render';
}

export interface RenderManifest {
  renderer: number;
  palette: string[];
  objects: Array<RenderedObject | MissingObject>;
}

/**
 * Spec §5.3: `parse_failed` is deterministic for the same bytes and settles the
 * plate; `crashed` (the render step or the attempt's own transport failed) and
 * `invalid_output` are transient and retried; `memory_limit` goes to the fallback
 * methods without retries.
 */
export type RenderErrorReason = 'parse_failed' | 'crashed' | 'invalid_output' | 'memory_limit';

/**
 * An allocation the heap could not satisfy -- a memory limit, not a crash and not a parse
 * failure (consilium N6). Asked by name, not instanceof: the engine's RangeError can come
 * from another realm (vitest's jsdom environment).
 */
export function isAllocationFailure(error: unknown): boolean {
  if (typeof error !== 'object' || error === null || (error as Error).name !== 'RangeError') return false;
  return /allocation failed|invalid (typed )?array length/i.test(String((error as Error).message));
}

export class RenderError extends Error {
  // A plain field, not a constructor parameter property: the app's tsconfig
  // sets erasableSyntaxOnly.
  readonly reason: RenderErrorReason;

  constructor(reason: RenderErrorReason, message: string) {
    super(message);
    this.name = 'RenderError';
    this.reason = reason;
  }
}
