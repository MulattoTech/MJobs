import babelParser from '@babel/eslint-parser';

// TypeScript 7 remains the type authority. Babel's ESTree parser lets ESLint
// inspect TS/TSX syntax without forcing an unsupported typescript-eslint peer.
const correctnessRules = {
  'no-constant-binary-expression': 'error',
  'no-dupe-else-if': 'error',
  'no-duplicate-case': 'error',
  'no-eval': 'error',
  'no-implied-eval': 'error',
  'no-new-func': 'error',
  'no-self-compare': 'error',
  'no-sparse-arrays': 'error',
  'no-template-curly-in-string': 'error',
  'no-unreachable': 'error',
  'no-unsafe-finally': 'error',
  'use-isnan': 'error',
  'valid-typeof': 'error',
};

export default [
  { ignores: ['dist/**', 'node_modules/**', 'coverage/**'] },
  {
    files: ['src/**/*.{ts,tsx}', 'vite.config.ts'],
    languageOptions: {
      parser: babelParser,
      parserOptions: {
        requireConfigFile: false,
        babelOptions: {
          babelrc: false,
          configFile: false,
          parserOpts: { plugins: ['typescript', 'jsx'] },
        },
      },
    },
    linterOptions: { reportUnusedDisableDirectives: 'error' },
    rules: correctnessRules,
  },
  {
    files: ['scripts/**/*.mjs', '*.config.mjs'],
    rules: correctnessRules,
  },
];
