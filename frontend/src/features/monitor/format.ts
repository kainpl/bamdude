const unitFormats = new Map<string, Intl.NumberFormat>();
const clockFormats = new Map<string, Intl.DateTimeFormat>();

function unit(value: number, name: 'minute' | 'hour', lang: string): string {
  const key = `${lang}:${name}`;
  if (!unitFormats.has(key)) unitFormats.set(key, new Intl.NumberFormat(lang, { style: 'unit', unit: name, unitDisplay: 'short' }));
  return unitFormats.get(key)!.format(value);
}

export function clockTime(timestamp: number, lang: string): string {
  if (!clockFormats.has(lang)) clockFormats.set(lang, new Intl.DateTimeFormat(lang, { hour: '2-digit', minute: '2-digit' }));
  return clockFormats.get(lang)!.format(timestamp);
}

export function duration(seconds: number | null | undefined, lang: string, compact = false): string {
  if (seconds == null || !Number.isFinite(seconds)) return '—';
  const minutes = Math.max(0, Math.ceil(seconds / 60));
  if (minutes < 60) return unit(minutes, 'minute', lang);
  if (compact) return `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, '0')}`;
  return [unit(Math.floor(minutes / 60), 'hour', lang), minutes % 60 ? unit(minutes % 60, 'minute', lang) : ''].filter(Boolean).join(' ');
}
