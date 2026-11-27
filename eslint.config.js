import velis from 'eslint-config-velis';
import globals from 'globals';

export default [
  ...velis,
  {
    ignores: ['.venv/*', 'dist/*', 'node_modules/*'],
  },
  {
    // The velis preset covers `.js`, `.ts` and `.vue`; the lint script is Node ESM.
    files: ['scripts/**/*.mjs'],
    languageOptions: {
      parserOptions: { project: './tsconfig.json' },
      globals: { ...globals.node },
    },
  },
];
