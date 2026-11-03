import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, Loader2 } from 'lucide-react';
import { api, ApiError, type ObicoDetectionEvent, type ObicoStatus } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { Card, CardContent, CardHeader } from './Card';
import { Button } from './Button';
import { ConfirmModal } from './ConfirmModal';
import { Modal } from './Modal';

type Selection = { printer: number; id: string };

export function ObicoHistoryPanel({ recent, printers, storage, onRefresh }: {
  recent: ObicoDetectionEvent[];
  printers: { id: number; name: string }[];
  storage?: ObicoStatus['storage'];
  onRefresh: () => void;
}) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const queries = useQueryClient();
  const canRead = hasPermission('printers:read');
  const canView = canRead && hasPermission('camera:view');
  const canClear = hasPermission('settings:update');
  const [tab, setTab] = useState<'recent' | 'saved'>('recent');
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const [selected, setSelected] = useState<Selection | null>(null);
  const [confirm, setConfirm] = useState(false);
  const saved = useQuery({ queryKey: ['obico-history', cursors.at(-1)],
    queryFn: () => api.getObicoHistory({ cursor: cursors.at(-1) }),
    enabled: tab === 'saved' && canRead, refetchInterval: 10000, retry: false });
  useEffect(() => {
    // A cursor can expire or belong to the JWT before its refresh. Recover once
    // by reading page one; a failure of that fresh read stays visible normally.
    if (cursors.length > 1 && saved.error instanceof ApiError && saved.error.status === 409) {
      setCursors([undefined]);
    }
  }, [cursors.length, saved.error]);
  const clearSummary = useQuery({ queryKey: ['obico-history-clear-summary'],
    queryFn: () => api.getObicoHistory({ limit: 1 }), enabled: confirm && canClear, retry: false });
  const clear = useMutation({ mutationFn: api.clearObicoHistory, onSuccess: () => {
    setConfirm(false); setSelected(null); setCursors([undefined]);
    void queries.invalidateQueries({ queryKey: ['obico-history'] });
    void queries.invalidateQueries({ queryKey: ['obico-status'] });
  } });
  const refresh = () => {
    setCursors([undefined]); onRefresh();
    void queries.invalidateQueries({ queryKey: ['obico-history'] });
  };
  const items = tab === 'recent' ? recent : saved.data?.items ?? [];
  const dropped = Object.values(storage?.dropped ?? {}).reduce((sum, count) => sum + count, 0);

  return <Card id="card-fd-history">
    <CardHeader>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-lg font-semibold text-white">{t('failureDetection.historyTitle')}</h2>
        <Button variant="ghost" size="sm" onClick={refresh}>{t('common.refresh')}</Button>
      </div>
      <div className="mt-3 flex gap-2" role="tablist" aria-label={t('failureDetection.historyTitle')}>
        {(['recent', 'saved'] as const).map(value => <button key={value} role="tab" aria-selected={tab === value}
          onClick={() => setTab(value)} className={`px-3 py-1.5 rounded text-sm ${tab === value ? 'bg-bambu-green text-black' : 'text-bambu-gray hover:text-white'}`}>
          {t(`failureDetection.evidence.${value}`)}
        </button>)}
      </div>
    </CardHeader>
    <CardContent className="space-y-3">
      {(dropped > 0 || storage?.reason) && <p className="flex items-start gap-2 text-sm text-amber-700 dark:text-amber-400" role="status">
        <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />{t(storage?.reason === 'cleanup_failed'
          ? 'failureDetection.evidence.cleanupWarning' : 'failureDetection.evidence.storageWarning', { count: dropped })}
      </p>}
      {tab === 'saved' && <>
        <p className="text-xs text-bambu-gray">{t('failureDetection.evidence.retention')}</p>
        {saved.data?.oldest_saved_at && <p className="text-xs text-bambu-gray">{t('failureDetection.evidence.oldest', {
          date: new Date(saved.data.oldest_saved_at).toLocaleString() })}</p>}
      </>}
      {!canRead ? <p className="text-sm text-bambu-gray">{t('failureDetection.evidence.noPermission')}</p>
        : tab === 'saved' && saved.isPending ? <Loader2 className="h-4 w-4 animate-spin" />
        : tab === 'saved' && saved.isError ? <div role="alert" className="text-sm text-red-700 dark:text-red-400">
          {saved.error.message}<Button variant="ghost" onClick={refresh}>{t('common.refresh')}</Button>
        </div>
        : !items.length ? <p className="text-sm text-bambu-gray">{t('failureDetection.noHistory')}</p>
        : <div className="max-h-96 overflow-y-auto space-y-2">
          {items.map((event, index) => <div key={event.id ?? index} className="border-b border-bambu-dark-tertiary py-2 min-w-0">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0 text-sm">
                <div className="truncate text-white" title={event.task_name}>{event.task_name || printers.find(p => p.id === event.printer_id)?.name || `#${event.printer_id}`}</div>
                <div className="text-xs text-bambu-gray">{printers.find(p => p.id === event.printer_id)?.name ?? `#${event.printer_id}`} · {new Date(event.timestamp).toLocaleString()}</div>
              </div>
              <span className={`shrink-0 text-xs ${event.class === 'failure' ? 'text-red-700 dark:text-red-400' : event.class === 'warning' ? 'text-amber-700 dark:text-amber-400' : 'text-bambu-gray'}`}>
                {t(`failureDetection.evidence.${event.class}`)} · {event.score.toFixed(3)}
              </span>
            </div>
            {event.id && event.state === 'ready' && canView
              ? <Button size="sm" variant="ghost" onClick={() => setSelected({ printer: event.printer_id, id: event.id! })}>{t('failureDetection.evidence.view')}</Button>
              : <span className="text-xs text-bambu-gray">{t(event.state === 'writing' ? 'failureDetection.evidence.pending' : event.state === 'unavailable' ? 'failureDetection.evidence.unavailableShort' : !canView && event.state === 'ready' ? 'failureDetection.evidence.noPermission' : 'failureDetection.evidence.notSaved')}</span>}
          </div>)}
        </div>}
      {tab === 'saved' && canRead && <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex gap-2">
          <Button size="sm" variant="secondary" disabled={cursors.length === 1 || saved.isFetching}
            onClick={() => setCursors(cursors.slice(0, -1))}>{t('failureDetection.evidence.previous')}</Button>
          <Button size="sm" variant="secondary" disabled={!saved.data?.next_cursor || saved.isFetching}
            onClick={() => setCursors([...cursors, saved.data!.next_cursor!])}>{t('failureDetection.evidence.next')}</Button>
        </div>
        {canClear && <Button size="sm" variant="danger" onClick={() => { clear.reset(); setConfirm(true); }}>
          {t('failureDetection.evidence.clear')}
        </Button>}
      </div>}
      {selected && <ObicoEvidenceViewer selection={selected} allowed={canView} onClose={() => setSelected(null)} />}
      {confirm && <ConfirmModal title={t('failureDetection.evidence.clear')}
        message={clearSummary.data && !clearSummary.isFetching ? t('failureDetection.evidence.clearConfirm', { count: clearSummary.data.total_count }) : t('common.loading')}
        confirmDisabled={!clearSummary.data || clearSummary.isFetching || clearSummary.isError} isLoading={clear.isPending} variant="danger"
        onConfirm={() => clear.mutate()} onCancel={() => setConfirm(false)}>
        {(clear.isError || clearSummary.isError) && <p role="alert" className="text-red-700 dark:text-red-400">{clear.error?.message ?? clearSummary.error?.message}</p>}
      </ConfirmModal>}
    </CardContent>
  </Card>;
}

