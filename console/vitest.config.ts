import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

/**
 * The console's tests run in jsdom, because the failure they exist to catch is a render
 * that throws — and a render only throws in something that renders.
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: false,
    include: ['src/**/*.test.tsx'],
  },
});
