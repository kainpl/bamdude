import {defineConfig} from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

// Backend port for dev server proxy (default: 8000)
const backendPort = process.env.BACKEND_PORT || '8000'
const backendUrl = `http://localhost:${backendPort}`

export default defineConfig({
    // Default base ('/') emits absolute asset URLs (/assets/..., /manifest.json,
    // /sw-register.js). Reverted from the previous `base: ''` (relative URLs)
    // because relative asset paths broke deep-route initial-load on every
    // browser: opening /camera/<id> popped a popup whose document URL had no
    // trailing slash, so `./assets/index-XXX.js` resolved against /camera/ as
    // the directory and /camera/ → SPA catch-all returned index.html
    // (text/html). Modern browsers refuse to execute HTML as a JS module
    // under nosniff, so the popup loaded but the bundle never did. Same
    // break hit any deep route on direct URL paste / refresh (/projects/:id,
    // /groups/:id/edit, /files/trash, /external/:id). Path-prefixed reverse
    // proxy users (Traefik / nginx subpath / Cloudflare Tunnel path routing)
    // who motivated `base: ''` have a working alternative: NPM addon +
    // Cloudflare Tunnel at a real domain + HA Webpage panel embedding via
    // TRUSTED_FRAME_ORIGINS — that path doesn't depend on `base` at all.
    // (Upstream Bambuddy #1221 reverts PR #1195.)
    plugins: [react()],
    build: {
        outDir: '../static',
        emptyOutDir: true,
        chunkSizeWarningLimit: 3000,
        rollupOptions: {
            output: {
                manualChunks(id) {
                    if (id.includes('node_modules/three/') ||
                        id.includes('gcode-preview'))
                        return 'vendor-three'
                    if (id.includes('node_modules/recharts/') ||
                        id.includes('node_modules/d3'))
                        return 'vendor-charts'
                    if (id.includes('@tiptap') || id.includes('prosemirror'))
                        return 'vendor-editor'
                    if (id.includes('node_modules/react/') ||
                        id.includes('node_modules/react-dom/') ||
                        id.includes('node_modules/react-router'))
                        return 'vendor-react'
                    if (id.includes('node_modules/react-i18next/') ||
                        id.includes('/i18n/locales/'))
                        return 'locales'
                },
            },
        },
    },
    server: {
        host: '0.0.0.0',
        headers: {
            'Cache-Control': 'no-store',
        },
        proxy: {
            '/api/v1/ws': {
                target: backendUrl,
                ws: true,
                changeOrigin: true,
            },
            '/api': {
                target: backendUrl,
                changeOrigin: true,
            },
            '/openapi.json': {
                target: backendUrl,
                changeOrigin: true,
            },
            '/docs': {
                target: backendUrl,
                changeOrigin: true,
            },
        },
    },
    resolve: {
        alias: {
            '@': path.resolve(__dirname, './src'),
        },
    },
})
