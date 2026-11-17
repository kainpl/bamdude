import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { Link, useSearchParams } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { AlertCircle, ArrowLeft, Box, Check, ChevronLeft, ChevronRight, Download, ExternalLink, Loader2, RefreshCw, Search, Settings } from 'lucide-react';

import { api, type ModelSourceFile } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { manyfoldMarkKey, useManyfoldImport, type ManyfoldImportSummary } from '../contexts/ManyfoldImportContext';
import { formatFileSize } from '../utils/file';
import { isHttpLink } from '../utils/httpLink';
import { Button } from './Button';
import { Card, CardContent } from './Card';
import { FolderTreeSelect } from './FolderTreeSelect';
import { LoadingBlock } from './LoadingBlock';
import { useManyfoldRefusal } from './manyfoldRefusal';

const fileLabel = (file: ModelSourceFile) => file.filename || file.name;

const hasDetails = (file: ModelSourceFile) => file.filename !== null || file.size !== null;
/** The model's files with the detail batches loaded since laid over them; a detailed entry is never lost. */
function withDetails(files: ModelSourceFile[], more: Map<string, ModelSourceFile> | undefined): ModelSourceFile[] {
  if (!more || more.size === 0) return files;
  const listed = new Set(files.map((file) => file.id));
  const merged = files.map((file) => {
    const later = more.get(file.id);
    return later && (hasDetails(later) || !hasDetails(file)) ? later : file;
  });
  for (const [id, file] of more) if (!listed.has(id)) merged.push(file);
  return merged;
}

/** The route's own cap on the search words. */
const SEARCH_MAX = 200;
/** A page number out of an address: anything that is not a whole number above zero is page 1. */
function pageFrom(raw: string | null): number {
  if (!raw || !/^[0-9]{1,6}$/.test(raw)) return 1;
  return Math.max(1, Number(raw));
}

/** A model's preview from our own proxy; a model without one shows a neutral tile. */
function Preview({ modelId, className, veiled = false }: { modelId: string; className: string; veiled?: boolean }) {
  const src = api.getManyfoldPreviewUrl(modelId);
  // Broken is a fact about one address: when the media token arrives the address changes
  // and the picture is tried again (a first load that beat the token must not latch the tile).
  const [brokenSrc, setBrokenSrc] = useState<string | null>(null);
  if (brokenSrc === src) {
    return (
      <div className={`${className} flex items-center justify-center bg-bambu-dark`} aria-hidden="true">
        <Box className="w-10 h-10 text-bambu-gray/50" />
      </div>
    );
  }
  // alt="": the model's name stands beside every preview as text, and is not read twice.
  return (
    <img
      src={src}
      alt=""
      loading="lazy"
      onError={() => setBrokenSrc(src)}
      className={`${className} object-cover bg-bambu-dark${veiled ? ' blur-xl' : ''}`}
    />
  );
}

/** Why a read failed, and the way on: asking again. */
function FailureNote({ text, onRetry }: { text: string; onRetry: () => void }) {
  const { t } = useTranslation();
  return (
    <div role="alert" className="flex flex-wrap items-center gap-3 text-sm">
      <p className="text-red-400">{text}</p>
      <Button size="sm" variant="secondary" onClick={onRetry}>
        <RefreshCw className="w-3.5 h-3.5 mr-1" />
        {t('common.retry')}
      </Button>
    </div>
  );
}

