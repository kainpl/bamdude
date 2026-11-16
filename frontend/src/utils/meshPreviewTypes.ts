/**
 * Which library files get which thumbnail path — by `file_type`, never by a display name (spec §3).
 * STL/OBJ render at once through the batch route; STEP/STP only in the background, through receipts.
 */

export const LEGACY_THUMBNAIL_TYPES = ['stl', 'obj'] as const;
export const CAD_THUMBNAIL_TYPES = ['step', 'stp'] as const;
/** A document's first page — rendered at once, like a mesh, but a group of its own: it is no mesh. */
export const PDF_THUMBNAIL_TYPES = ['pdf'] as const;

const lower = (value: string | null | undefined) => (value ?? '').toLowerCase();

export function isLegacyThumbnailType(fileType: string | null | undefined): boolean {
  return (LEGACY_THUMBNAIL_TYPES as readonly string[]).includes(lower(fileType));
}

export function isCadThumbnailType(fileType: string | null | undefined): boolean {
  return (CAD_THUMBNAIL_TYPES as readonly string[]).includes(lower(fileType));
}

export function isPdfThumbnailType(fileType: string | null | undefined): boolean {
  return (PDF_THUMBNAIL_TYPES as readonly string[]).includes(lower(fileType));
}

/** Whether "Generate thumbnail" is offered for a file of this type at all. */
export function offersThumbnail(fileType: string | null | undefined): boolean {
  return isLegacyThumbnailType(fileType) || isCadThumbnailType(fileType) || isPdfThumbnailType(fileType);
}

/**
 * An upload the dialog offers "generate thumbnails" for, by its name: a mesh, a CAD model or a PDF.
 * The server decides the type; this only chooses whether the dialog shows the checkbox.
 */
export function isThumbnailUploadName(name: string): boolean {
  return offersThumbnail(lower(name.split('.').pop()));
}
