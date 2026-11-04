export type JsonStorage = Pick<Storage, 'getItem' | 'setItem'> | null;

export function readJson(storage: JsonStorage, key: string): unknown {
  try {
    const stored = storage?.getItem(key);
    return stored == null ? null : JSON.parse(stored);
  } catch {
    return null;
  }
}

export function writeJson(storage: JsonStorage, key: string, value: unknown): void {
  storage?.setItem(key, JSON.stringify(value));
}