/** What a run did — it stays until it is closed, wherever the user was when the run ended. */
function SummaryNote({ summary, onClose }: { summary: ManyfoldImportSummary; onClose: () => void }) {
  const { t } = useTranslation();
  const nothing = summary.total === 0 && summary.unreadable.length === 0 && summary.stopped === null;
  return (
    <div role="status" className="rounded-lg border border-bambu-dark-tertiary bg-bambu-dark-secondary p-3 text-sm space-y-1">
      <div className="flex items-start justify-between gap-3">
        <p className="text-white">
          {nothing
            ? t(summary.noPrintable ? 'manyfold.bulkNoPrintable' : 'manyfold.bulkNothing')
            : t('manyfold.bulkDone', {
                imported: summary.imported,
                existing: summary.existing,
                failed: summary.failed.length,
              })}
        </p>
        <button type="button" onClick={onClose} className="text-bambu-gray hover:text-white text-xs shrink-0">
          {t('common.close')}
        </button>
      </div>
      {summary.stopped === 'cancelled' && (
        <p className="text-yellow-400">{t('manyfold.runCancelled', { skipped: summary.skipped })}</p>
      )}
      {summary.stopped === 'refused' && (
        <p className="text-yellow-400">{t('manyfold.runRefused', { reason: summary.stopReason ?? '', skipped: summary.skipped })}</p>
      )}
      {summary.stopped === 'reloaded' && (
        <p className="text-yellow-400">{t('manyfold.runReloaded', { done: summary.done ?? 0, total: summary.total })}</p>
      )}
      {summary.failed.length > 0 && (
        <ul className="max-h-48 overflow-y-auto space-y-1">
          {summary.failed.map((entry) => (
            <li key={entry.key} className="text-red-400 break-words">
              {t('manyfold.bulkFailed', { model: entry.modelName, name: entry.name, reason: entry.reason })}
            </li>
          ))}
        </ul>
      )}
      {summary.unreadable.length > 0 && (
        <p className="text-yellow-400">{t('manyfold.modelsUnreadable', { names: summary.unreadable.join(', ') })}</p>
      )}
    </div>
  );
}

