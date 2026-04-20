// Settings search registry.
//
// Each settings section registers itself at module-import time by calling
// `registerSettingsSearch(...)` at module scope (NOT inside a component).
// SettingsPage reads the accumulated registry to power its cross-tab search.
//
// Convention: co-locate the registration call with the component/section that
// owns the `anchor` id when possible. For tab-level entries that just switch
// to a tab, grouping them in SettingsPage itself is fine.
//
// Design note (BamDude divergence): upstream Bambuddy v0.2.3 registers one
// entry per settings Card with an `id="card-*"` anchor and scrollIntoView.
// We ship the same registry API but only register tab-level entries for
// now — the cards we didn't annotate can still be found by their tab, and
// individual cards can be added incrementally as keyword misses show up.

export type SettingsSearchTab =
  | 'general'
  | 'printing'
  | 'filament'
  | 'notifications'
  | 'plugs'
  | 'network'
  | 'virtual-printer'
  | 'apikeys'
  | 'users'
  | 'backup';

export type SettingsSearchSubTab = 'users' | 'email' | 'ldap';

export interface SettingsSearchEntry {
  /** i18n key for the label. Resolved with t() at render time. */
  labelKey: string;
  /** Fallback label if the i18n key is missing. */
  labelFallback?: string;
  tab: SettingsSearchTab;
  subTab?: SettingsSearchSubTab;
  /** Space-separated extra search terms (lowercase). */
  keywords: string;
  /** DOM id attached to the target card/tab — used for scrollIntoView. */
  anchor: string;
}

const entries = new Map<string, SettingsSearchEntry>();

export function registerSettingsSearch(entry: SettingsSearchEntry): void {
  entries.set(entry.anchor, entry);
}

export function getSettingsSearchEntries(): SettingsSearchEntry[] {
  return Array.from(entries.values());
}
