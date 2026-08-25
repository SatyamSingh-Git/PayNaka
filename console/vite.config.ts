import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // The console talks to PayNaka on :8002. Proxying in dev keeps the browser
      // origin single, so nothing here depends on the CORS allowance holding.
      '/api': { target: 'http://127.0.0.1:8002', changeOrigin: true },
      '/sse': { target: 'http://127.0.0.1:8002', changeOrigin: true },
      '/mcp': { target: 'http://127.0.0.1:8002', changeOrigin: true },
    },
  },
  build: {
    rollupOptions: {
      output: {
        // One 1 MB chunk meant every visitor downloaded the design system, the animation
        // library and the app before anything appeared. Splitting on the boundaries that
        // actually differ in cache lifetime: Blade and React change when we upgrade them,
        // our own code changes every commit.
        manualChunks: {
          react: ['react', 'react-dom', 'styled-components'],
          blade: ['@razorpay/blade/components', '@razorpay/blade/tokens'],
          motion: ['framer-motion'],
        },
      },
    },
  },
  resolve: {
    // Blade ships one package for web and native. Without this, bundling can pick a
    // .native entrypoint and fail on a react-native import that has no business here.
    extensions: ['.web.tsx', '.web.ts', '.tsx', '.ts', '.jsx', '.js', '.json'],
  },
});
