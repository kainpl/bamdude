import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, DOWNLOAD_JOB_TERMINAL, type DownloadJob, type DownloadUrl } from '../../api/client';
import { startNativeDownload } from '../../utils/nativeDownload';

/** The three calls a download job needs, bound to its scope (one printer, or one archive). */
export interface DownloadJobApi {
  get: (jobId: string) => Promise<DownloadJob>;
  cancel: (jobId: string) => Promise<DownloadJob>;
  mint: (jobId: string, acceptPartial: boolean) => Promise<DownloadUrl>;
}

const FIRST_POLL_MS = 500;
const MAX_POLL_MS = 3000;
const MAX_ERROR_POLL_MS = 10000;

export function isTerminal(job: DownloadJob | null): boolean {
  return job !== null && DOWNLOAD_JOB_TERMINAL.includes(job.phase);
}

/**
 * Follows one download job: polls it with a growing interval until it is
 * terminal, and saves its result through the browser's own download.
 *
 * `adopt(job, { autoSave })` starts following; `autoSave` saves a complete
 * result as soon as it is ready — for the dialog that started the job, never
 * for one that merely found it again. A partial result is never saved without
 * the operator's confirmation.
 *
 * Cancelling asks the server; the job reads `cancelled` only when the server
 * says its transfer has really stopped, so the phase shown here is the
 * server's, not a local guess.
 */
/**
 * The job is not this caller's to follow any more: the server no longer knows
 * it (404 — expired, a restart, another session's), or the caller lost what the
 * job needs (403 — the right to the printer it read). Neither changes by asking
 * again, so polling stops.
 */
function isGone(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 404 || error.status === 403);
}

export function useDownloadJob(api: DownloadJobApi) {
  const [job, setJob] = useState<DownloadJob | null>(null);
  /** The job can no longer be followed (see `isGone`). */
  const [lost, setLost] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const autoSave = useRef(false);
  const apiRef = useRef(api);
  apiRef.current = api;

  const jobId = job?.job_id ?? null;
  const terminal = isTerminal(job);

  useEffect(() => {
    if (jobId === null || terminal || lost) return;
    let stopped = false;
    let delay = FIRST_POLL_MS;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      try {
        const next = await apiRef.current.get(jobId);
        if (stopped) return;
        setJob(next);
        delay = Math.min(Math.round(delay * 1.5), MAX_POLL_MS);
        if (!DOWNLOAD_JOB_TERMINAL.includes(next.phase)) timer = setTimeout(tick, delay);
      } catch (error) {
        if (stopped) return;
        if (isGone(error)) {
          setLost(true);
          return;
        }
        // The server could not be asked: keep asking, more slowly. The job goes on without us.
        delay = Math.min(delay * 2, MAX_ERROR_POLL_MS);
        timer = setTimeout(tick, delay);
      }
    };
    timer = setTimeout(tick, delay);
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [jobId, terminal, lost]);

  const save = useCallback(async (acceptPartial: boolean) => {
    if (jobId === null) return;
    setSaving(true);
    setSaveError(null);
    try {
      const { url } = await apiRef.current.mint(jobId, acceptPartial);
      startNativeDownload(url);
    } catch (error) {
      if (isGone(error)) setLost(true);
      else setSaveError(error instanceof Error ? error.message : String(error));
    } finally {
      setSaving(false);
    }
  }, [jobId]);

  // The dialog that started the job saves a complete result by itself, once.
  const phase = job?.phase ?? null;
  const partial = job?.partial ?? false;
  useEffect(() => {
    if (phase === 'ready' && !partial && autoSave.current) {
      autoSave.current = false;
      void save(false);
    }
  }, [phase, partial, save]);

  const adopt = useCallback((next: DownloadJob | null, options: { autoSave?: boolean } = {}) => {
    autoSave.current = Boolean(options.autoSave);
    setLost(false);
    setSaveError(null);
    setJob(next);
  }, []);

  const cancel = useCallback(async () => {
    if (jobId === null) return;
    autoSave.current = false;
    try {
      setJob(await apiRef.current.cancel(jobId));
    } catch (error) {
      if (isGone(error)) setLost(true);
    }
  }, [jobId]);

  return { job, lost, saving, saveError, adopt, cancel, save };
}
