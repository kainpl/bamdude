/**
 * Utility for opening files in slicer applications
 *
 * Protocol handler URL formats (from BambuStudio/OrcaSlicer source code):
 *
 * Bambu Studio has TWO separate URL handlers:
 *   1. post_init() [Windows/Linux CLI args]: bambustudio://open?file=<URL>
 *      - Checks: starts_with("bambustudio://open")
 *      - Calls url_decode(), then split_str(url, "file=")
 *   2. MacOpenURL() [macOS Apple Events]: bambustudioopen://<encoded-URL>
 *      - Checks: starts_with("bambustudioopen://")
 *      - Strips prefix, then url_decode()
 *
 * OrcaSlicer Downloader accepts both formats via regex:
 *   - (orcaslicer|bambustudio|...)://open?file=<URL>
 *   - bambustudioopen://<URL>
 *
 * Key insight: every form needs encodeURIComponent on the file URL, because
 * the slicer calls url_decode() on the received query (post_init calls
 * url_decode then split_str; MacOpenURL strips the prefix then url_decode;
 * OrcaSlicer's Downloader regex-extracts then url_decode). Without encoding,
 * any already-percent-encoded character in the download URL (most commonly
 * %20 in filenames with spaces) decodes to a literal space and the slicer's
 * subsequent HTTP fetch fails with a 0-byte body or 404. See issue #1059.
 */

export type SlicerType = 'bambu_studio' | 'orcaslicer';

type Platform = 'windows' | 'macos' | 'linux' | 'unknown';

/**
 * Detect the user's operating system
 */
export function detectPlatform(): Platform {
  const userAgent = navigator.userAgent.toLowerCase();
  const platform = navigator.platform?.toLowerCase() || '';

  if (userAgent.includes('win') || platform.includes('win')) {
    return 'windows';
  }
  if (userAgent.includes('mac') || platform.includes('mac')) {
    return 'macos';
  }
  if (userAgent.includes('linux') || platform.includes('linux')) {
    return 'linux';
  }
  return 'unknown';
}

/**
 * Open a URL in the specified slicer application.
 * @param downloadUrl - The URL to the file to open
 * @param slicer - Which slicer to use (defaults to bambu_studio)
 */
export function openInSlicer(downloadUrl: string, slicer: SlicerType = 'bambu_studio'): void {
  let url: string;

  const encoded = encodeURIComponent(downloadUrl);
  if (slicer === 'orcaslicer') {
    url = `orcaslicer://open?file=${encoded}`;
  } else {
    const platform = detectPlatform();
    if (platform === 'macos') {
      // macOS only: bambustudioopen scheme via MacOpenURL() callback.
      url = `bambustudioopen://${encoded}`;
    } else {
      // Windows/Linux: bambustudio://open?file= via post_init() CLI args.
      // IMPORTANT: On Linux, BS only handles "bambustudio://open" prefix —
      // it does NOT process "bambustudioopen://" (that's macOS-only).
      url = `bambustudio://open?file=${encoded}`;
    }
  }

  // Use a temporary <a> element to trigger the protocol handler.
  // This avoids navigating away from the page (unlike window.location.href).
  const link = document.createElement('a');
  link.href = url;
  link.style.display = 'none';
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}

/**
 * The file types the slicer *sidecar* can slice.
 *
 * ⚠️ Narrower than what the DESKTOP slicers accept, by exactly STEP. The
 * desktop applications open a STEP happily; their command-line interfaces do
 * not — both OrcaSlicer and Bambu Studio answer one with "Unknown file format.
 * Input file must have .stl, .obj, .amf(.xml) extension."
 *
 * So a STEP still gets an "Open in Slicer" handoff, and no longer gets a
 * "Slice" button that could only ever fail — after reading, converting and
 * uploading the file first.
 *
 * Kept as its own predicate rather than a flag on a shared one so the two
 * questions cannot drift back together.
 */
export const API_SLICEABLE_FILE_TYPES = ['3mf', 'stl'] as const;

/** Does a `LibraryFile.file_type` name something the sidecar can slice? */
export function isApiSliceableFileType(fileType?: string | null): boolean {
  const normalized = (fileType || '').toLowerCase();
  return (API_SLICEABLE_FILE_TYPES as readonly string[]).includes(normalized);
}
