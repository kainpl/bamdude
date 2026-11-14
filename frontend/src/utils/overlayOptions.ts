/**
 * The stream overlay's optional URL parameters, read in ONE place.
 *
 * The overlay page reads them off its own address; the builder in Settings
 * reads them off a pasted address and off the choices it remembers. All three
 * go through the validators here, so a value the page would ignore is never
 * one the builder accepts.
 *
 * ⚠️ The overlay is opened by OBS with a token and no session, from an address
 * somebody typed or pasted. A value that is not understood is IGNORED — never
 * an error, and never passed on to a style.
 */

/**
 * A colour as `#rrggbb`, or null.
 *
 * Six hex digits and nothing else, with or without the hash (a bare `#` in an
 * address starts the fragment, so hand-written addresses leave it out). The
 * result goes into a style, which is why no other CSS colour form is accepted.
 */
export function overlayColour(raw: string | null | undefined): string | null {
  if (!raw) return null;
  const match = /^#?([0-9a-f]{6})$/i.exec(raw);
  // `$` also matches before a trailing line break: compare the lengths.
  if (!match || match[0].length !== raw.length) return null;
  return `#${match[1].toLowerCase()}`;
}

/**
 * The fill of the progress bar for `progressFrom` / `progressTo`: a gradient
 * from left to right, one colour when only one of the two is valid, undefined
 * when neither is — the overlay then keeps its own colour.
 */
export function progressFill(
  from: string | null | undefined,
  to: string | null | undefined,
): string | undefined {
  const start = overlayColour(from);
  const end = overlayColour(to);
  if (!start && !end) return undefined;
  return `linear-gradient(to right, ${start ?? end}, ${end ?? start})`;
}

export type OverlayDesign = 'classic' | 'v2';

/** `design=2` selects Version 2; absent or anything else is Classic. */
export function overlayDesign(raw: string | null | undefined): OverlayDesign {
  return raw === '2' ? 'v2' : 'classic';
}

/**
 * `backgroundTransparency`: a whole number of percent, 0 (solid) … 100.
 * Anything else — a sign, a fraction, an exponent, a number out of range — is
 * ignored and the ground stays solid.
 */
export function overlayTransparency(raw: string | null | undefined): number {
  if (!raw || raw.length > 3) return 0;
  for (const char of raw) {
    if (char < '0' || char > '9') return 0;
  }
  const value = Number(raw);
  return value <= 100 ? value : 0;
}

export type OverlayLayout = 'landscape' | 'portrait';

/** The size each layout is drawn at before it is scaled to the browser source. */
export const OVERLAY_CANVAS: Record<OverlayLayout, { width: number; height: number }> = {
  landscape: { width: 1920, height: 1080 },
  portrait: { width: 1080, height: 1920 },
};

/**
 * `layout=landscape|portrait` draws the overlay on a fixed canvas. Null — no
 * parameter, or anything else — keeps the overlay sized by its window, as
 * every address from before the parameter expects.
 */
export function overlayLayout(raw: string | null | undefined): OverlayLayout | null {
  return raw === 'landscape' || raw === 'portrait' ? raw : null;
}

// ---------------------------------------------------------------------------
// The builder's choices: everything that shapes an overlay address EXCEPT the
// token. They are what the builder remembers in the browser and what it reads
// off a pasted address; both go through `overlayChoices`, which checks every
// value the way the overlay page itself would.
// ---------------------------------------------------------------------------

export type OverlaySize = 'small' | 'medium' | 'large';
export type BuilderDesign = 'classic' | '2';
export type BuilderLayout = 'auto' | OverlayLayout | 'both';

/** Every `?show=` item, in the overlay's own top-to-bottom order. */
export const OVERLAY_FIELDS = [
  'printer',
  'model',
  'filename',
  'status',
  'progress',
  'layers',
  'eta',
  'nozzle',
  'bed',
  'chamber',
] as const;

/** What an overlay shows when its address carries no `show` at all. */
export const DEFAULT_OVERLAY_FIELDS: string[] = ['progress', 'layers', 'eta', 'filename', 'status'];

export const DEFAULT_OVERLAY_FPS = 15;
export const DEFAULT_PROGRESS_FROM = '#00ae42';
export const DEFAULT_PROGRESS_TO = '#7ee081';

export interface OverlayChoices {
  printerId: number | null;
  fields: string[];
  size: OverlaySize;
  fps: number;
  showCamera: boolean;
  customColours: boolean;
  progressFrom: string;
  progressTo: string;
  design: BuilderDesign;
  transparency: number;
  layout: BuilderLayout;
}

