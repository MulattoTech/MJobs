import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  test: {
    coverage: {
      enabled: true,
      provider: 'v8',
      include: [
        'src/ai-provider-settings.tsx',
        'src/ai-prompt-versioning.tsx',
      ],
      exclude: [
        '**/node_modules/**',
        '**/dist/**',
        '**/build/**',
        '**/generated/**',
        '**/private/**',
        '**/*.test.*',
      ],
      reporter: ['json-summary', 'text-summary'],
      reportsDirectory: 'coverage',
    },
  },
});
