// Registers the BamDude PWA service worker. Lives as a real JS file so the
// strict `script-src 'self'` CSP covers it without `'unsafe-inline'`.
//
// `register('sw.js')` (relative) — paired with `base: ''` in vite.config.ts so
// the entire built SPA loads correctly under any subpath (#1195). The
// service-worker URL is resolved relative to the document URL, so a SPA served
// at `/bamdude/` registers `/bamdude/sw.js` and the SW scope auto-pins to that
// subpath without per-deploy configuration.
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('sw.js')
      .then((registration) => {
        console.log('SW registered:', registration.scope);
      })
      .catch((error) => {
        console.log('SW registration failed:', error);
      });
  });
}