export function ObicoEvidenceViewer({ selection, allowed, onClose }: {
  selection: Selection; allowed: boolean; onClose: () => void;
}) {
  const { t } = useTranslation();
  const [overlay, setOverlay] = useState(true);
  const [url, setUrl] = useState<string | null>(null);
  const [imageError, setImageError] = useState(false);
  const supportsEncodedOrientation = typeof CSS !== 'undefined' && typeof CSS.supports === 'function'
    && CSS.supports('image-orientation', 'none');
  const detail = useQuery({ queryKey: ['obico-event', selection.printer, selection.id],
    queryFn: () => api.getObicoEvent(selection.printer, selection.id), enabled: allowed, retry: false, staleTime: 0 });
  const event = detail.data;
  useEffect(() => {
    let retired = false;
    let objectUrl: string | undefined;
    setUrl(null); setImageError(false);
    if (allowed && event?.state === 'ready') {
      void api.getObicoEventImage(selection.printer, selection.id).then(blob => {
        if (retired) return;
        objectUrl = URL.createObjectURL(blob); setUrl(objectUrl);
      }).catch(() => { if (!retired) setImageError(true); });
    }
    return () => { retired = true; if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [allowed, selection.printer, selection.id, event?.state]);

  return <Modal title={t('failureDetection.evidence.view')} onClose={onClose} size="5xl" bodyClassName="p-4 space-y-3">
    {!allowed ? <p>{t('failureDetection.evidence.noPermission')}</p>
      : detail.isError || imageError || event?.state === 'unavailable' ? <p role="alert">{t('failureDetection.evidence.unavailable')}</p>
      : !event || !url ? <Loader2 aria-label={t('common.loading')} className="h-5 w-5 animate-spin" />
      : <>
        <p className="text-sm text-bambu-gray">{t('failureDetection.evidence.serverBoxes')}</p>
        {!supportsEncodedOrientation && <p role="status" className="text-sm text-amber-700 dark:text-amber-400">{t('failureDetection.evidence.orientationUnsupported')}</p>}
        <label className="flex items-center gap-2 text-sm text-white"><input type="checkbox" checked={overlay}
          disabled={!supportsEncodedOrientation} onChange={e => setOverlay(e.target.checked)} />{t('failureDetection.evidence.overlay')}</label>
        <div className="relative mx-auto w-full overflow-hidden bg-black"
          style={{ aspectRatio: `${event.width}/${event.height}`, maxWidth: `calc(60vh * ${event.width / event.height})` }}>
          <img src={url} alt={t('failureDetection.evidence.original')} onError={() => setImageError(true)}
            className="block w-full h-full" style={{ imageOrientation: 'none' }} />
          {overlay && supportsEncodedOrientation && <svg aria-label={t('failureDetection.evidence.overlay')} className="absolute inset-0 w-full h-full pointer-events-none"
            viewBox={`0 0 ${event.width} ${event.height}`}>
            {event.boxes.map((box, index) => {
              const [x1, y1, x2, y2] = box.xyxy;
              return <g key={index}><title>{box.label}: {(box.confidence * 100).toFixed(1)}%</title>
                <rect x={x1 * event.width} y={y1 * event.height} width={(x2 - x1) * event.width} height={(y2 - y1) * event.height}
                  fill="none" stroke="#ffb020" strokeWidth={Math.max(2, event.width / 350)} />
              </g>;
            })}
          </svg>}
          {overlay && supportsEncodedOrientation && event.boxes.map((box, index) => <span key={index}
            className="pointer-events-none absolute flex gap-1 rounded bg-black/80 px-1 text-[11px] leading-tight text-amber-300"
            style={{ left: `${Math.min(box.xyxy[0], .7) * 100}%`, top: `${box.xyxy[1] * 100}%`,
              maxWidth: `${Math.max(.3, 1 - box.xyxy[0]) * 100}%`, transform: box.xyxy[1] > 0 ? 'translateY(-100%)' : undefined }}>
            <span className="min-w-0 truncate">{box.label}</span><span className="shrink-0">{(box.confidence * 100).toFixed(1)}%</span>
          </span>)}
        </div>
        {!event.boxes.length && <p className="text-sm text-bambu-gray">{t('failureDetection.evidence.noBoxes')}</p>}
        <p className="text-xs text-bambu-gray">{t('failureDetection.evidence.source', { source: t(`failureDetection.evidence.source_${event.facts.frame_source ?? 'unknown'}`) })}</p>
        {event.facts.buffer_received_at && <p className="text-xs text-bambu-gray">{t('failureDetection.evidence.bufferArrival', {
          date: new Date(event.facts.buffer_received_at).toLocaleString() })}</p>}
        <p className="text-xs text-bambu-gray">{t('failureDetection.evidence.captureUnknown')}</p>
      </>}
  </Modal>;
}
