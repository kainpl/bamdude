// The remembered filter values of the slicing-setup family (spec r5 §10): one
// in-tab store per key, so every mounted consumer shows one value, written
// through to localStorage when it can be. Storage that throws (a private
// window, a quota) leaves the store working in memory.
import { useCallback, useSyncExternalStore } from 'react';

export const SOURCE_FILTER_KEY = 'bamdude:presets:source-filter';
export const FARM_MODELS_KEY = 'bamdude:presets:only-farm-models';
/** The key "Only printers that are online" used; read once to seed the farm-models switch. */
export const LEGACY_ONLINE_KEY = 'bamdude:slice-modal:only-online-printers';
export const LOADED_KEY = 'bamdude:slice-modal:only-loaded-filaments';

interface Store {
  value: string | null;
  listeners: Set<() => void>;
}

const stores = new Map<string, Store>();

function read(key: string): string | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string): void {
  try {
    if (typeof localStorage !== 'undefined') localStorage.setItem(key, value);
  } catch {
    /* private mode / quota — the value still lives in this tab */
  }
}

function storeFor(key: string, initial: () => string | null): Store {
  let store = stores.get(key);
  if (!store) {
    store = { value: initial(), listeners: new Set() };
    stores.set(key, store);
  }
  return store;
}

function useStoredValue(key: string, initial: () => string | null): [string | null, (v: string) => void] {
  const store = storeFor(key, initial);
  const subscribe = useCallback(
    (listener: () => void) => {
      store.listeners.add(listener);
      return () => {
        store.listeners.delete(listener);
      };
    },
    [store],
  );
  const value = useSyncExternalStore(subscribe, () => store.value);
  const set = useCallback(
    (next: string) => {
      store.value = next;
      write(key, next);
      for (const listener of store.listeners) listener();
    },
    [key, store],
  );
  return [value, set];
}

/**
 * A switch remembered per browser as `'1'` / `'0'`. With `seedFrom`, a key
 * that was never written takes the legacy key's value once (`'1'` → on,
 * anything else → off) and is written, so an explicit new `'0'` always wins.
 */
export function useSharedFlag(key: string, seedFrom?: string): [boolean, (on: boolean) => void] {
  const [value, set] = useStoredValue(key, () => {
    const own = read(key);
    if (own !== null || !seedFrom) return own;
    const seeded = read(seedFrom) === '1' ? '1' : '0';
    write(key, seeded);
    return seeded;
  });
  const setFlag = useCallback((on: boolean) => set(on ? '1' : '0'), [set]);
  return [value === '1', setFlag];
}

/** One of `allowed`, remembered per browser; anything else reads as `fallback`. */
export function useSharedChoice<T extends string>(key: string, allowed: readonly T[], fallback: T): [T, (v: T) => void] {
  const [value, set] = useStoredValue(key, () => read(key));
  const choice = (allowed as readonly string[]).includes(value ?? '') ? (value as T) : fallback;
  return [choice, set as (v: T) => void];
}

/** Tests only: forget every store. */
export function __resetSharedMemoryForTests(): void {
  stores.clear();
}
