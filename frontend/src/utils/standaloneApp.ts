/**
 * Whether this page runs as a web app started from the iOS / iPadOS home
 * screen.
 *
 * There `window.open` does not open a window: it loads the target into the app
 * itself, which has no browser chrome — no back button, no address bar — so a
 * page without navigation of its own strands the user.
 *
 * ⚠️ `navigator.standalone` on purpose, not `display-mode: standalone`: the
 * media query is also true in an installed desktop or Android app, where a
 * second window does open and both camera modes keep working.
 */
export function isHomeScreenApp(): boolean {
  return typeof navigator !== 'undefined'
    && (navigator as Navigator & { standalone?: boolean }).standalone === true;
}
