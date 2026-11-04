import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { checkBundle } from '../scripts/check-bundle.mjs';

describe('checkBundle', () => {
  let assets: string;

  beforeEach(() => {
    assets = mkdtempSync(join(tmpdir(), 'bundle-check-'));
  });

  afterEach(() => {
    rmSync(assets, { recursive: true, force: true });
  });

  const chunk = (name: string, code: string) => writeFileSync(join(assets, name), code);

  /** A good build: index loads App, which loads the scene chunk, the only one with three.js, when the Range opens. */
  const goodBuild = () => {
    chunk('index-Cjlyf9xW.js', 'import("./App-B9e4C24I.js")');
    chunk('App-B9e4C24I.js', 'import("./createRangeScene-CWPDvcLF.js")');
    chunk('clubs-D4kq1pXa.js', 'export const clubs = []');
    chunk('createRangeScene-CWPDvcLF.js', 'new WebGLRenderer()');
  };

  it('passes while three.js stays in the scene chunk', () => {
    goodBuild();

    expect(checkBundle(assets)).toEqual([]);
  });

  it.each(['index-Cjlyf9xW.js', 'App-B9e4C24I.js', 'clubs-D4kq1pXa.js'])(
    'names %s when three.js leaks into it',
    (name) => {
      goodBuild();
      chunk(name, 'new WebGLRenderer()');

      expect(checkBundle(assets)).toEqual([expect.stringContaining(name)]);
    }
  );

  it('fails without a scene chunk, so a rename cannot pass it by default', () => {
    goodBuild();
    rmSync(join(assets, 'createRangeScene-CWPDvcLF.js'));

    expect(checkBundle(assets)).toEqual([expect.stringContaining('createRangeScene-')]);
  });
});
