import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

// three.js belongs only in the scene chunk, which loads when the Range opens; any other chunk can load with every page.
const SCENE_CHUNK = /^createRangeScene-.+\.js$/;

export function checkBundle(assetsDir) {
  const chunks = readdirSync(assetsDir).filter((name) => name.endsWith('.js'));
  const problems = [];
  if (!chunks.some((name) => SCENE_CHUNK.test(name))) {
    problems.push('no createRangeScene-*.js chunk found: update scripts/check-bundle.mjs');
  }
  for (const chunk of chunks) {
    if (!SCENE_CHUNK.test(chunk) && readFileSync(join(assetsDir, chunk), 'utf8').includes('WebGLRenderer')) {
      problems.push(`${chunk} contains three.js (WebGLRenderer): keep it in the lazily loaded scene chunk`);
    }
  }
  return problems;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const problems = checkBundle(fileURLToPath(new URL('../dist/assets/', import.meta.url)));
  if (problems.length > 0) {
    console.error(`bundle check failed:\n${problems.join('\n')}`);
    process.exit(1);
  }
  console.log('bundle check: three.js only in the scene chunk');
}
