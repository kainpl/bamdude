import { useTranslation } from 'react-i18next';
import { ExternalLink, Printer } from 'lucide-react';
import { Modal } from '../../components/Modal';
import type { PrinterForecast } from '../../api/client';
import { duration } from './format';
import { useJobLabel } from './useJobLabel';
import { tileState } from './state';
import type { MonitorPrinter, MonitorSnapshot, MonitorView } from './types';

export function MonitorDetails({ printer: p, view, capabilities, forecast, stale, onClose }: {
  printer: MonitorPrinter; view: MonitorView; capabilities: MonitorSnapshot['capabilities'];
  forecast?: PrinterForecast; stale: boolean; onClose: () => void;
}) {
  const { t, i18n } = useTranslation();
  const jobLabel = useJobLabel();
  const label = t(`monitor.states.${tileState(p, view).kind}`);
  const rows = [
    [t('monitor.printerState'), t(`monitor.rawStates.${p.state}`, { defaultValue: t('monitor.states.unknown') })],
    [t('monitor.currentJob'), jobLabel(p.current_job)],
    [t('monitor.jobEta'), duration(p.remaining_seconds, i18n.language)],
    [t('monitor.progress'), p.progress == null ? '—' : `${Math.round(p.progress)}%`],
    [t('monitor.layers'), `${p.layer_num ?? '—'} / ${p.total_layers ?? '—'}`],
    [t('monitor.queue'), p.queue ? t(p.queue.status === 'error' ? 'monitor.states.queueError' : p.queue.is_paused || p.queue.status === 'paused' ? 'monitor.states.queuePaused' : 'monitor.queueEnabled') : '—'],
    [t('monitor.nextJob'), jobLabel(p.queue?.next_job, true)],
    [t('monitor.pending'), String(p.queue?.pending_count ?? '—')],
    [t('monitor.queueEta'), forecast && !(forecast.unknown_prints > 0 && forecast.free_seconds === 0) ? `${forecast.unknown_prints ? '≥' : '≈'} ${duration(forecast.free_seconds, i18n.language)}` : t('monitor.forecastUnavailable')],
    [t('monitor.unknownDurations'), String(forecast?.unknown_prints ?? '—')],
    [t('monitor.waitReason'), p.queue?.waiting ? t(`monitor.waits.${p.queue.waiting.code}`) : '—'],
    [t('monitor.scheduledAt'), p.queue?.waiting?.until ? new Date(p.queue.waiting.until).toLocaleString(i18n.language) : '—'],
    [t('monitor.pauseReason'), p.pause_reason ? t(`monitor.pauses.${p.pause_reason}`, { defaultValue: t('monitor.pauses.unknown') }) : '—'],
    [t('monitor.autoQueue'), p.queue?.auto_distribute_eligible === false ? t('monitor.autoQueueOff') : '—'],
    [t('monitor.telemetryAt'), p.status_received_at ? new Date(p.status_received_at).toLocaleString(i18n.language) : '—'],
  ];
  return <Modal title={p.name} icon={<Printer size={20} />} onClose={onClose} size="2xl">
    <div className="p-4 space-y-4">
      <p className="text-lg font-semibold text-white">{label}</p>
      {(stale || p.source_stale) && <p role="status" className="text-status-warning">{t('monitor.stale')}</p>}
      {!capabilities.job_details && <p className="text-sm text-bambu-gray">{t('monitor.tvPrivacy')}</p>}
      <dl className="sm-detail-list">{rows.map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl>
      {Object.keys(p.temperatures).length > 0 && <div className="flex gap-4 flex-wrap text-sm text-white">
        {Object.entries(p.temperatures).map(([key, value]) => <span key={key}>{t(`monitor.temperatures.${key}`, { defaultValue: key })}: {Math.round(value)}°C</span>)}
      </div>}
      {p.hms_errors.length > 0 && <div className="text-status-warning"><p>{t('monitor.activeHms')}</p><ul>{p.hms_errors.map(e => <li key={e.code}>{e.code} · {t('monitor.severity', { value: e.severity })}</li>)}</ul></div>}
      <div className="flex gap-4 text-sm">
        {capabilities.open_printer && <a className="text-bambu-green flex items-center gap-1 underline" href={`/?monitorPrinter=${p.printer_id}`} target="_blank" rel="noopener noreferrer"><ExternalLink size={14} />{t('monitor.openPrinter')}</a>}
        {capabilities.open_queue && <a className="text-bambu-green flex items-center gap-1 underline" href={`/queue?monitorPrinter=${p.printer_id}`} target="_blank" rel="noopener noreferrer"><ExternalLink size={14} />{t('monitor.openQueue')}</a>}
      </div>
    </div>
  </Modal>;
}
