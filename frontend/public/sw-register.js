// Registers the BamDude PWA service worker. Lives as a real JS file so the
// strict `script-src 'self'` CSP covers it without `'unsafe-inline'`.
//
// `register('/sw.js')` (absolute) — paired with the default Vite base ('/')
// in vite.config.ts so deep-route initial loads resolve assets against the
// host root regardless of document URL (no relative-URL MIME-blocked-module
// trap on /camera/<id> popups, deep-route refresh, etc — upstream Bambuddy
// #1221, our A.FE-revert).
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js')
      .then((registration) => {
        console.log('SW registered:', registration.scope);
      })
      .catch((error) => {
        console.log('SW registration failed:', error);
      });
  });
}
