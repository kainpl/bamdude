import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { CheckSquare, Download, Film, Loader2, MinusSquare, RefreshCw, Trash2 } from 'lucide-react';
import {
  api,
  type ArchiveLocalVideo,
  type ArchiveMediaSources,
  type ArchiveRemoteVideo,
  type MediaSourceStatus,
} from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { useToast } from '../contexts/ToastContext';
import { parseUTCDate } from '../utils/date';
import { formatFileSize } from '../utils/file';
import { clickSelection } from '../utils/rangeSelection';
import { Button } from './Button';
import { ConfirmModal } from './ConfirmModal';
import { Modal } from './Modal';
import { Select } from './Select';
import { SelectionBox } from './SelectionBox';
import { DownloadCloseChoice } from './downloads/DownloadCloseChoice';
import { DownloadJobPanel } from './downloads/DownloadJobPanel';
import { useDownloadJob, type DownloadJobApi } from './downloads/useDownloadJob';

interface ArchiveMediaDownloadModalProps {
  archiveId: number;
  archiveName: string;
  onClose: () => void;
}

const MARGINS = [10, 30, 60, 180] as const;
const LOCAL = 'local:';
const REMOTE = 'remote:';

const remoteKey = (item: ArchiveRemoteVideo) => `${REMOTE}${item.storage}:${item.path}`;

/** The printer's clock offset as the operator types it: hours, halves allowed; empty means "not given". */
function parseOffsetHours(text: string): number | null | undefined {
  const trimmed = text.trim().replace(',', '.');
  if (trimmed === '') return null;
  const hours = Number(trimmed);
  if (!Number.isFinite(hours) || Math.abs(hours) > 24) return undefined;
  return Math.round(hours * 60);
}

/**
 * The videos of one print: the one attached to the archive, and what its
 * printer still holds — timelapse recordings and the onboard camera's chunks.
 *
 * The three lists answer separately; a printer that is off never hides the
 * attached video. Nothing is preselected: a remote file's time is the
 * printer's clock, whose zone is not known, so a match is shown only for an
 * exact time or after the operator states the clock's offset — and then it is
 * marked as their assumption.
 */
