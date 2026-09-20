import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, Expand, ListOrdered, Monitor, Printer, RefreshCw, Search, Sun, Wifi, WifiOff } from 'lucide-react';
import { Button } from '../components/Button';
import { CardSizeSwitch } from '../components/CardSizeSwitch';
import { FilterDropdown } from '../components/FilterDropdown';
import { useTheme } from '../contexts/ThemeContext';
import { useToast } from '../contexts/ToastContext';
import { ApiError } from '../api/client';
import { readMonitorConfig, readMonitorToken } from '../features/monitor/location';
import { useMonitorForecast, useMonitorSnapshot } from '../features/monitor/useMonitorSnapshot';
import { MonitorTile } from '../features/monitor/MonitorTile';
import { MonitorDetails } from '../features/monitor/MonitorDetails';
import { ElementVirtualGrid } from '../components/ElementVirtualGrid';
import { useMountedPrinterPriority } from '../hooks/useMountedPrinterPriority';
import { groupMonitor, sortMonitor } from '../features/monitor/sort';
import { tileState } from '../features/monitor/state';
import type { MonitorConfig, MonitorPrinter } from '../features/monitor/types';
import '../features/monitor/monitor.css';

const SETTINGS = 'bamdude-monitor-v1';
function initialConfig(): MonitorConfig {
  let saved = {};
  try { if (readMonitorToken() === null) saved = JSON.parse(localStorage.getItem(SETTINGS) || '{}'); } catch { /* defaults */ }
  return readMonitorConfig(new URL(window.location.href), saved);
}

