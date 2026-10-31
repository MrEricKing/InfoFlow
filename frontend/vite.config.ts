import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import path from 'path';
import {defineConfig} from 'vite';

const defaultDevServerPort = 5173;

function parseDevServerPort(value: string | undefined): number {
  if (!value) {
    return defaultDevServerPort;
  }

  const parsed = Number.parseInt(value, 10);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) {
    throw new Error(`FRONTEND_PORT must be an integer between 1 and 65535. Received: ${value}`);
  }

  return parsed;
}

export default defineConfig({
  clearScreen: false,
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    host: process.env.FRONTEND_HOST || process.env.VITE_HOST || '0.0.0.0',
    port: parseDevServerPort(process.env.FRONTEND_PORT || process.env.VITE_PORT),
    strictPort: true,
  },
});
