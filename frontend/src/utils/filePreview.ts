/**
 * Which viewer opens a library file.
 *
 * One answer for every door — the menu entries, the inline button and "open" —
 * so they cannot disagree about what a file opens in.
 */

/** The picture types `GET /library/files/{id}/image` serves — every browser draws them in an `<img>`. */
const VIEWABLE_IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp']);

/** The types the 3D / G-code viewer takes. */
const MODEL_FILE_TYPES = new Set(['3mf', 'gcode', 'stl', 'obj']);

export type PreviewKind = 'image' | 'model' | 'pdf';

interface PreviewableFile {
  filename: string;
  file_type: string;
}

export function isViewableImage(file: Pick<PreviewableFile, 'filename'>): boolean {
  const dot = file.filename.lastIndexOf('.');
  return dot >= 0 && VIEWABLE_IMAGE_EXTENSIONS.has(file.filename.slice(dot + 1).toLowerCase());
}

/** A PDF — the server types a file by its extension, so `file_type` is the answer. */
export function isPdf(file: Pick<PreviewableFile, 'file_type'>): boolean {
  return file.file_type.toLowerCase() === 'pdf';
}

export function hasModelViewer(file: Pick<PreviewableFile, 'file_type'>): boolean {
  return MODEL_FILE_TYPES.has(file.file_type);
}

/** The viewer a file opens in, or `null` when nothing shows it. */
export function previewKindOf(file: PreviewableFile): PreviewKind | null {
  if (hasModelViewer(file)) return 'model';
  if (isViewableImage(file)) return 'image';
  if (isPdf(file)) return 'pdf';
  return null;
}
