import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { Link } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { AlertCircle, ArrowLeft, Box, Check, ChevronLeft, ChevronRight, Download, ExternalLink, Loader2, Search, Settings } from 'lucide-react';

import { api, type ModelSourceFile } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { manyfoldMarkKey, useManyfoldImport, type ManyfoldImportSummary } from '../contexts/ManyfoldImportContext';
import { formatFileSize } from '../utils/file';
import { Button } from './Button';
import { Card, CardContent } from './Card';
import { FolderTreeSelect } from './FolderTreeSelect';
import { LoadingBlock } from './LoadingBlock';
import { useManyfoldRefusal } from './manyfoldRefusal';

const fileLabel = (file: ModelSourceFile) => file.filename || file.name;

/** A model's preview from our own proxy; a model without one shows a neutral tile. */
function Preview({ modelId, name, className }: { modelId: string; name: string; className: string }) {
  const [broken, setBroken] = useState(false);
  if (broken) {
    return (
      <div className={`${className} flex items-center justify-center bg-bambu-dark`} aria-hidden="true">
        <Box className="w-10 h-10 text-bambu-gray/50" />
      </div>
    );
  }
  return (
    <img
      src={api.getManyfoldPreviewUrl(modelId)}
      alt={name}
      loading="lazy"
      onError={() => setBroken(true)}
      className={`${className} object-cover bg-bambu-dark`}
    />
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
  const canImport = hasPermission('manyfold:import');
  const canConnect = hasPermission('settings:update');

  const [searchInput, setSearchInput] = useState('');
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [openModelId, setOpenModelId] = useState<string | null>(null);
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
    const importable = (data?.files ?? []).filter((file) => file.importable && !inLibrary.has(file.id));
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
          onClick={() => setOpenModelId(null)}
          className="inline-flex items-center gap-2 text-sm text-bambu-gray hover:text-white"
        >
          <ArrowLeft className="w-4 h-4" />
          {t('manyfold.back')}
        </button>
        {summary && <SummaryNote summary={summary} onClose={dismissSummary} />}
        {model.isLoading && <LoadingBlock label={t('common.loading')} />}
        {model.isError && <p className="text-red-400 text-sm">{refusalText(model.error)}</p>}
        {data && (
          <Card>
            <CardContent className="space-y-4">
              <div className="flex flex-col sm:flex-row gap-4">
                {data.has_preview && <Preview modelId={data.id} name={data.name} className="w-full sm:w-48 h-48 rounded-lg shrink-0" />}
                <div className="min-w-0 space-y-2">
                  <h2 className="text-xl font-semibold text-white break-words">{data.name}</h2>
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
                  {data.url && (
                    <a href={data.url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-sm text-bambu-green hover:underline">
                      <ExternalLink className="w-3.5 h-3.5" />
                      {t('manyfold.openInManyfold')}
                    </a>
                  )}
                </div>
              </div>

              <div className="space-y-2">
                <h3 className="text-sm font-medium text-white">{t('manyfold.files')}</h3>
                {data.files.length === 0 && <p className="text-sm text-bambu-gray">{t('manyfold.noFiles')}</p>}
                {canImport && importable.length > 0 && (
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm text-bambu-gray">{t('manyfold.importTo')}</span>
                    <FolderTreeSelect
                      folders={folders.data}
                      value={folderId}
                      onChange={setFolderId}
                      rootLabel={t('manyfold.folderAuto')}
                      disabled={busy}
                    />
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
                  {data.files.map((file) => {
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
  const submitSearch = (event: FormEvent) => {
    event.preventDefault();
    setQuery(searchInput.trim());
    setPage(1);
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

      {canImport && pageModels.length > 0 && (
        <div className="flex flex-wrap items-center gap-3 text-sm">
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
          {pickedModels.size > 0 && (
            <>
              <span className="text-bambu-gray">{t('manyfold.modelsSelected', { count: pickedModels.size })}</span>
              <FolderTreeSelect
                folders={folders.data}
                value={folderId}
                onChange={setFolderId}
                rootLabel={t('manyfold.folderAuto')}
                disabled={busy}
              />
              <Button size="sm" disabled={busy} title={busyTitle} onClick={importModels}>{t('manyfold.importModels')}</Button>
              <button type="button" disabled={busy} onClick={() => setPickedModels(new Map())} className="text-bambu-gray hover:text-white">
                {t('manyfold.clearSelection')}
              </button>
            </>
          )}
        </div>
      )}

      {models.isLoading && <LoadingBlock label={t('common.loading')} />}
      {models.isError && <p className="text-red-400 text-sm">{refusalText(models.error)}</p>}
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
            <button type="button" onClick={() => setOpenModelId(entry.id)} className="block w-full text-left">
              <Preview modelId={entry.id} name={entry.name} className="w-full aspect-square" />
              <span className="block p-2 text-sm text-white break-words">{entry.name}</span>
            </button>
          </li>
        ))}
      </ul>

      {models.data && (models.data.has_previous || models.data.has_next) && (
        <nav className="flex items-center justify-center gap-3" aria-label={t('manyfold.pages')}>
          <Button size="sm" variant="secondary" disabled={!models.data.has_previous} onClick={() => setPage(page - 1)}>
            <ChevronLeft className="w-4 h-4" />
            {t('manyfold.previous')}
          </Button>
          <span className="text-sm text-bambu-gray">
            {models.data.pages
              ? t('manyfold.pageOf', { page: models.data.page, pages: models.data.pages })
              : t('manyfold.page', { page: models.data.page })}
          </span>
          <Button size="sm" variant="secondary" disabled={!models.data.has_next} onClick={() => setPage(page + 1)}>
            {t('manyfold.next')}
            <ChevronRight className="w-4 h-4" />
          </Button>
        </nav>
      )}
    </div>
  );
}
