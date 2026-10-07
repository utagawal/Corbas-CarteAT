import { defineConfig } from 'vite';

// base « ./ » : le site fonctionne sous n'importe quel préfixe (ex. /ATCorbas/ derrière nginx).
const page = (f: string) => new URL(f, import.meta.url).pathname;

export default defineConfig({
  base: './',
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 1500,
    rollupOptions: {
      input: {
        index: page('./index.html'),
        admin: page('./admin.html'),
        mentions: page('./mentions-legales.html'),
        accessibilite: page('./accessibilite.html'),
      },
    },
  },
  server: {
    proxy: { '/api': 'http://127.0.0.1:8080' },
  },
});