export const DEFAULT_OVERLAY_CHOICES: OverlayChoices = {
  printerId: null,
  fields: DEFAULT_OVERLAY_FIELDS,
  size: 'medium',
  fps: DEFAULT_OVERLAY_FPS,
  showCamera: true,
  customColours: false,
  progressFrom: DEFAULT_PROGRESS_FROM,
  progressTo: DEFAULT_PROGRESS_TO,
  design: 'classic',
  transparency: 0,
  layout: 'auto',
};

function knownFields(values: unknown[]): string[] {
  return OVERLAY_FIELDS.filter((field) => values.includes(field));
}

/**
 * Choices read back from storage (or from anywhere else untrusted): each value
 * is kept only if it is one the builder could have produced, else it falls
 * back to its default. Never throws.
 */
export function overlayChoices(raw: unknown): OverlayChoices {
  if (!raw || typeof raw !== 'object') return DEFAULT_OVERLAY_CHOICES;
  const value = raw as Record<string, unknown>;
  const whole = (candidate: unknown, min: number, max: number, fallback: number) =>
    typeof candidate === 'number' && Number.isInteger(candidate) && candidate >= min && candidate <= max
      ? candidate
      : fallback;
  const from = typeof value.progressFrom === 'string' ? overlayColour(value.progressFrom) : null;
  const to = typeof value.progressTo === 'string' ? overlayColour(value.progressTo) : null;
  return {
    printerId: whole(value.printerId, 1, 999_999_999, 0) || null,
    fields: Array.isArray(value.fields) ? knownFields(value.fields) : DEFAULT_OVERLAY_FIELDS,
    size: value.size === 'small' || value.size === 'large' ? value.size : 'medium',
    fps: whole(value.fps, 1, 30, DEFAULT_OVERLAY_FPS),
    showCamera: value.showCamera !== false,
    customColours: value.customColours === true && from != null && to != null,
    progressFrom: from ?? DEFAULT_PROGRESS_FROM,
    progressTo: to ?? DEFAULT_PROGRESS_TO,
    design: value.design === '2' ? '2' : 'classic',
    transparency: whole(value.transparency, 0, 100, 0),
    layout: value.layout === 'landscape' || value.layout === 'portrait' || value.layout === 'both' ? value.layout : 'auto',
  };
}

/**
 * An overlay address somebody pasted → the choices it stands for and the token
 * it carries. Null when the text is not an overlay address.
 *
 * ⚠️ Only the path and the parameters are read. The ORIGIN IS DISCARDED: the
 * address may come from another server, and the builder must never fetch,
 * frame or link to it — whatever it builds points at this server. The token is
 * returned as the text it is; nothing here checks, stores or sends it.
 */
export function parseOverlayUrl(text: string): { choices: OverlayChoices; token: string } | null {
  let url: URL;
  try {
    // The base only lets a bare "/overlay/3?…" through; a pasted absolute
    // address ignores it.
    url = new URL(text.trim(), 'http://overlay.invalid');
  } catch {
    return null;
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
  if (url.username || url.password) return null;
  const path = /^\/overlay\/([1-9][0-9]{0,8})\/?$/.exec(url.pathname);
  if (!path) return null;
  const params = url.searchParams;
  const tokens = params.getAll('token');
  if (tokens.length > 1) return null;

  const show = params.get('show');
  const fpsParam = parseInt(params.get('fps') || '', 10);
  const camera = params.get('camera');
  const from = overlayColour(params.get('progressFrom'));
  const to = overlayColour(params.get('progressTo'));
  const size = params.get('size');
  const design = overlayDesign(params.get('design'));
  return {
    choices: {
      printerId: Number(path[1]),
      // As the page reads it: no `show` is the default set, an empty one is nothing.
      fields: show == null ? DEFAULT_OVERLAY_FIELDS : knownFields(show.split(',')),
      size: size === 'small' || size === 'large' ? size : 'medium',
      fps: Number.isNaN(fpsParam) ? DEFAULT_OVERLAY_FPS : Math.min(Math.max(fpsParam, 1), 30),
      showCamera: camera !== 'false' && camera !== '0',
      customColours: from != null || to != null,
      progressFrom: from ?? to ?? DEFAULT_PROGRESS_FROM,
      progressTo: to ?? from ?? DEFAULT_PROGRESS_TO,
      design: design === 'v2' ? '2' : 'classic',
      // Classic has no ground to fade: the builder would not emit it either.
      transparency: design === 'v2' ? overlayTransparency(params.get('backgroundTransparency')) : 0,
      layout: overlayLayout(params.get('layout')) ?? 'auto',
    },
    token: tokens[0] ?? '',
  };
}
