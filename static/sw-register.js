// Registers the BamDude PWA service worker. Lives as a real JS file so the
// strict `script-src 'self'` CSP covers it without `'unsafe-inline'`.
//
// `register('/sw.js')` (absolute) — paired with the default Vite base ('/')
// in vite.config.ts so deep-route initial loads resolve assets against the
// host root regardless of document URL (no relative-URL MIME-blocked-module
// trap on /camera/<id> popups, deep-route refresh, etc — upstream Bambuddy
// #1221, our A.FE-revert).
if ('serviceWorker' in navigator) {
  // Capture controller state at script-load. Our sw.js does skipWaiting() +
  // clients.claim(), so a fresh deploy activates and takes over an open tab
  // immediately — but the document keeps running the previously-cached bundle
  // until something reloads it. This gated controllerchange listener does that
  // reload, but ONLY when the page already had a SW controller at load time
  // (hadController). That gate distinguishes an upgrade-on-existing-client
  // (reload wanted — pick up the new bundle) from a first install (no prior
  // controller — a reload here would race the in-flight React mount and can
  // wedge the page on a spinner). #1... — upstream a104ccc9.
  const hadController = !!navigator.serviceWorker.controller;
  let reloading = false;
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (!hadController || reloading) return;
    reloading = true;
    location.reload();
  });

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
