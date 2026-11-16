/**
 * The Manyfold import run (review MF-I1).
 *
 * A run is a list of files imported one after another by THIS page: the server
 * imports one file per request and keeps no queue. It therefore lives at app
 * level, above the router — like the slice-job tracker — so it goes on while
 * the user is on another tab or page, and the Manyfold tab finds its progress
 * and its outcome when they come back.
 *
 * - One run at a time: a second start is refused while one is in flight.
 * - Cancel stops BEFORE the next file; the file whose request is in flight is
 *   finished and counted (the request is not aborted — the server would go on
 *   with it anyway, and an aborted request would leave its outcome unknown).
 * - A refusal no other file can get past (Manyfold switched off, not connected,
 *   credentials refused, the connection changed) stops the run the same way.
 * - A reload ends the run with the page. What it had done by then is kept in
 *   sessionStorage and told once after the reload; nothing is resumed.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { api, type ModelSourceFile } from '../api/client';
import { useManyfoldRefusal, manyfoldErrorCode } from '../components/manyfoldRefusal';
import { useToast } from './ToastContext';

export interface ManyfoldImportJob {
  modelId: string;
  modelName: string;
  file: ModelSourceFile;
}

export interface ManyfoldImportFailure {
  /** Model id + file id: two files may share a name, never this. */
  key: string;
  modelName: string;
  name: string;
  reason: string;
}

export interface ManyfoldImportSummary {
  imported: number;
  existing: number;
  failed: ManyfoldImportFailure[];
  /** Models that could not be read at all (only when whole models were imported). */
  unreadable: string[];
  /** Files the run was asked for. */
  total: number;
  /** Files never tried, when the run ended early. */
  skipped: number;
  /** Why the run ended early; null when it ran to its end. */
  stopped: null | 'cancelled' | 'refused' | 'reloaded';
  /** The refusal that stopped it, in the reader's language. */
  stopReason?: string;
  /** How many files had been handled when the page was reloaded. */
  done?: number;
  /** Whole models were asked for and none holds a printable file. */
  noPrintable: boolean;
}

export interface ManyfoldImportProgress {
  kind: 'reading' | 'importing';
  current: number;
  total: number;
  cancelling: boolean;
}

export interface ManyfoldImportedMark {
  library_file_id: number;
  folder_id: number | null;
  /** When it was imported — a model read after this moment knows better. */
  at: number;
}

interface ManyfoldImportContextValue {
  progress: ManyfoldImportProgress | null;
  busy: boolean;
  summary: ManyfoldImportSummary | null;
  /** Files this session imported, by `markKey`. */
  justImported: ReadonlyMap<string, ManyfoldImportedMark>;
  /** Import these files. `single` reports with a toast instead of a summary. False when a run is in flight. */
  startFiles: (jobs: ManyfoldImportJob[], folderId: number | null, options?: { single?: boolean }) => boolean;
  /** Import every printable file of these models that the library does not hold. */
  startModels: (models: { id: string; name: string }[], folderId: number | null) => boolean;
  cancel: () => void;
  dismissSummary: () => void;
}

const ManyfoldImportContext = createContext<ManyfoldImportContextValue | null>(null);

const TOAST_ID = 'manyfold-import';
export const MANYFOLD_IMPORT_STORAGE_KEY = 'bamdude.manyfoldImport';
/** The failure lines kept for the after-reload note. */
const STORED_FAILURES = 50;

/** Refusals that would be repeated, word for word, for every file still to come. */
const RUN_STOPPING_CODES: ReadonlySet<string> = new Set([
  'manyfold_disabled',
  'manyfold_not_configured',
  'manyfold_credentials',
  'source_connection_changed',
]);

export const manyfoldMarkKey = (modelId: string, fileId: string) => `${modelId}:${fileId}`;
const fileLabel = (file: ModelSourceFile) => file.filename || file.name;

interface StoredRun {
  done: number;
  total: number;
  imported: number;
  existing: number;
  failed: ManyfoldImportFailure[];
  unreadable: string[];
}

function storeRun(run: StoredRun | null) {
  try {
    if (run === null) window.sessionStorage.removeItem(MANYFOLD_IMPORT_STORAGE_KEY);
    else window.sessionStorage.setItem(MANYFOLD_IMPORT_STORAGE_KEY, JSON.stringify(run));
  } catch {
    // Storage is a convenience: without it a reload simply tells nothing.
  }
}

