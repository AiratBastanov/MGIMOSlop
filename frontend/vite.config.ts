import { defineConfig } from 'vite';

export default defineConfig({
  base: '/static/frontend/',
  build: {
    outDir: '../static/frontend', emptyOutDir: true, manifest: 'manifest.json',
    target: 'es2022', sourcemap: false,
    rollupOptions: { input: 'src/main.tsx' },
  },
});
