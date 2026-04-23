// Registers the BamDude PWA service worker. Lives as a real JS file so the
// strict `script-src 'self'` CSP covers it without `'unsafe-inline'`.
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