export function ArchiveMediaDownloadModal({ archiveId, archiveName, onClose }: ArchiveMediaDownloadModalProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [anchors, setAnchors] = useState<Record<string, string | null>>({});
  const [offsetText, setOffsetText] = useState('');
  // The offset the lists were asked with. Typing does not ask the printer again; leaving the
  // field, Enter or Refresh does — every answer is a fresh listing of the printer's storage.
  const [appliedOffset, setAppliedOffset] = useState<number | null>(null);
  const [margin, setMargin] = useState<number>(MARGINS[0]);
  const [starting, setStarting] = useState(false);
  const [closeChoice, setCloseChoice] = useState(false);
  const { hasAnyPermission } = useAuth();
  const canRemoveSaved = hasAnyPermission('archives:delete_own', 'archives:delete_all');
  const [removing, setRemoving] = useState<ArchiveLocalVideo | null>(null);
  const [removeBusy, setRemoveBusy] = useState(false);

  const offsetMinutes = parseOffsetHours(offsetText);
  const offsetValid = offsetMinutes !== undefined;

  const applyOffset = () => {
    if (offsetValid) setAppliedOffset(offsetMinutes ?? null);
  };

  const { data, isLoading, isError, isFetching, refetch } = useQuery({
    queryKey: ['archiveMediaSources', archiveId, appliedOffset, margin],
    queryFn: () => api.getArchiveMediaSources(archiveId, { offsetMinutes: appliedOffset, marginMinutes: margin }),
    // Every refetch lists the printer's storage: only on open, on a changed window and on Refresh.
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    placeholderData: (previous) => previous,
  });

  const downloadApi = useMemo<DownloadJobApi>(
    () => ({
      get: (jobId) => api.getArchiveDownloadJob(archiveId, jobId),
      cancel: (jobId) => api.cancelArchiveDownloadJob(archiveId, jobId),
      mint: (jobId, acceptPartial) => api.mintArchiveDownloadUrl(archiveId, jobId, acceptPartial),
    }),
    [archiveId],
  );
  const download = useDownloadJob(downloadApi);
  const adoptDownload = download.adopt;
  useEffect(() => {
    let ignore = false;
    api
      .listArchiveDownloadJobs(archiveId)
      .then(({ jobs }) => {
        if (ignore) return;
        const current = [...jobs]
          .reverse()
          .find((job) => job.phase === 'queued' || job.phase === 'preparing' || job.phase === 'cancelling' || job.phase === 'ready');
        if (current) adoptDownload(current);
      })
      .catch(() => {
        // Nothing to pick up.
      });
    return () => {
      ignore = true;
    };
  }, [archiveId, adoptDownload]);
  const downloadPhase = download.lost ? null : (download.job?.phase ?? null);
  const downloadActive = downloadPhase === 'queued' || downloadPhase === 'preparing' || downloadPhase === 'cancelling';

  // What the server offers now. A selected file that is no longer offered leaves the selection
  // only when its list answered completely — a printer that did not answer proves nothing.
  const offered = useMemo(() => {
    const keys = new Set<string>();
    if (!data) return keys;
    for (const item of data.local.items) keys.add(`${LOCAL}${item.asset}`);
    for (const item of [...data.timelapse.items, ...data.camera.items]) keys.add(remoteKey(item));
    return keys;
  }, [data]);
  useEffect(() => {
    if (!data) return;
    const settled = (status: MediaSourceStatus) => status === 'ok' || status === 'empty';
    if (!settled(data.timelapse.status) || !settled(data.camera.status)) return;
    setSelected((prev) => {
      const kept = [...prev].filter((key) => offered.has(key));
      return kept.length === prev.size ? prev : new Set(kept);
    });
  }, [data, offered]);

  const closed = (status: MediaSourceStatus) => status === 'no_printer' || status === 'forbidden';
  const remoteClosed = data ? closed(data.timelapse.status) && closed(data.camera.status) : false;

  const click = (section: string, order: string[], key: string, shift: boolean) => {
    const next = clickSelection(selected, order, anchors[section] ?? null, key, shift);
    setSelected(next.selected);
    setAnchors((prev) => ({ ...prev, [section]: next.anchor }));
  };
  const setMany = (keys: string[], on: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev);
      for (const key of keys) {
        if (on) next.add(key);
        else next.delete(key);
      }
      return next;
    });

  const startDownload = async () => {
    if (!data || selected.size === 0 || starting || downloadActive) return;
    const assets = data.local.items.filter((item) => selected.has(`${LOCAL}${item.asset}`)).map((item) => item.asset);
    const remote = [...data.timelapse.items, ...data.camera.items]
      .filter((item) => selected.has(remoteKey(item)))
      .map((item) => ({ printer_id: item.printer_id, storage: item.storage, path: item.path, size: item.size }));
    setStarting(true);
    try {
      const job = await api.createArchiveDownloadJob(archiveId, {
        assets,
        remote,
        as_zip: assets.length + remote.length > 1,
      });
      download.adopt(job, { autoSave: true });
    } catch (error) {
      showToast(
        t('archives.media.startFailed', { error: error instanceof Error ? error.message : String(error) }),
        'error',
      );
    } finally {
      setStarting(false);
    }
  };

  const removeSaved = async () => {
    if (!removing) return;
    setRemoveBusy(true);
    try {
      await api.deleteTimelapseVariant(archiveId, removing.asset);
      setRemoving(null);
      await refetch();
    } catch (error) {
      showToast(
        t('archives.media.removeSavedFailed', { error: error instanceof Error ? error.message : String(error) }),
        'error',
      );
    } finally {
      setRemoveBusy(false);
    }
  };

  const dismissDownload = () => {
    const job = download.job;
    if (job && job.phase === 'ready' && !download.lost) {
      void api.cancelArchiveDownloadJob(archiveId, job.job_id).catch(() => {});
    }
    download.adopt(null);
  };

  const requestClose = () => {
    if (downloadActive) setCloseChoice(true);
    else onClose();
  };

  return (
    <>
      <Modal
        onClose={requestClose}
        title={t('archives.media.title')}
        icon={<Film className="w-5 h-5 text-bambu-green" />}
        size="2xl"
        subheader={<p className="px-4 pb-2 text-sm text-bambu-gray truncate">{archiveName}</p>}
        footer={
          <div className="flex items-center justify-between gap-3">
            <span className="text-sm text-bambu-gray">{t('archives.media.selected', { count: selected.size })}</span>
            <div className="flex gap-2">
              <Button variant="secondary" onClick={requestClose}>
                {t('common.close')}
              </Button>
              <Button onClick={startDownload} disabled={selected.size === 0 || starting || downloadActive}>
                {starting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
                {t('archives.media.downloadSelected')}
              </Button>
            </div>
          </div>
        }
        alert={
          download.job ? (
            <DownloadJobPanel
              job={download.job}
              lost={download.lost}
              saving={download.saving}
              saveError={download.saveError}
              onCancel={() => void download.cancel()}
              onSave={(acceptPartial) => void download.save(acceptPartial)}
              onDismiss={dismissDownload}
            />
          ) : undefined
        }
      >
        {isLoading ? (
          <div className="flex items-center justify-center gap-2 p-4 py-10 text-bambu-gray">
            <Loader2 className="w-5 h-5 animate-spin" />
            {t('archives.media.searching')}
          </div>
        ) : isError || !data ? (
          <div className="p-4 py-10 text-center text-sm text-bambu-gray" data-testid="archive-media-error">
            {t('archives.media.searchFailed')}
            <div className="mt-3">
              <Button variant="secondary" size="sm" onClick={() => void refetch()}>
                <RefreshCw className="w-4 h-4" />
                {t('archives.media.refresh')}
              </Button>
            </div>
          </div>
        ) : (
          <div className="space-y-5 p-4">
            <LocalSection
              data={data}
              selected={selected}
              onClick={click}
              onMany={setMany}
              onRemove={canRemoveSaved ? setRemoving : undefined}
            />

            {/* The clock settings only matter where the printer's files can be listed at all. */}
            {!remoteClosed && (
            <div className="rounded-lg border border-bambu-dark-tertiary p-3 text-sm" data-testid="archive-media-clock">
              <div className="flex flex-wrap items-end gap-4">
                <label className="flex flex-col gap-1">
                  <span className="text-xs text-bambu-gray">{t('archives.media.offsetLabel')}</span>
                  <input
                    type="text"
                    inputMode="decimal"
                    value={offsetText}
                    onChange={(event) => setOffsetText(event.target.value)}
                    onBlur={applyOffset}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter') applyOffset();
                    }}
                    placeholder={t('archives.media.offsetPlaceholder')}
                    aria-invalid={!offsetValid}
                    className={`w-28 rounded-md border bg-bambu-dark px-2 py-1 text-white ${
                      offsetValid ? 'border-bambu-dark-tertiary' : 'border-red-500'
                    }`}
                  />
                </label>
                <label className="flex flex-col gap-1">
                  <span className="text-xs text-bambu-gray">{t('archives.media.marginLabel')}</span>
                  <Select size="sm" value={margin} onChange={(event) => setMargin(Number(event.target.value))}>
                    {MARGINS.map((minutes) => (
                      <option key={minutes} value={minutes}>
                        {t('archives.media.marginMinutes', { count: minutes })}
                      </option>
                    ))}
                  </Select>
                </label>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => {
                    const next = offsetValid ? (offsetMinutes ?? null) : appliedOffset;
                    if (next !== appliedOffset) setAppliedOffset(next);
                    else void refetch();
                  }}
                  disabled={isFetching}
                >
                  {isFetching ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
                  {t('archives.media.refresh')}
                </Button>
              </div>
              <p className="mt-2 text-xs text-bambu-gray">
                {data.window ? t('archives.media.windowHint') : t('archives.media.noWindow')}
              </p>
            </div>
            )}

            <RemoteSection
              section="timelapse"
              title={t('archives.media.timelapse')}
              source={data.timelapse}
              selected={selected}
              onClick={click}
              onMany={setMany}
            />
            <RemoteSection
              section="camera"
              title={t('archives.media.camera')}
              source={data.camera}
              selected={selected}
              onClick={click}
              onMany={setMany}
            />
          </div>
        )}
      </Modal>
      {removing && (
        <ConfirmModal
          title={t('archives.media.removeSavedTitle')}
          message={t('archives.media.removeSavedConfirm', { name: removing.name })}
          confirmText={t('archives.media.removeSaved')}
          variant="danger"
          isLoading={removeBusy}
          onConfirm={() => void removeSaved()}
          onCancel={() => setRemoving(null)}
        />
      )}
      {closeChoice && (
        <DownloadCloseChoice
          onStay={() => setCloseChoice(false)}
          onKeep={onClose}
          onCancelDownload={() => {
            void download.cancel().finally(onClose);
          }}
        />
      )}
    </>
  );
}

