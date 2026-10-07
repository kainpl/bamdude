/**
 * Wire contract between the part-render script and its Python child
 * (backend/app/services/part_render_node.py). Spec §5.2, §5.5, §5.6.
 */

/** Bump when anything that reaches the pixels or the instance choice changes (spec §8.5). Mirrored in part_render_protocol.py. */
export const RENDERER_VERSION = 2;

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
  reason: 'empty_selection' | 'model_unproven';
}

export interface RenderManifest {
  renderer: number;
  palette: string[];
  objects: Array<RenderedObject | MissingObject>;
}

/**
 * Spec §5.3: `parse_failed` is deterministic for the same bytes and settles the
 * plate; `crashed` (the render step or the attempt's own transport failed) and
 * `invalid_output` are transient and retried.
 */
export type RenderErrorReason = 'parse_failed' | 'crashed' | 'invalid_output';

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
