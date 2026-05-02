// Tag → visual style table for ``LibraryFile.file_tags`` (m036). Both
// the FileTagBadges component (renders the pills) and the FileManager
// chip-row filter (paints the toggle buttons in the same colours) read
// from this single map so labels and colours stay consistent.
//
// Adding a new tag: add an entry here, add a label key under
// ``library.tags.{tag}`` in en.ts + uk.ts, and add the tag emission
// rule on the backend in ``compute_file_tags`` plus a backfill in the
// next migration.

export type TagStyle = {
  label: string;
  // Background + text classes for the pill, matching FileManager
  // colour conventions.
  bg: string;
  text: string;
};

export const TAG_STYLES: Record<string, TagStyle> = {
  // Format chips — match the file extension. Sliced 3MFs carry both
  // ``gcode`` + ``3mf`` so the green ``3MF`` chip restores the "this is
  // a sliced container" cue the m035 file_type collapse erased.
  '3mf': { label: '3MF', bg: 'bg-bambu-green/90', text: 'text-white' },
  gcode: { label: 'GCODE', bg: 'bg-blue-500/90', text: 'text-white' },
  stl: { label: 'STL', bg: 'bg-purple-500/90', text: 'text-white' },
  obj: { label: 'OBJ', bg: 'bg-fuchsia-500/90', text: 'text-white' },
  step: { label: 'STEP', bg: 'bg-bambu-gray/90', text: 'text-white' },
  // Semantic-group chips (m037) co-emitted alongside the format chip:
  // ``project`` for unsliced ``.3mf`` packages, ``geometry`` for raw mesh
  // / CAD source. They overlap the format chips on purpose — the format
  // pill answers "what extension" and the semantic pill answers "is it
  // ready to print", so both are useful filters.
  project: { label: 'PROJ', bg: 'bg-blue-700/80', text: 'text-white' },
  geometry: { label: 'GEO', bg: 'bg-purple-700/80', text: 'text-white' },
  multiplate: { label: 'MP', bg: 'bg-cyan-500/90', text: 'text-white' },
  swap: { label: 'SWAP', bg: 'bg-amber-500/90', text: 'text-white' },
  sliced: { label: 'SLICED', bg: 'bg-cyan-700/80', text: 'text-white' },
  makerworld: { label: 'MW', bg: 'bg-orange-500/90', text: 'text-white' },
};

// Unknown tags fall back to a neutral gray pill — keeps forward-compat
// when the backend starts emitting a tag the frontend hasn't shipped
// styling for yet (no broken layout, just an unstyled pill the dev can
// notice and add a TAG_STYLES entry for).
export const UNKNOWN_TAG_BG = 'bg-bambu-gray/70';
export const UNKNOWN_TAG_TEXT = 'text-white';

export const KNOWN_FILE_TAGS = Object.keys(TAG_STYLES);

export function getTagStyle(tag: string): TagStyle | null {
  return TAG_STYLES[tag] ?? null;
}

// Display order for the badge row. Read right-to-left, the row goes
// from most-specific (the file format chip anchors the right edge)
// outward to broadest context (provenance leftmost). The backend emits
// tags in semantic-emission order; ``sortTagsForDisplay`` re-projects
// onto this precedence so the row is consistent regardless of who wrote
// the row (upload vs slicer-output vs MakerWorld import) and so a future
// reordering is a one-file frontend change instead of a backend +
// migration cycle.
const TAG_DISPLAY_ORDER: readonly string[] = [
  // Provenance — leftmost
  'makerworld',
  // Modifiers — multi-plate first (further left), swap closer to the chip cluster
  'multiplate',
  'swap',
  // Readiness / state — mutually exclusive in practice (sliced wins over project/geometry)
  'sliced',
  'project',
  'geometry',
  // Format chips — rightmost; sliced .gcode.3mf composite is gcode then 3mf
  'gcode',
  '3mf',
  'stl',
  'obj',
  'step',
];
const TAG_RANK = new Map<string, number>(
  TAG_DISPLAY_ORDER.map((tag, idx) => [tag, idx]),
);

/**
 * Returns a copy of ``tags`` sorted by the display precedence above.
 * Unknown tags (no entry in ``TAG_DISPLAY_ORDER``) fall to the right
 * end and are sub-sorted alphabetically so the position is deterministic
 * across re-renders (avoids the React key churn pitfall when the array
 * order changes between renders).
 */
export function sortTagsForDisplay(tags: string[] | null | undefined): string[] {
  if (!tags || tags.length === 0) return [];
  return [...tags].sort((a, b) => {
    const ra = TAG_RANK.get(a) ?? Number.MAX_SAFE_INTEGER;
    const rb = TAG_RANK.get(b) ?? Number.MAX_SAFE_INTEGER;
    if (ra !== rb) return ra - rb;
    return a.localeCompare(b);
  });
}

// Predicate helpers — read from ``file_tags`` so the same question
// asked from FileCard / list-row / project detail / bulk-action handler
// resolves through a single source. Pre-m036 callers used three
// different shapes for the same question (``filename.endsWith('.gcode')``
// OR ``file_type === 'gcode'`` OR ``is_multi_plate``); the tag list
// makes them uniform and the backend write-side guarantees the tags
// stay consistent with the underlying flags.
//
// All helpers accept a sparse object so they can be used with both the
// full ``LibraryFile`` shape and the lighter ``LibraryFileListItem`` —
// both carry ``file_tags``.

export function hasTag(tags: string[] | null | undefined, tag: string): boolean {
  return Array.isArray(tags) && tags.includes(tag);
}

// Sliced file — has the ``gcode`` tag (raw .gcode OR .gcode.3mf).
export function isSliced(file: { file_tags?: string[] | null }): boolean {
  return hasTag(file.file_tags, 'gcode');
}

// Sliceable model — has a semantic-group tag the slicer can consume
// AND is NOT already sliced. Post-m037 the relevant tags are ``project``
// (unsliced .3mf) and ``geometry`` (STL / OBJ / STEP / STP). The
// ``isSliced`` short-circuit handles sliced .gcode.3mf (carries both
// ``gcode`` + ``3mf`` — bailing here keeps the predicate symmetric).
export function isSliceable(file: { file_tags?: string[] | null }): boolean {
  if (isSliced(file)) return false;
  return hasTag(file.file_tags, 'project') || hasTag(file.file_tags, 'geometry');
}

// Multi-plate 3MF (sliced or unsliced). Replaces the standalone
// ``is_multi_plate`` column read at call sites where the tag list is
// already in scope — saves carrying the boolean separately.
export function isMultiPlate(file: { file_tags?: string[] | null }): boolean {
  return hasTag(file.file_tags, 'multiplate');
}
