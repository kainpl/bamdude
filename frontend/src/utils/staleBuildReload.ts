/**
 * A tab left open across an update asks for a file the new build no longer has.
 *
 * The bundle is served in pieces that load when first needed (the app shell, the slicer panel's
 * data, the PDF runtime). After an update their names change, and a tab still running the old
 * build gets a 404 for the piece it asks for next — a panel that never opens, with nothing on
 * screen to say why. Vite reports that as `vite:preloadError`; one reload brings the tab onto
 * the build the server has.
 *
 * Once per minute at most: when the reload did not help (the file is really gone, the server is
 * down), the error is left to surface instead of looping the page.
 */
const KEY = 'bamdude-stale-build-reload';
const QUIET_MS = 60_000;

export function reloadOnceForStaleBuild(
  reload: () => void = () => window.location.reload(),
  now: () => number = Date.now,
): boolean {
  try {
    const last = Number(window.sessionStorage.getItem(KEY));
    if (Number.isFinite(last) && last > 0 && now() - last < QUIET_MS) return false;
    window.sessionStorage.setItem(KEY, String(now()));
  } catch {
    // No session storage: no way to tell a loop from a first try, so do not reload.
    return false;
  }
  reload();
  return true;
}

export function installStaleBuildReload(): void {
  window.addEventListener('vite:preloadError', (event) => {
    // Handled here: without this Vite rethrows it as an unhandled rejection.
    if (reloadOnceForStaleBuild()) event.preventDefault();
  });
}
