import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * The local API is protected by a bearer token stored in storage/runtime.json.
 * For the production build the backend injects the token into index.html.
 * In dev we embed it at server start so `npm run dev` can talk to the API directly.
 */
function readDevToken(): string {
  try {
    const path = fileURLToPath(new URL('../storage/runtime.json', import.meta.url))
    return JSON.parse(readFileSync(path, 'utf8')).api_token ?? ''
  } catch {
    return ''
  }
}

export default defineConfig({
  plugins: [react()],
  define: {
    __DAAS_TOKEN__: JSON.stringify(readDevToken()),
  },
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
  },
})