export default function MonitorPage() {
  useMountedPrinterPriority('monitor');
  const { t, i18n } = useTranslation();
  const { resolvedMode, setMode } = useTheme();
  const { showToast } = useToast();
  const [config, setConfig] = useState(initialConfig);
  const [token, setToken] = useState(readMonitorToken);
  const [now, setNow] = useState(Date.now);
  const [selected, setSelected] = useState<number | null>(null);
  const [frozen, setFrozen] = useState(false);
  const [order, setOrder] = useState<number[]>([]);
  const [viewport, setViewport] = useState({ width: window.innerWidth - 48, height: window.innerHeight - 220 });
  const [visibleCount, setVisibleCount] = useState(0);
  const content = useRef<HTMLElement>(null);
  const [scrollElement, setScrollElement] = useState<HTMLElement | null>(null);
  const contentRef = useCallback((node: HTMLElement | null) => {
    content.current = node;
    setScrollElement(node);
  }, []);
  const lastOrder = useRef({ at: 0, priority: '', config: '' });
  const beforeAttention = useRef({ search: config.search, group: config.group });
  const data = useMonitorSnapshot(config.view, token);
  const forecast = useMonitorForecast(token, !data.terminal && Boolean(data.data?.capabilities.forecast) && (config.view === 'queues' || config.sort === 'freeAt'));
  const stale = data.updatedAt > 0 && now - data.updatedAt >= 15000;
  const forecastStale = !forecast.data || Boolean(forecast.error) || now - forecast.updatedAt > 60000;
  const forecasts = useMemo(() => forecast.data ? new Map(forecast.data.printers.map(p => [p.printer_id, p])) : undefined, [forecast.data]);
  const printers = useMemo(() => data.data?.printers || [], [data.data]);
  const attentionCount = printers.filter(p => tileState(p, config.view).attention).length;
  const offlineCount = printers.filter(p => !p.connected).length;
  const printingCount = printers.filter(p => p.state === 'RUNNING' && p.connected).length;
  const patch = (value: Partial<MonitorConfig>) => setConfig(old => ({ ...old, ...value }));

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    const tick = () => setNow(Date.now());
    const change = () => { setToken(readMonitorToken()); setConfig(initialConfig()); };
    const referrer = document.createElement('meta'); referrer.name = 'referrer'; referrer.content = 'no-referrer'; document.head.appendChild(referrer);
    window.addEventListener('popstate', change); window.addEventListener('hashchange', change);
    document.addEventListener('visibilitychange', tick); window.addEventListener('online', tick);
    return () => { clearInterval(timer); referrer.remove(); window.removeEventListener('popstate', change); window.removeEventListener('hashchange', change);
      document.removeEventListener('visibilitychange', tick); window.removeEventListener('online', tick); };
  }, []);

  useEffect(() => {
    const url = new URL(window.location.href);
    for (const [key, value] of Object.entries(config)) url.searchParams.set(key, String(value));
    window.history.replaceState(null, '', url);
    if (token === null) localStorage.setItem(SETTINGS, JSON.stringify(config));
    void i18n.changeLanguage(config.lang);
    document.title = `BamDude · ${i18n.t('monitor.title')} · ${i18n.t(`monitor.${config.view === 'queues' ? 'queue' : 'printers'}`)}`;
  }, [config, token, i18n]);

  useEffect(() => {
    const node = content.current;
    if (!node) return;
    const measure = () => setViewport({ width: node.clientWidth - 48, height: node.clientHeight - 20 });
    measure();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
    observer?.observe(node); window.addEventListener('resize', measure);
    return () => { observer?.disconnect(); window.removeEventListener('resize', measure); };
  }, [data.loading, data.terminal]);

  const filtered = useMemo(() => printers.filter(p => config.attention ? tileState(p, config.view).attention :
    [p.name, p.model, p.location, ...p.tags].filter(Boolean).join(' ').toLocaleLowerCase(config.lang).includes(config.search.toLocaleLowerCase(config.lang))), [printers, config]);
  useEffect(() => {
    if (frozen) return;
    const key = JSON.stringify(config);
    const priority = filtered.map(p => `${p.printer_id}:${tileState(p, config.view).rank}`).sort().join(',');
    if (key === lastOrder.current.config && priority === lastOrder.current.priority && now - lastOrder.current.at < 15000) return;
    const next = sortMonitor(filtered, config, forecasts).map(p => p.printer_id);
    setOrder(old => old.join(',') === next.join(',') ? old : next);
    lastOrder.current = { at: now, priority, config: key };
  }, [filtered, config, forecasts, frozen, now]);
  const sorted = useMemo(() => {
    const rows = new Map(filtered.map(p => [p.printer_id, p]));
    const ordered = order.map(id => rows.get(id)).filter((p): p is MonitorPrinter => Boolean(p));
    return [...ordered, ...filtered.filter(p => !order.includes(p.printer_id))];
  }, [filtered, order]);
  const groups = useMemo(() => groupMonitor(sorted, config), [sorted, config]);
  const entries = groups.reduce((n, group) => n + group.printers.length, 0);
  const minWidth = config.size === 'auto' ? 176 : [0, 210, 270, 350, 450][Number(config.size)];
  const columns = Math.max(1, Math.floor((viewport.width + 10) / (minWidth + 10)));
  const rows = Math.ceil(entries / columns);
  const tileHeight = config.size === 'auto' && config.group === 'none' ? Math.max(158, Math.min(210, Math.floor((viewport.height - Math.max(0, rows - 1) * 10) / Math.max(1, rows)))) : 190 + (config.size === 'auto' ? 0 : (Number(config.size) - 1) * 18);
  const fits = config.group === 'none' && rows * (tileHeight + 10) - 10 <= viewport.height;
  useEffect(() => {
    const node = content.current;
    if (!node || typeof IntersectionObserver === 'undefined') return;
    const seen = new Map<Element, boolean>();
    const updateVisibleCount = () => {
      setVisibleCount(new Set([...seen]
        .filter(([tile, visible]) => node.contains(tile) && visible)
        .map(([tile]) => tile.getAttribute('data-printer-id'))).size);
    };
    const observer = new IntersectionObserver(changes => {
      for (const change of changes) seen.set(change.target, change.isIntersecting && change.intersectionRatio >= 0.98);
      updateVisibleCount();
    }, { root: node, threshold: [0, 0.98, 1] });
    const observeTiles = () => node.querySelectorAll('[data-printer-id]').forEach(tile => observer.observe(tile));
    observeTiles();
    // A virtual row replaces its tiles while the monitor scrolls. Observe the
    // new nodes as well, otherwise the footer would keep yesterday's count.
    const mutations = new MutationObserver(() => {
      observeTiles();
      updateVisibleCount();
    });
    mutations.observe(node, { childList: true, subtree: true });
    return () => { mutations.disconnect(); observer.disconnect(); };
  }, [groups, tileHeight, columns]);

  const toggleAttention = () => {
    if (!config.attention) { beforeAttention.current = { search: config.search, group: config.group }; patch({ attention: true, search: '', group: 'none' }); }
    else patch({ attention: false, ...beforeAttention.current });
  };
  const fullscreen = async () => {
    try { if (document.fullscreenElement) await document.exitFullscreen(); else await document.documentElement.requestFullscreen(); }
    catch { showToast(t('monitor.fullscreenUnavailable'), 'info'); }
  };
  const terminal = data.terminal || forecast.terminal;
  const chosen = terminal ? undefined : printers.find(p => p.printer_id === selected);
  return <div className={`sm-root sm-color-${config.color}`}>
    <header className="sm-header">
      <div className="sm-brand"><Monitor size={30} className="text-bambu-green" /><div><strong>BamDude</strong><span>{t('monitor.title')}{token !== null ? ` · ${t('monitor.tv')}` : ''}</span></div></div>
      <nav className="sm-tabs" aria-label={t('monitor.view')}>
        <Button size="sm" variant={config.view === 'printers' ? 'primary' : 'ghost'} aria-pressed={config.view === 'printers'} onClick={() => patch({ view: 'printers' })}><Printer size={16} />{t('monitor.printers')}</Button>
        <Button size="sm" variant={config.view === 'queues' ? 'primary' : 'ghost'} aria-pressed={config.view === 'queues'} disabled={data.data?.capabilities.queues === false} onClick={() => patch({ view: 'queues' })}><ListOrdered size={16} />{t('monitor.queue')}</Button>
      </nav>
      <div className="sm-header-actions">
        <Button size="sm" variant="ghost" onClick={() => patch({ lang: config.lang === 'uk' ? 'en' : 'uk' })} aria-label={t('monitor.language')}>{config.lang.toUpperCase()}</Button>
        <Button size="sm" variant="ghost" onClick={() => setMode(resolvedMode === 'dark' ? 'light' : 'dark')} aria-label={t('monitor.theme')}><Sun size={17} /></Button>
        <Button size="sm" variant="outline" onClick={fullscreen}><Expand size={16} /><span>{t('monitor.fullscreen')}</span></Button>
      </div>
    </header>
    <div className="sm-summary">
      <h1>{t(config.view === 'printers' ? 'monitor.printers' : 'monitor.queue')} <span>{data.data ? printers.length : '—'}</span></h1>
      <div className="sm-counters"><span><i className="sm-dot-ok" /><b>{printingCount}</b> {t('monitor.states.printing')}</span><span><i className="sm-dot-neutral" /><b>{offlineCount}</b> {t('monitor.states.offline')}</span>
        <Button size="sm" variant={config.attention ? 'primary' : 'outline'} aria-pressed={config.attention} onClick={toggleAttention}><AlertTriangle size={14} /><b>{attentionCount}</b> {t('monitor.attention')}</Button>
      </div>
    </div>
    <div className="sm-toolbar">
      <label className="sm-search"><Search size={16} /><input aria-label={t('monitor.search')} placeholder={t('monitor.search')} value={config.search} disabled={config.attention} onChange={e => patch({ search: e.target.value })} /></label>
      <FilterDropdown label={t('monitor.sort')} value={config.sort} options={['attention', 'eta', 'freeAt', 'name'].filter(key => key !== 'freeAt' || data.data?.capabilities.forecast !== false).map(value => ({ value, label: t(`monitor.sorts.${value}`) }))} onChange={value => patch({ sort: value as MonitorConfig['sort'] })} />
      <FilterDropdown label={t('monitor.group')} value={config.group} options={['none', 'location', 'tag'].map(value => ({ value, label: t(`monitor.groups.${value}`) }))} onChange={value => patch({ group: value as MonitorConfig['group'] })} />
      <div className="sm-sizing"><Button size="sm" variant={config.size === 'auto' ? 'primary' : 'ghost'} aria-pressed={config.size === 'auto'} onClick={() => patch({ size: 'auto' })}>{t('monitor.auto')}</Button><CardSizeSwitch value={config.size === 'auto' ? 0 : Number(config.size)} onChange={size => patch({ size: String(size) as MonitorConfig['size'] })} /></div>
      <Button size="sm" variant="ghost" aria-pressed={config.color === 'strong'} onClick={() => patch({ color: config.color === 'strong' ? 'soft' : 'strong' })}>{t(config.color === 'strong' ? 'monitor.strong' : 'monitor.soft')}</Button>
    </div>
    <div className={`sm-freshness ${stale || data.error ? 'text-status-warning' : 'text-bambu-gray'}`} role="status">
      {stale || data.error ? <WifiOff size={13} /> : <Wifi size={13} />}
      <span>{terminal ? t(token !== null ? 'monitor.tokenInvalid' : data.error instanceof ApiError && data.error.status === 403 ? 'monitor.accessDenied' : 'monitor.signInRequired') :
        stale ? t('monitor.stale') : data.error ? t('monitor.reconnecting') : data.loading ? t('monitor.loading') : t('monitor.serverUpdated', { time: new Date(data.updatedAt).toLocaleTimeString(config.lang) })}</span>
      {!terminal && <Button size="sm" variant="ghost" onClick={() => { data.retry(); if (!forecast.terminal) forecast.retry(); }} aria-label={t('monitor.retry')}><RefreshCw size={13} /></Button>}
      {frozen && <span>{t('monitor.orderHeld')}</span>}
      {(config.view === 'queues' || config.sort === 'freeAt') && forecastStale && !terminal && <span>{t('monitor.forecastUnavailable')}</span>}
    </div>
    <main ref={contentRef} className="sm-main" onFocusCapture={() => setFrozen(true)} onBlurCapture={e => { if (!e.currentTarget.contains(e.relatedTarget)) setFrozen(false); }}
      onPointerDown={() => setFrozen(true)} onPointerUp={e => setFrozen(e.currentTarget.contains(document.activeElement))} onPointerCancel={() => setFrozen(false)}>
      {terminal ? <div className="sm-empty"><WifiOff size={36} /><h2>{t(token !== null ? 'monitor.tokenInvalid' : 'monitor.accessDenied')}</h2><p>{t(token !== null ? 'monitor.tokenHelp' : 'monitor.accessHelp')}</p>
        {token === null && <a className="text-bambu-green underline" href="/login" target="_blank" rel="noopener noreferrer">{t('monitor.signIn')}</a>}
        {token === null && <Button size="sm" onClick={() => window.location.reload()}>{t('monitor.retry')}</Button>}
      </div> : data.loading ? <div className="sm-grid" aria-label={t('monitor.loading')} style={{ '--sm-columns': columns } as CSSProperties}>{Array.from({ length: 12 }, (_, i) => <div key={i} className="sm-skeleton" />)}</div> : !data.data ?
        <div className="sm-empty"><WifiOff size={36} /><h2>{t('monitor.connectionFailed')}</h2><Button onClick={data.retry}>{t('monitor.retry')}</Button></div> : filtered.length === 0 ?
          <div className="sm-empty"><Printer size={36} /><h2>{t(printers.length === 0 ? 'monitor.empty' : config.attention ? 'monitor.noAttention' : 'monitor.noResults')}</h2></div> : groups.map(group => <section key={group.key} className="sm-group">
            {config.group !== 'none' && !config.attention && <h2 className="sm-group-title">{group.key || t(config.group === 'tag' ? 'monitor.noTag' : 'monitor.noLocation')} <span>{group.printers.length}</span></h2>}
            <ElementVirtualGrid
              items={group.printers}
              scrollElement={scrollElement}
              columns={columns}
              estimateRowHeight={tileHeight}
              rowGap={10}
              className="sm-grid"
              rowClassName="sm-grid"
              gridStyle={{ '--sm-columns': columns, '--sm-height': `${tileHeight}px` } as CSSProperties}
              getItemKey={(printer) => printer.printer_id}
              renderItem={(printer) => <MonitorTile key={printer.printer_id} printer={printer} view={config.view} generatedAt={data.data!.generated_at} now={now} stale={stale}
                forecast={forecasts?.get(printer.printer_id)} forecastStale={forecastStale} onOpen={() => setSelected(printer.printer_id)} />}
              forceVirtualized={entries > 18 && !fits}
              baseOverscan={1}
              debugId={`monitor-${config.view}-${group.key || 'all'}`}
            />
          </section>)}
    </main>
    <footer className="sm-footer"><span>{t('monitor.visible', { count: visibleCount, total: printers.length })}{!fits && entries > 0 ? ` · ${t('monitor.scrollHint')}` : ''}</span>
      <span className="sm-legend"><i className="sm-dot-ok" />{t('monitor.states.printing')}<i className="sm-dot-error" />{t('monitor.states.error')}<i className="sm-dot-warning" />{t('monitor.states.paused')}<i className="sm-dot-info" />{t('monitor.states.plate')}</span>
    </footer>
    {chosen && data.data && <MonitorDetails printer={chosen} view={config.view} capabilities={data.data.capabilities} forecast={forecasts?.get(chosen.printer_id)} stale={stale} onClose={() => setSelected(null)} />}
  </div>;
}
