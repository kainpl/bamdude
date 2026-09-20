import type { MonitorConfig, MonitorSort, MonitorView } from './types';

export function isMonitorKioskLocation(): boolean {
  return typeof window !== 'undefined' && window.location.pathname === '/monitor' &&
    new URLSearchParams(window.location.hash.slice(1)).has('token');
}

export function readMonitorToken(): string | null {
  return new URLSearchParams(window.location.hash.slice(1)).get('token');
}

function choice<T extends string>(value: string | undefined | null, choices: readonly T[], fallback: T): T {
  return choices.includes(value as T) ? value as T : fallback;
}

export function readMonitorConfig(url: URL, saved: Partial<MonitorConfig> = {}): MonitorConfig {
  const p = url.searchParams;
  const get = (key: keyof MonitorConfig) => p.has(key) ? p.get(key) : String(saved[key] ?? '');
  return {
    view: choice(get('view'), ['printers', 'queues'], 'printers'),
    sort: choice(get('sort'), ['attention', 'eta', 'freeAt', 'name'], 'attention'),
    group: choice(get('group'), ['none', 'location', 'tag'], 'none'),
    size: choice(get('size'), ['auto', '1', '2', '3', '4'], 'auto'),
    color: choice(get('color'), ['strong', 'soft'], 'strong'),
    lang: choice(get('lang'), ['uk', 'en'], 'uk'),
    search: get('search') || '', attention: get('attention') === 'true',
  };
}

export function monitorUrl(view: MonitorView, sort?: string, token?: string): string {
  const p = new URLSearchParams({ view });
  if (sort === 'location' || sort === 'tag') p.set('group', sort);
  else if (['attention', 'eta', 'freeAt', 'name'].includes(sort || '')) p.set('sort', sort as MonitorSort);
  return `/monitor?${p}${token === undefined ? '' : `#token=${encodeURIComponent(token)}`}`;
}
