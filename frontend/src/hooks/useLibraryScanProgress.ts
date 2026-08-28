import { useCallback, useEffect, useState } from 'react';

/**
 * What one external-folder scan is doing, as the tab knows it.
 *
 * ⚠️ Kept per FOLDER, not globally. The duplicate guard is per folder, so two
 * folders can legitimately be scanning at once, and the page shows one of them.
 */
export interface LibraryScanState {
  jobId: number;
  status: 'running' | 'finished' | 'failed';
  total: number;
  seen: number;
  added: number;
  updated: number;
  removed: number;
  skippedDeletions: boolean;
  error?: string | null;
}

interface ScanEventDetail {
  data?: {
    job_id?: number;
    folder_id?: number;
    status?: string;
    total?: number;
    files_seen?: number;
    files_added?: number;
    files_updated?: number;
    files_removed?: number;
    skipped_deletions?: boolean;
    error?: string | null;
  };
}

function toState(data: NonNullable<ScanEventDetail['data']>, status: LibraryScanState['status']): LibraryScanState {
  return {
    jobId: data.job_id ?? 0,
    status,
    total: data.total ?? 0,
    seen: data.files_seen ?? 0,
    added: data.files_added ?? 0,
    updated: data.files_updated ?? 0,
    removed: data.files_removed ?? 0,
    skippedDeletions: Boolean(data.skipped_deletions),
    error: data.error ?? null,
  };
}

/**
 * Follows external-folder scans over the WebSocket.
 *
 * The scan used to be the request: you pressed the button and the answer came
 * back with the counts, minutes later. It is a background job now, so this is
 * the only place the numbers arrive.
 *
 * `onFinished` fires once per ending — the page uses it for the toast, and this
 * hook keeps no history beyond the last state per folder.
 */
export function useLibraryScanProgress(onFinished?: (folderId: number, state: LibraryScanState) => void) {
  const [scans, setScans] = useState<Record<number, LibraryScanState>>({});

  useEffect(() => {
    const progress = (event: Event) => {
      const data = (event as CustomEvent<ScanEventDetail>).detail?.data;
      if (!data || typeof data.folder_id !== 'number') return;
      setScans((current) => ({ ...current, [data.folder_id as number]: toState(data, 'running') }));
    };

    const finished = (event: Event) => {
      const data = (event as CustomEvent<ScanEventDetail>).detail?.data;
      // ⚠️ A failure that cannot name its folder still has to be reported, but
      // it cannot clear a strip — the worker always sends folder_id for exactly
      // this reason, and dropping the event silently would be the spinning-
      // forever bug again.
      if (!data || typeof data.folder_id !== 'number') return;
      const folderId = data.folder_id;
      const state = toState(data, data.status === 'failed' ? 'failed' : 'finished');
      setScans((current) => ({ ...current, [folderId]: state }));
      onFinished?.(folderId, state);
    };

    window.addEventListener('library-scan-progress', progress);
    window.addEventListener('library-scan-finished', finished);
    return () => {
      window.removeEventListener('library-scan-progress', progress);
      window.removeEventListener('library-scan-finished', finished);
    };
  }, [onFinished]);

  /** Show the strip the moment the button is pressed, before the first event. */
  const markStarted = useCallback((folderId: number, jobId: number) => {
    setScans((current) => ({
      ...current,
      [folderId]: {
        jobId,
        status: 'running',
        total: 0,
        seen: 0,
        added: 0,
        updated: 0,
        removed: 0,
        skippedDeletions: false,
        error: null,
      },
    }));
  }, []);

  const dismiss = useCallback((folderId: number) => {
    setScans((current) => {
      const next = { ...current };
      delete next[folderId];
      return next;
    });
  }, []);

  return { scans, markStarted, dismiss };
}