/** What a run cut short by a reload had done — read once, then forgotten. */
function takeInterruptedRun(): ManyfoldImportSummary | null {
  try {
    const raw = window.sessionStorage.getItem(MANYFOLD_IMPORT_STORAGE_KEY);
    if (!raw) return null;
    window.sessionStorage.removeItem(MANYFOLD_IMPORT_STORAGE_KEY);
    const run = JSON.parse(raw) as Partial<StoredRun>;
    const done = Number(run.done) || 0;
    const total = Number(run.total) || 0;
    return {
      imported: Number(run.imported) || 0,
      existing: Number(run.existing) || 0,
      failed: Array.isArray(run.failed) ? run.failed : [],
      unreadable: Array.isArray(run.unreadable) ? run.unreadable : [],
      total,
      done,
      skipped: Math.max(0, total - done),
      stopped: 'reloaded',
      noPrintable: false,
    };
  } catch {
    return null;
  }
}

export function ManyfoldImportProvider({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const { showToast, showPersistentToast, dismissToast } = useToast();
  const queryClient = useQueryClient();
  const refusalText = useManyfoldRefusal();

  const [progress, setProgress] = useState<ManyfoldImportProgress | null>(null);
  const [summary, setSummary] = useState<ManyfoldImportSummary | null>(takeInterruptedRun);
  const [justImported, setJustImported] = useState<Map<string, ManyfoldImportedMark>>(new Map());
  // The run's own facts. State is for showing; these decide.
  const running = useRef(false);
  const cancelled = useRef(false);

  const showProgress = useCallback(
    (kind: ManyfoldImportProgress['kind'], current: number, total: number) => {
      setProgress({ kind, current, total, cancelling: cancelled.current });
      showPersistentToast(
        TOAST_ID,
        t(kind === 'reading' ? 'manyfold.toastReading' : 'manyfold.toastImporting', { current, total }),
        'loading',
      );
    },
    [showPersistentToast, t],
  );

  const refreshLibrary = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['library-folders'] });
    queryClient.invalidateQueries({ queryKey: ['library-files'] });
    queryClient.invalidateQueries({ queryKey: ['manyfold-model'] });
  }, [queryClient]);

  const importFiles = useCallback(
    async (jobs: ManyfoldImportJob[], folderId: number | null, base: Pick<ManyfoldImportSummary, 'unreadable' | 'noPrintable'>, single: boolean) => {
      const result: ManyfoldImportSummary = {
        imported: 0,
        existing: 0,
        failed: [],
        total: jobs.length,
        skipped: 0,
        stopped: null,
        ...base,
      };
      const remember = (done: number) =>
        storeRun({
          done,
          total: jobs.length,
          imported: result.imported,
          existing: result.existing,
          failed: result.failed.slice(0, STORED_FAILURES),
          unreadable: result.unreadable,
        });
      for (const [index, job] of jobs.entries()) {
        if (cancelled.current) {
          result.stopped = 'cancelled';
          result.skipped = jobs.length - index;
          break;
        }
        showProgress('importing', index + 1, jobs.length);
        remember(index);
        try {
          const done = await api.importManyfoldFile(job.modelId, job.file.id, folderId);
          if (done.was_existing) result.existing += 1;
          else result.imported += 1;
          setJustImported((previous) =>
            new Map(previous).set(manyfoldMarkKey(job.modelId, job.file.id), {
              library_file_id: done.library_file_id,
              folder_id: done.folder_id,
              at: Date.now(),
            }),
          );
          if (single) {
            showToast(t(done.was_existing ? 'manyfold.alreadyInLibrary' : 'manyfold.imported', { filename: done.filename }), 'success');
          }
        } catch (error) {
          const reason = refusalText(error);
          if (single) showToast(reason, 'error');
          result.failed.push({
            key: manyfoldMarkKey(job.modelId, job.file.id),
            modelName: job.modelName,
            name: fileLabel(job.file),
            reason,
          });
          const code = manyfoldErrorCode(error);
          if (code && RUN_STOPPING_CODES.has(code) && index + 1 < jobs.length) {
            result.stopped = 'refused';
            result.stopReason = reason;
            result.skipped = jobs.length - index - 1;
            break;
          }
        }
      }
      return result;
    },
    [refusalText, showProgress, showToast, t],
  );

  const finish = useCallback(
    (result: ManyfoldImportSummary | null) => {
      running.current = false;
      cancelled.current = false;
      storeRun(null);
      setProgress(null);
      refreshLibrary();
      if (result === null) {
        dismissToast(TOAST_ID);
        return;
      }
      setSummary(result);
      const clean = result.failed.length === 0 && result.unreadable.length === 0 && result.stopped === null;
      // Stays until dismissed: the user may be on any page when the run ends.
      showPersistentToast(
        TOAST_ID,
        t('manyfold.toastDone', { imported: result.imported, existing: result.existing, failed: result.failed.length }),
        clean ? 'success' : 'warning',
      );
    },
    [dismissToast, refreshLibrary, showPersistentToast, t],
  );

  const begin = useCallback(() => {
    if (running.current) return false;
    running.current = true;
    cancelled.current = false;
    setSummary(null);
    return true;
  }, []);

  const startFiles = useCallback<ManyfoldImportContextValue['startFiles']>(
    (jobs, folderId, options) => {
      if (jobs.length === 0 || !begin()) return false;
      const single = options?.single === true;
      void importFiles(jobs, folderId, { unreadable: [], noPrintable: false }, single)
        .then((result) => finish(single ? null : result))
        .catch(() => finish(null));
      return true;
    },
    [begin, finish, importFiles],
  );

  const startModels = useCallback<ManyfoldImportContextValue['startModels']>(
    (models, folderId) => {
      if (models.length === 0 || !begin()) return false;
      const collect = async () => {
        const jobs: ManyfoldImportJob[] = [];
        const unreadable: string[] = [];
        let printable = 0;
        let unread = 0;
        for (const [index, entry] of models.entries()) {
          if (cancelled.current) {
            unread = models.length - index;
            break;
          }
          showProgress('reading', index + 1, models.length);
          try {
            const read = await api.getManyfoldModel(entry.id);
            for (const file of read.files) {
              if (!file.importable) continue;
              printable += 1;
              if (!read.already_imported[file.id]) jobs.push({ modelId: entry.id, modelName: read.name || entry.name, file });
            }
          } catch {
            unreadable.push(entry.name);
          }
        }
        const result = await importFiles(
          jobs,
          folderId,
          { unreadable, noPrintable: printable === 0 && unread === 0 && unreadable.length < models.length },
          false,
        );
        // Cancelled while the models were still being read: nothing of theirs was tried.
        if (unread > 0 && result.stopped === null) result.stopped = 'cancelled';
        return result;
      };
      void collect()
        .then((result) => finish(result))
        .catch(() => finish(null));
      return true;
    },
    [begin, finish, importFiles, showProgress],
  );

  const cancel = useCallback(() => {
    if (!running.current) return;
    cancelled.current = true;
    setProgress((previous) => (previous ? { ...previous, cancelling: true } : previous));
  }, []);

  const dismissSummary = useCallback(() => {
    setSummary(null);
    if (!running.current) dismissToast(TOAST_ID);
  }, [dismissToast]);

  // The note of a run a reload cut short is also said where the user is now.
  const interrupted = useRef(summary?.stopped === 'reloaded' ? summary : null);
  useEffect(() => {
    const run = interrupted.current;
    if (!run) return;
    interrupted.current = null;
    showPersistentToast(TOAST_ID, t('manyfold.runReloaded', { done: run.done ?? 0, total: run.total }), 'warning');
  }, [showPersistentToast, t]);

  const value = useMemo<ManyfoldImportContextValue>(
    () => ({ progress, busy: progress !== null, summary, justImported, startFiles, startModels, cancel, dismissSummary }),
    [progress, summary, justImported, startFiles, startModels, cancel, dismissSummary],
  );

  return <ManyfoldImportContext.Provider value={value}>{children}</ManyfoldImportContext.Provider>;
}

export function useManyfoldImport(): ManyfoldImportContextValue {
  const context = useContext(ManyfoldImportContext);
  if (!context) throw new Error('useManyfoldImport must be used inside ManyfoldImportProvider');
  return context;
}