/**
 * A remote file's time as it may honestly be shown. An exact time is an instant and goes
 * into the viewer's zone; anything else is the printer's own wall clock, shown as the
 * printer wrote it — converting it would invent a zone nobody confirmed.
 */
function fileTime(item: ArchiveRemoteVideo): string {
  if (!item.mtime) return '';
  if (item.time_basis === 'exact') return parseUTCDate(item.mtime)?.toLocaleString() ?? '';
  return item.mtime.replace('T', ' ').slice(0, 16);
}

type ClickHandler = (section: string, order: string[], key: string, shift: boolean) => void;
type ManyHandler = (keys: string[], on: boolean) => void;

function SectionHeader({
  title,
  keys,
  selected,
  onMany,
}: {
  title: string;
  keys: string[];
  selected: Set<string>;
  onMany: ManyHandler;
}) {
  const { t } = useTranslation();
  const any = keys.some((key) => selected.has(key));
  return (
    <div className="mb-2 flex items-center justify-between gap-3">
      <h3 className="text-sm font-semibold text-white">{title}</h3>
      {keys.length > 1 &&
        (any ? (
          <button
            type="button"
            onClick={() => onMany(keys, false)}
            className="flex items-center gap-1 text-xs text-bambu-gray hover:text-white"
          >
            <MinusSquare className="w-4 h-4" />
            {t('archives.media.clear')}
          </button>
        ) : (
          <button
            type="button"
            onClick={() => onMany(keys, true)}
            className="flex items-center gap-1 text-xs text-bambu-gray hover:text-white"
          >
            <CheckSquare className="w-4 h-4" />
            {t('archives.media.selectAll')}
          </button>
        ))}
    </div>
  );
}

