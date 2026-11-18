import { useCallback, useEffect, useState } from 'react';
import { ApiError, api, TIMELAPSE_EDIT_TERMINAL, type TimelapseEditJob } from '../../api/client';

const FIRST_POLL_MS = 500;
const MAX_POLL_MS = 2000;
const MAX_ERROR_POLL_MS = 10000;

/** The server no longer knows the job (404) or the caller lost what it needs (403): asking again changes nothing. */
function isGone(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 404 || error.status === 403);
}

export function isEditActive(job: TimelapseEditJob | null): boolean {
  return job !== null && !TIMELAPSE_EDIT_TERMINAL.includes(job.phase);
}

/**
 * Follows one background edit of an archive's video.
 *
 * On mount it asks the server for this caller's own edits of the archive and
 * picks up the one still running — the editor can be closed and opened again
 * without losing sight of an encode. The phase and the progress shown are the
 * server's: an edit reads `cancelled` only when the server says its encoder
 * has stopped, and `done` only when the result is published.
 */
export function useTimelapseEditJob(archiveId: number) {
  const [job, setJob] = useState<TimelapseEditJob | null>(null);
  const [lost, setLost] = useState(false);

  useEffect(() => {
    let ignore = false;
    api
      .listTimelapseEditJobs(archiveId)
      .then(({ jobs }) => {
        if (ignore) return;
        const running = [...jobs].reverse().find((item) => isEditActive(item));
        if (running) setJob((current) => current ?? running);
      })
      .catch(() => {
        // Nothing to pick up.
      });
    return () => {
      ignore = true;
    };
  }, [archiveId]);

  const jobId = job?.job_id ?? null;
  const active = isEditActive(job);

  useEffect(() => {
    if (jobId === null || !active || lost) return;
    let stopped = false;
    let delay = FIRST_POLL_MS;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      try {
        const next = await api.getTimelapseEditJob(archiveId, jobId);
        if (stopped) return;
        setJob(next);
        delay = Math.min(Math.round(delay * 1.5), MAX_POLL_MS);
        if (isEditActive(next)) timer = setTimeout(tick, delay);
      } catch (error) {
        if (stopped) return;
        if (isGone(error)) {
          setLost(true);
          return;
        }
        // The server could not be asked: keep asking, more slowly. The edit goes on without us.
        delay = Math.min(delay * 2, MAX_ERROR_POLL_MS);
        timer = setTimeout(tick, delay);
      }
    };
    timer = setTimeout(tick, delay);
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [archiveId, jobId, active, lost]);

  const adopt = useCallback((next: TimelapseEditJob | null) => {
    setLost(false);
    setJob(next);
  }, []);

  const cancel = useCallback(async () => {
    if (jobId === null) return;
    try {
      setJob(await api.cancelTimelapseEditJob(archiveId, jobId));
    } catch (error) {
      if (isGone(error)) setLost(true);
    }
  }, [archiveId, jobId]);

  return { job, lost, active: active && !lost, adopt, cancel };
}
