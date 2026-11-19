// The authorisation context of the preset catalogue and the farm read (spec
// slicing-setup R10): one monotonic generation, bumped whenever the observed
// auth tuple (user, rights, cloud link states) changes and after a CONFIRMED
// cloud binding change. Query keys carry `g<generation>`, so a list read under
// one context is never shown, and never written, under another — rights going
// A → B → A give three keys, not two.
//
// No React and no imports here: the API client bumps it.

interface PresetContextState {
  generation: number;
  /** The auth tuple the generation belongs to; null before the first one is noted. */
  tuple: string | null;
  /** Counts binding bumps only, so the status queries can be refreshed after one. */
  binding: number;
}

let state: PresetContextState = { generation: 0, tuple: null, binding: 0 };
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

export function presetContextSnapshot(): PresetContextState {
  return state;
}

export function presetContextGeneration(): number {
  return state.generation;
}

export function subscribePresetContext(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** A confirmed cloud binding change (login, verify, token, logout, Orca connected / logout). */
export function bumpPresetContext(): void {
  state = { generation: state.generation + 1, tuple: state.tuple, binding: state.binding + 1 };
  emit();
}

/** Note the auth tuple a render saw; a new one starts a new generation. */
export function notePresetAuthTuple(tuple: string): void {
  if (state.tuple === tuple) return;
  state = { generation: state.generation + 1, tuple, binding: state.binding };
  emit();
}

/** Tests only: start from a clean state. */
export function __resetPresetContextForTests(): void {
  state = { generation: 0, tuple: null, binding: 0 };
  emit();
}
