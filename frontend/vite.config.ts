import {defineConfig} from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

// Backend port for dev server proxy (default: 8000)
const backendPort = process.env.BACKEND_PORT || '8000'
const backendUrl = `http://localhost:${backendPort}`

export default defineConfig({
    // Empty base emits relative asset URLs (./assets/..., ./manifest.json,
    // ./img/..., ./sw-register.js) instead of host-rooted ones, so the built
    // SPA loads correctly when served at any subpath — Traefik / nginx path
    // prefix / Cloudflare Tunnel path routing / HA Webpage panel iframes
    // (#1195). Backend /assets and /img mounts in main.py keep working because
    // the proxy strips the prefix before forwarding, so the backend always
    // sees unprefixed paths.
    base: '',
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
            // Vendored PrettyGCode viewer (B.8) — proxied to uvicorn so the
            // same FastAPI static-serve route handles both dev + prod with
            // no duplicate file-serving logic in this config.
            '/gcode-viewer': {
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
