import { compareCurrentJobEta, compareFreeAt } from '../../utils/etaSort';
import type { PrinterForecast } from '../../api/client';
import { tileState } from './state';
import type { MonitorConfig, MonitorPrinter } from './types';

export function sortMonitor(printers: MonitorPrinter[], config: MonitorConfig, forecast?: Map<number, PrinterForecast>) {
  const collator = new Intl.Collator(config.lang, { numeric: true, sensitivity: 'base' });
  const completeForecast = forecast && printers.every(p => forecast.has(p.printer_id));
  const eta = (p: MonitorPrinter) => ({ connected: p.connected, state: p.state,
    remaining_time: p.remaining_seconds == null ? null : p.remaining_seconds / 60 });
  return [...printers].sort((a, b) => {
    let result = 0;
    if (config.sort === 'attention') {
      const sa = tileState(a, config.view), sb = tileState(b, config.view);
      result = sa.rank - sb.rank;
      if (!result && sa.attention) result = sa.since === sb.since ? 0 : sa.since < sb.since ? -1 : 1;
      if (!result && sa.rank === 4) result = (a.remaining_seconds ?? 0) - (b.remaining_seconds ?? 0);
    } else if (config.sort === 'eta') result = compareCurrentJobEta(eta(a), eta(b));
    else if (config.sort === 'freeAt' && completeForecast && forecast) {
      result = compareFreeAt(forecast.get(a.printer_id), forecast.get(b.printer_id), eta(a), eta(b));
    }
    return result || collator.compare(a.name, b.name) || a.printer_id - b.printer_id;
  });
}

export function groupMonitor(printers: MonitorPrinter[], config: MonitorConfig): { key: string; printers: MonitorPrinter[] }[] {
  if (config.group === 'none' || config.attention) return [{ key: '', printers }];
  const groups = new Map<string, MonitorPrinter[]>();
  for (const printer of printers) {
    const keys = config.group === 'location' ? [printer.location || ''] : printer.tags.length ? printer.tags : [''];
    for (const key of keys) groups.set(key, [...(groups.get(key) || []), printer]);
  }
  return [...groups].sort(([a], [b]) => !a ? 1 : !b ? -1 : a.localeCompare(b, config.lang))
    .map(([key, rows]) => ({ key, printers: rows }));
}