function Row({
  checked,
  name,
  detail,
  note,
  onClick,
}: {
  checked: boolean;
  name: string;
  detail: string;
  note?: { text: string; tone: 'match' | 'muted' };
  onClick: (shift: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={checked}
      onClick={(event) => onClick(event.shiftKey)}
      className={`flex w-full items-center gap-3 rounded-lg p-2 text-left transition-colors ${
        checked ? 'bg-bambu-green/20 border border-bambu-green/50' : 'border border-transparent hover:bg-bambu-dark-tertiary'
      }`}
    >
      <SelectionBox checked={checked} className="w-5 h-5 flex-shrink-0" />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-white">{name}</span>
        {note && (
          <span className={`block text-xs ${note.tone === 'match' ? 'text-bambu-green' : 'text-bambu-gray'}`}>
            {note.text}
          </span>
        )}
      </span>
      <span className="flex-shrink-0 text-sm text-bambu-gray">{detail}</span>
    </button>
  );
}

function LocalSection({
  data,
  selected,
  onClick,
  onMany,
  onRemove,
}: {
  data: ArchiveMediaSources;
  selected: Set<string>;
  onClick: ClickHandler;
  onMany: ManyHandler;
  /** Offered only for a video saved beside the original, and only to someone who may delete. */
  onRemove?: (item: ArchiveLocalVideo) => void;
}) {
  const { t } = useTranslation();
  const keys = data.local.items.map((item) => `${LOCAL}${item.asset}`);
  return (
    <section data-testid="archive-media-local">
      <SectionHeader title={t('archives.media.attached')} keys={keys} selected={selected} onMany={onMany} />
      {data.local.items.length === 0 ? (
        <p className="text-sm text-bambu-gray">{t('archives.media.attachedNone')}</p>
      ) : (
        <div className="space-y-1">
          {data.local.items.map((item) => {
            const key = `${LOCAL}${item.asset}`;
            return (
              <div key={key} className="flex items-center gap-1">
                <div className="min-w-0 flex-1">
                  <Row
                    checked={selected.has(key)}
                    name={item.name}
                    detail={formatFileSize(item.size)}
                    note={item.variant ? { text: t('archives.media.savedCopy'), tone: 'muted' } : undefined}
                    onClick={(shift) => onClick('local', keys, key, shift)}
                  />
                </div>
                {item.variant && onRemove && (
                  <button
                    type="button"
                    onClick={() => onRemove(item)}
                    title={t('archives.media.removeSaved')}
                    aria-label={t('archives.media.removeSaved')}
                    className="flex-shrink-0 rounded p-2 text-bambu-gray hover:bg-red-500/20 hover:text-red-400"
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

function RemoteSection({
  section,
  title,
  source,
  selected,
  onClick,
  onMany,
}: {
  section: 'timelapse' | 'camera';
  title: string;
  source: { status: MediaSourceStatus; items: ArchiveRemoteVideo[] };
  selected: Set<string>;
  onClick: ClickHandler;
  onMany: ManyHandler;
}) {
  const { t } = useTranslation();
  const keys = source.items.map(remoteKey);
  const note = (item: ArchiveRemoteVideo): { text: string; tone: 'match' | 'muted' } | undefined => {
    if (item.overlap === true) {
      return { text: t(item.assumed ? 'archives.media.matchAssumed' : 'archives.media.match'), tone: 'match' };
    }
    if (item.overlap === false) return { text: t('archives.media.outside'), tone: 'muted' };
    if (item.time_basis === 'unknown') return { text: t('archives.media.timeUnknown'), tone: 'muted' };
    return { text: t('archives.media.timeUnconfirmed'), tone: 'muted' };
  };
  return (
    <section data-testid={`archive-media-${section}`} data-status={source.status}>
      <SectionHeader title={title} keys={keys} selected={selected} onMany={onMany} />
      {source.status !== 'ok' ? (
        <p className="text-sm text-bambu-gray">{t(`archives.media.status.${source.status}`)}</p>
      ) : (
        <div className="space-y-1">
          {source.items.map((item) => {
            const key = remoteKey(item);
            const size = item.size != null ? formatFileSize(item.size) : '';
            return (
              <Row
                key={key}
                checked={selected.has(key)}
                name={item.name}
                detail={[fileTime(item), size].filter(Boolean).join(' · ')}
                note={note(item)}
                onClick={(shift) => onClick(section, keys, key, shift)}
              />
            );
          })}
        </div>
      )}
    </section>
  );
}
