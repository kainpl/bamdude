import { AlertTriangle, CheckCircle2, Clock3, Pause, Printer, WifiOff } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Card } from '../../components/Card';
import { getPrinterImage } from '../../utils/printer';
import type { PrinterForecast } from '../../api/client';
import { remainingSeconds, tileState } from './state';
import type { MonitorPrinter, MonitorView } from './types';
import { clockTime, duration } from './format';
import { useJobLabel } from './useJobLabel';

interface Props {
  printer: MonitorPrinter; view: MonitorView; generatedAt: string; now: number; stale: boolean;
  forecast?: PrinterForecast; forecastStale: boolean; onOpen: () => void;
}

export function MonitorTile({ printer: p, view, generatedAt, now, stale, forecast, forecastStale, onOpen }: Props) {
  const { t, i18n } = useTranslation();
  const jobLabel = useJobLabel();
  const state = tileState(p, view);
  const Icon = state.kind === 'offline' || state.kind === 'lost' ? WifiOff : state.kind === 'paused' || state.kind === 'queuePaused' ? Pause :
    state.attention ? AlertTriangle : state.kind === 'finished' ? CheckCircle2 : Printer;
  const outdated = stale || p.source_stale;
  const seconds = remainingSeconds(p, generatedAt, now, outdated);
  const progress = Math.max(0, Math.min(100, p.dispatch?.upload_progress ?? p.progress ?? 0));
  const label = t(`monitor.states.${state.kind}`);
  const queueEstimate = !forecast || (forecast.unknown_prints > 0 && forecast.free_seconds === 0) ? null : forecast.free_seconds;
  const queueTime = queueEstimate == null ? '—' : `${forecast?.unknown_prints ? '≥ ' : '≈ '}${duration(queueEstimate, i18n.language, true)}`;
  const clock = seconds == null || p.state !== 'RUNNING' || outdated ? '—' :
    clockTime(now + seconds * 1000, i18n.language);
  return <Card className={`sm-tile sm-tone-${state.tone} ${outdated ? 'sm-outdated' : ''}`}
    data-printer-id={p.printer_id} data-live-status-printer-id={p.printer_id} data-state={state.kind} role="button" tabIndex={0}
    aria-label={`${p.name}: ${label}${outdated ? `, ${t('monitor.stale')}` : ''}`}
    onClick={onOpen} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen(); } }}>
    <div className="sm-tile-top">
      <div className="min-w-0"><h2 title={p.name}>{p.name}</h2><span className="sm-meta">{p.model || '—'}{p.location ? ` · ${p.location}` : ''}</span></div>
      <img className="sm-printer-image" src={getPrinterImage(p.model)} alt="" loading="lazy" />
    </div>
    <div className="sm-state-line"><Icon size={14} aria-hidden /><strong>{label}</strong>{outdated && <WifiOff size={13} aria-label={t('monitor.stale')} />}</div>
    <div className="sm-job" title={jobLabel(p.current_job)}>
      {p.state === 'RUNNING' && state.kind !== 'printing' && <span>{t('monitor.states.printing')} · </span>}{jobLabel(p.current_job)}
    </div>
    <div className="sm-progress" role="progressbar" aria-label={t(p.dispatch?.phase === 'uploading' ? 'monitor.uploadProgress' : 'monitor.progress')}
      aria-valuemin={0} aria-valuemax={100} aria-valuenow={p.progress == null && p.dispatch?.upload_progress == null ? undefined : progress}>
      <span style={{ width: `${progress}%` }} />
    </div>
    <div className="sm-metrics">
      <div title={`${t('monitor.jobEta')}: ${duration(seconds, i18n.language)}`}><span>{t('monitor.jobEtaShort')}</span><strong>{seconds === 0 && p.state === 'RUNNING' ? t('monitor.waitingUpdateShort') : duration(seconds, i18n.language, true)}</strong></div>
      <div className="text-right" title={view === 'queues' ? `${t('monitor.queueEta')}: ${queueEstimate == null ? t('monitor.forecastUnavailable') : duration(queueEstimate, i18n.language)}` : undefined}><span>{t(view === 'queues' ? 'monitor.queueEtaShort' : 'monitor.progress')}</span><strong className={forecastStale && view === 'queues' ? 'opacity-60' : ''}>{view === 'queues' ? queueTime : p.progress == null ? '—' : `${Math.round(progress)}%`}</strong></div>
    </div>
    <div className="sm-tile-footer">
      {view === 'queues' ? <>
        <span title={jobLabel(p.queue?.next_job, true)}>{p.queue?.pending_count ? jobLabel(p.queue.next_job, true) : t('monitor.queueEmpty')}</span>
        <b>{p.queue?.pending_count ?? '—'} <span className="font-normal">{t('monitor.pending')}</span></b>
      </> : <>
        <span><Clock3 size={12} aria-hidden />{clock}</span>
        <span>{p.queue?.is_paused ? t('monitor.queuePausedShort') : p.pause_reason ? t(`monitor.pauses.${p.pause_reason}`, { defaultValue: t('monitor.pauses.unknown') }) :
          p.queue?.auto_distribute_eligible === false ? t('monitor.autoQueueOff') : `${p.layer_num ?? '—'} / ${p.total_layers ?? '—'}`}</span>
      </>}
    </div>
  </Card>;
}