export function ManyfoldTab() {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const refusalText = useManyfoldRefusal();
  // The run belongs to the app, not to this tab: it goes on, and is found again, across navigation.
  const { progress, busy, summary, justImported, startFiles, startModels, cancel, dismissSummary } = useManyfoldImport();
  // An import adds a file to the library: the server asks for that right as well.
  const canImport = hasPermission('manyfold:import') && hasPermission('library:upload');
  const canConnect = hasPermission('settings:update');

  // The place in the catalogue lives in the address (review MF-I3): the search words, the page
  // and the open model are query parameters beside the page's own `tab`. Back returns from a
  // model to the list it was opened from, a reload restores the view, a link can be shared.
  // Every change of place is a history step (push); typing in the search box is not a place
  // and touches nothing until the search is submitted.
  const [searchParams, setSearchParams] = useSearchParams();
  const query = (searchParams.get('q') ?? '').trim().slice(0, SEARCH_MAX);
  const page = pageFrom(searchParams.get('page'));
  const openModelId = searchParams.get('model') || null;
  const goTo = (place: { q?: string; page?: number; model?: string | null }) =>
    setSearchParams((previous) => {
      const next = new URLSearchParams(previous);
      const put = (key: string, value: string | null) => (value ? next.set(key, value) : next.delete(key));
      if (place.q !== undefined) put('q', place.q);
      if (place.page !== undefined) put('page', place.page > 1 ? String(place.page) : null);
      if (place.model !== undefined) put('model', place.model);
      return next;
    });
  const [searchInput, setSearchInput] = useState(query);
  // Back and forward move the address; the box shows the search that address names.
  useEffect(() => {
    setSearchInput(query);
  }, [query]);
  const [folderId, setFolderId] = useState<number | null>(null);
  const [pickedFiles, setPickedFiles] = useState<Set<string>>(new Set());
  // Models ticked in the grid, with their names: the tick outlives the page that showed it.
  const [pickedModels, setPickedModels] = useState<Map<string, string>>(new Map());

  const status = useQuery({ queryKey: ['manyfold-status'], queryFn: () => api.getManyfoldStatus() });
  const usable = status.data?.usable === true;

  const models = useQuery({
    queryKey: ['manyfold-models', query, page],
    queryFn: () => api.listManyfoldModels(query, page),
    enabled: usable && openModelId === null,
    retry: false,
  });
  const model = useQuery({
    queryKey: ['manyfold-model', openModelId],
    queryFn: () => api.getManyfoldModel(openModelId as string),
    enabled: usable && openModelId !== null,
    retry: false,
  });
  const folders = useQuery({
    queryKey: ['library-folders'],
    queryFn: () => api.getLibraryFolders(),
    enabled: usable && canImport,
  });

  useEffect(() => {
    setPickedFiles(new Set());
  }, [openModelId]);

  // The last page that did arrive, for this search: when a page fails, the pager is drawn
  // from it, so the way back (and forward, as far as is known) survives the failure.
  const [lastPage, setLastPage] = useState<{ query: string; page: number; pages: number | null; hasNext: boolean } | null>(null);
  const arrived = models.data;
  useEffect(() => {
    if (arrived) setLastPage({ query, page: arrived.page, pages: arrived.pages, hasNext: arrived.has_next });
  }, [arrived, query]);

  // The sensitive model whose preview the user asked to see.
  const [revealed, setRevealed] = useState<string | null>(null);
  // File details of a large model arrive in batches: the model's answer carries a cursor while
  // more exist, and each further batch is asked for by the user. What was loaded stays for as
  // long as that model is open — a re-read of the model (after an import, on returning to the
  // window) does not throw the batches away.
  const [moreDetails, setMoreDetails] = useState<{
    modelId: string;
    files: Map<string, ModelSourceFile>;
    cursor: string | number | null;
  } | null>(null);
  const [loadingDetails, setLoadingDetails] = useState(false);
  const [detailsError, setDetailsError] = useState<string | null>(null);

  // What the library holds of the open model: the model's own answer, plus what this session
  // imported since that answer was read (a later read knows better — the file may be gone again).
  const modelReadAt = model.dataUpdatedAt;
  const inLibrary = useMemo(() => {
    const known = new Map<string, { library_file_id: number; folder_id: number | null }>();
    if (!model.data) return known;
    for (const file of model.data.files) {
      const mark = justImported.get(manyfoldMarkKey(model.data.id, file.id));
      if (mark && mark.at > modelReadAt) known.set(file.id, mark);
    }
    for (const [fileId, entry] of Object.entries(model.data.already_imported ?? {})) known.set(fileId, entry);
    return known;
  }, [model.data, modelReadAt, justImported]);

  /** Files one after another; a failure never stops the rest. One run at a time. */
  const importFiles = (modelId: string, modelName: string, files: ModelSourceFile[], single = false) => {
    const started = startFiles(
      files.map((file) => ({ modelId, modelName, file })),
      folderId,
      { single },
    );
    if (started) setPickedFiles(new Set());
  };

  /** Every printable file of the ticked models that the library does not hold yet. */
  const importModels = () => {
    const started = startModels(
      [...pickedModels.entries()].map(([id, name]) => ({ id, name })),
      folderId,
    );
    if (started) setPickedModels(new Map());
  };
  // Why an import control is off while a run is in flight.
  const busyTitle = busy ? t('manyfold.importRunning') : undefined;

  if (status.isLoading) return <LoadingBlock label={t('common.loading')} />;

  // "Could not be checked" is not "not connected": a failed status read says so and can be asked again.
  if (status.isError && status.data === undefined) {
    return status.isFetching ? (
      <LoadingBlock label={t('common.loading')} />
    ) : (
      <Card>
        <CardContent className="py-8 flex flex-col items-center text-center gap-3">
          <AlertCircle className="w-8 h-8 text-bambu-gray" />
          <p className="text-white font-medium">{t('manyfold.statusFailed')}</p>
          <Button size="sm" variant="secondary" onClick={() => void status.refetch()}>
            <RefreshCw className="w-3.5 h-3.5 mr-1" />
            {t('common.retry')}
          </Button>
        </CardContent>
      </Card>
    );
  }

  if (!usable) {
    return (
      <Card>
        <CardContent className="py-8 flex flex-col items-center text-center gap-2">
          <AlertCircle className="w-8 h-8 text-bambu-gray" />
          <p className="text-white font-medium">{t('manyfold.notConnectedTitle')}</p>
          <p className="text-sm text-bambu-gray">
            {t(canConnect ? 'manyfold.notConnectedAdmin' : 'manyfold.notConnectedBody')}
          </p>
          {canConnect && (
            <Link to="/settings?tab=network" className="inline-flex items-center gap-2 text-sm text-bambu-green hover:underline mt-2">
              <Settings className="w-4 h-4" />
              {t('manyfold.editConnection')}
            </Link>
          )}
        </CardContent>
      </Card>
    );
  }

  const progressNote = progress && (
    <div className="text-sm text-bambu-gray space-y-1">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <p className="flex items-center gap-2">
          <Loader2 className="w-4 h-4 animate-spin" />
          {t(progress.kind === 'reading' ? 'manyfold.collectProgress' : 'manyfold.importProgress', {
            current: progress.current,
            total: progress.total,
          })}
        </p>
        {progress.cancelling ? (
          <span role="status">{t('manyfold.cancelling')}</span>
        ) : (
          <Button size="sm" variant="secondary" onClick={cancel}>{t('manyfold.cancelImport')}</Button>
        )}
      </div>
      <p className="text-xs">{t('manyfold.keepPageOpen')}</p>
    </div>
  );

  // ---- one model ------------------------------------------------------
  if (openModelId !== null) {
    const data = model.data;
    const extra = data && moreDetails && moreDetails.modelId === data.id ? moreDetails : null;
    // Non-null only while the server says more file details exist.
    const detailCursor = extra ? extra.cursor : (data?.detail_cursor ?? null);
    const files = data ? withDetails(data.files, extra?.files) : [];
    const loadMoreDetails = async () => {
      if (!data || detailCursor === null || loadingDetails) return;
      setLoadingDetails(true);
      setDetailsError(null);
      try {
        const batch = await api.getManyfoldModel(data.id, detailCursor);
        setMoreDetails((previous) => {
          const known = new Map(previous && previous.modelId === data.id ? previous.files : []);
          for (const file of batch.files) known.set(file.id, file);
          return { modelId: data.id, files: known, cursor: batch.detail_cursor ?? null };
        });
      } catch (error) {
        setDetailsError(refusalText(error));
      } finally {
        setLoadingDetails(false);
      }
    };
    const importable = files.filter((file) => file.importable && !inLibrary.has(file.id));
    const picked = importable.filter((file) => pickedFiles.has(file.id));
    const toggleFile = (fileId: string) =>
      setPickedFiles((previous) => {
        const next = new Set(previous);
        if (next.has(fileId)) next.delete(fileId);
        else next.add(fileId);
        return next;
      });
    return (
      <div className="space-y-4">
        <button
          type="button"
          onClick={() => goTo({ model: null })}
          className="inline-flex items-center gap-2 text-sm text-bambu-gray hover:text-white"
        >
          <ArrowLeft className="w-4 h-4" />
          {t('manyfold.back')}
        </button>
        {summary && <SummaryNote summary={summary} onClose={dismissSummary} />}
        {model.isFetching && !data && <LoadingBlock label={t('common.loading')} />}
        {model.isError && !model.isFetching && (
          <FailureNote text={refusalText(model.error)} onRetry={() => void model.refetch()} />
        )}
        {data && (
          <Card>
            <CardContent className="space-y-4">
              <div className="flex flex-col sm:flex-row gap-4">
                {data.has_preview &&
                  (data.sensitive && revealed !== data.id ? (
                    // Manyfold marks this model sensitive: its picture stays blurred until it is asked for.
                    <button
                      type="button"
                      onClick={() => setRevealed(data.id)}
                      aria-label={t('manyfold.showPreview')}
                      className="relative w-full sm:w-48 h-48 rounded-lg shrink-0 overflow-hidden"
                    >
                      <Preview modelId={data.id} className="w-full h-full" veiled />
                      <span className="absolute inset-0 flex items-center justify-center p-2 text-center text-xs text-white bg-black/40">
                        {t('manyfold.sensitiveHidden')}
                      </span>
                    </button>
                  ) : (
                    <Preview modelId={data.id} className="w-full sm:w-48 h-48 rounded-lg shrink-0" />
                  ))}
                <div className="min-w-0 space-y-2">
                  <h2 className="text-xl font-semibold text-white break-words">{data.name}</h2>
                  {data.sensitive && (
                    <span className="inline-block px-2 py-0.5 rounded bg-yellow-500/20 text-yellow-300 text-xs">
                      {t('manyfold.sensitive')}
                    </span>
                  )}
                  {data.caption && <p className="text-sm text-bambu-gray break-words">{data.caption}</p>}
                  <dl className="text-sm grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
                    {data.creator && (
                      <>
                        <dt className="text-bambu-gray">{t('manyfold.creator')}</dt>
                        <dd className="text-white break-words">{data.creator}</dd>
                      </>
                    )}
                    {data.license && (
                      <>
                        <dt className="text-bambu-gray">{t('manyfold.license')}</dt>
                        <dd className="text-white break-words">{data.license}</dd>
                      </>
                    )}
                  </dl>
                  {data.tags.length > 0 && (
                    <ul className="flex flex-wrap gap-1">
                      {data.tags.map((tag) => (
                        <li key={tag} className="px-2 py-0.5 rounded bg-bambu-dark text-xs text-bambu-gray">{tag}</li>
                      ))}
                    </ul>
                  )}
                  {isHttpLink(data.url) && (
                    <a href={data.url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-sm text-bambu-green hover:underline">
                      <ExternalLink className="w-3.5 h-3.5" />
                      {t('manyfold.openInManyfold')}
                    </a>
                  )}
                </div>
              </div>

              <div className="space-y-2">
                <h3 className="text-sm font-medium text-white">{t('manyfold.files')}</h3>
                {files.length === 0 && <p className="text-sm text-bambu-gray">{t('manyfold.noFiles')}</p>}
                {canImport && importable.length > 0 && (
                  <div className="flex flex-wrap items-center gap-2">
                    <div role="group" aria-label={t('manyfold.importTo')} className="flex flex-wrap items-center gap-2">
                      <span className="text-sm text-bambu-gray" aria-hidden="true">{t('manyfold.importTo')}</span>
                      <FolderTreeSelect
                        folders={folders.data}
                        value={folderId}
                        onChange={setFolderId}
                        rootLabel={t('manyfold.folderAuto')}
                        disabled={busy}
                      />
                    </div>
                    <label className="inline-flex items-center gap-2 text-sm text-white">
                      <input
                        type="checkbox"
                        checked={picked.length === importable.length}
                        disabled={busy}
                        onChange={(event) =>
                          setPickedFiles(event.target.checked ? new Set(importable.map((file) => file.id)) : new Set())
                        }
                      />
                      {t('manyfold.selectAll')}
                    </label>
                    <Button
                      size="sm"
                      variant="secondary"
                      disabled={busy || picked.length === 0}
                      title={busyTitle}
                      onClick={() => importFiles(data.id, data.name, picked)}
                    >
                      {t('manyfold.importSelected', { count: picked.length })}
                    </Button>
                    <Button
                      size="sm"
                      disabled={busy}
                      title={busyTitle}
                      onClick={() => importFiles(data.id, data.name, importable)}
                    >
                      {t('manyfold.importAll')}
                    </Button>
                  </div>
                )}
                {progressNote}
                <ul className="divide-y divide-bambu-dark-tertiary">
                  {files.map((file) => {
                    const held = inLibrary.get(file.id);
                    const label = fileLabel(file);
                    return (
                      <li key={file.id} className="py-2 flex flex-wrap items-center gap-x-3 gap-y-1">
                        {canImport && file.importable && !held && (
                          <input
                            type="checkbox"
                            aria-label={t('manyfold.selectFile', { name: label })}
                            checked={pickedFiles.has(file.id)}
                            disabled={busy}
                            onChange={() => toggleFile(file.id)}
                          />
                        )}
                        <div className="min-w-0 flex-1">
                          <p className={`text-sm break-all ${file.importable ? 'text-white' : 'text-bambu-gray'}`}>{label}</p>
                          <p className="text-xs text-bambu-gray">
                            {[
                              file.size !== null ? formatFileSize(file.size) : null,
                              file.presupported ? t('manyfold.presupported') : null,
                              !file.importable ? t(file.available ? 'manyfold.notImportable' : 'manyfold.notAvailable') : null,
                              detailCursor !== null && !hasDetails(file) ? t('manyfold.detailsPending') : null,
                            ]
                              .filter(Boolean)
                              .join(' · ')}
                          </p>
                        </div>
                        {held ? (
                          <span className="inline-flex items-center gap-2 text-xs text-bambu-green">
                            <Check className="w-3.5 h-3.5" />
                            {t('manyfold.inLibrary')}
                            <Link
                              to={held.folder_id ? `/files?folder=${held.folder_id}` : '/files'}
                              className="text-bambu-gray hover:text-white underline"
                            >
                              {t('manyfold.showInLibrary')}
                            </Link>
                          </span>
                        ) : (
                          canImport &&
                          file.importable && (
                            <Button size="sm" variant="secondary" disabled={busy} title={busyTitle} onClick={() => importFiles(data.id, data.name, [file], true)}>
                              <Download className="w-3.5 h-3.5 mr-1" />
                              {t('manyfold.import')}
                            </Button>
                          )
                        )}
                      </li>
                    );
                  })}
                </ul>
                {detailCursor !== null && (
                  <div className="flex flex-wrap items-center gap-3 text-sm">
                    <p className="text-bambu-gray">{t('manyfold.moreDetailsNote')}</p>
                    <Button size="sm" variant="secondary" disabled={loadingDetails} onClick={() => void loadMoreDetails()}>
                      {loadingDetails && <Loader2 className="w-3.5 h-3.5 mr-1 animate-spin" />}
                      {t('manyfold.loadMoreDetails')}
                    </Button>
                    {detailsError && <p role="alert" className="text-red-400">{detailsError}</p>}
                  </div>
                )}
              </div>
            </CardContent>
          </Card>
        )}
      </div>
    );
  }

  // ---- the catalogue --------------------------------------------------
  const pageModels = models.data?.models ?? [];
  const pageAllPicked = pageModels.length > 0 && pageModels.every((entry) => pickedModels.has(entry.id));
  const toggleModel = (id: string, name: string) =>
    setPickedModels((previous) => {
      const next = new Map(previous);
      if (next.has(id)) next.delete(id);
      else next.set(id, name);
      return next;
    });
  // The pager is the server's while the page is on screen. For a page that failed it is drawn
  // from the last page that arrived for this search: back always, forward only as far as known.
  const known = lastPage && lastPage.query === query ? lastPage : null;
  const hasPrevious = models.data ? models.data.has_previous : models.isError && page > 1;
  const hasNext = models.data
    ? models.data.has_next
    : models.isError && known !== null && (known.pages !== null ? page < known.pages : known.hasNext && page <= known.page);
  const submitSearch = (event: FormEvent) => {
    event.preventDefault();
    const words = searchInput.trim();
    // The same words on the same page are no new place: ask the server again
    // (after a failure nothing else on the screen would).
    if (words === query && page === 1) void models.refetch();
    else goTo({ q: words, page: 1 });
  };

  return (
    <div className="space-y-4">
      <p className="text-bambu-gray">{t('manyfold.description')}</p>
      <form onSubmit={submitSearch} className="flex flex-wrap items-center gap-2" role="search">
        <div className="relative flex-1 min-w-48 max-w-xl">
          <Search className="w-4 h-4 text-bambu-gray absolute left-3 top-1/2 -translate-y-1/2" />
          <input
            type="search"
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
            placeholder={t('manyfold.searchPlaceholder')}
            aria-label={t('manyfold.searchPlaceholder')}
            maxLength={200}
            className="w-full pl-9 pr-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white"
          />
        </div>
        <Button type="submit" size="sm" variant="secondary">{t('manyfold.search')}</Button>
        {typeof models.data?.total === 'number' && (
          <span className="text-sm text-bambu-gray">{t('manyfold.modelCount', { count: models.data.total })}</span>
        )}
      </form>

      {summary && <SummaryNote summary={summary} onClose={dismissSummary} />}
      {progressNote}

      {/* The ticks outlive the page that showed them, so their bar does too: an empty or failed page keeps it. */}
      {canImport && (pageModels.length > 0 || pickedModels.size > 0) && (
        <div className="flex flex-wrap items-center gap-3 text-sm">
          {pageModels.length > 0 && (
            <label className="inline-flex items-center gap-2 text-white">
              <input
                type="checkbox"
                checked={pageAllPicked}
                disabled={busy}
                onChange={(event) =>
                  setPickedModels((previous) => {
                    const next = new Map(previous);
                    for (const entry of pageModels) {
                      if (event.target.checked) next.set(entry.id, entry.name);
                      else next.delete(entry.id);
                    }
                    return next;
                  })
                }
              />
              {t('manyfold.selectPage')}
            </label>
          )}
          {pickedModels.size > 0 && (
            <>
              <span className="text-bambu-gray">{t('manyfold.modelsSelected', { count: pickedModels.size })}</span>
              <div role="group" aria-label={t('manyfold.importTo')}>
                <FolderTreeSelect
                  folders={folders.data}
                  value={folderId}
                  onChange={setFolderId}
                  rootLabel={t('manyfold.folderAuto')}
                  disabled={busy}
                />
              </div>
              <Button size="sm" disabled={busy} title={busyTitle} onClick={importModels}>{t('manyfold.importModels')}</Button>
              <button type="button" disabled={busy} onClick={() => setPickedModels(new Map())} className="text-bambu-gray hover:text-white">
                {t('manyfold.clearSelection')}
              </button>
            </>
          )}
        </div>
      )}

      {models.isFetching && !models.data && <LoadingBlock label={t('common.loading')} />}
      {models.isError && !models.isFetching && (
        <FailureNote text={refusalText(models.error)} onRetry={() => void models.refetch()} />
      )}
      {models.data && pageModels.length === 0 && <p className="text-sm text-bambu-gray">{t('manyfold.noModels')}</p>}

      <ul className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6 gap-3">
        {pageModels.map((entry) => (
          <li key={entry.id} className="relative rounded-lg border border-bambu-dark-tertiary bg-bambu-dark-secondary overflow-hidden">
            {canImport && (
              <input
                type="checkbox"
                aria-label={t('manyfold.selectModel', { name: entry.name })}
                checked={pickedModels.has(entry.id)}
                disabled={busy}
                onChange={() => toggleModel(entry.id, entry.name)}
                className="absolute top-2 left-2 z-10"
              />
            )}
            <button type="button" onClick={() => goTo({ model: entry.id })} className="block w-full text-left">
              {/* A list that says a model is sensitive gets a blurred tile; its picture is shown on the model itself. */}
              <span className="block overflow-hidden">
                <Preview modelId={entry.id} className="w-full aspect-square" veiled={entry.sensitive === true} />
              </span>
              <span className="block p-2 text-sm text-white break-words">
                {entry.name}
                {entry.sensitive === true && (
                  <span className="ml-2 px-1.5 py-0.5 rounded bg-yellow-500/20 text-yellow-300 text-xs">{t('manyfold.sensitive')}</span>
                )}
              </span>
            </button>
          </li>
        ))}
      </ul>

      {(hasPrevious || hasNext) && (
        <nav className="flex items-center justify-center gap-3" aria-label={t('manyfold.pages')}>
          <Button size="sm" variant="secondary" disabled={!hasPrevious} onClick={() => goTo({ page: page - 1 })}>
            <ChevronLeft className="w-4 h-4" />
            {t('manyfold.previous')}
          </Button>
          <span className="text-sm text-bambu-gray">
            {models.data?.pages
              ? t('manyfold.pageOf', { page: models.data.page, pages: models.data.pages })
              : t('manyfold.page', { page: models.data?.page ?? page })}
          </span>
          <Button size="sm" variant="secondary" disabled={!hasNext} onClick={() => goTo({ page: page + 1 })}>
            {t('manyfold.next')}
            <ChevronRight className="w-4 h-4" />
          </Button>
        </nav>
      )}
    </div>
  );
}
