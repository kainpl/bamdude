/**
 * Background slice-job tracker (Phase 2 of 0.5.x slicer cycle).
 *
 * SliceModal calls `trackJob(id, kind, sourceName)` after enqueuing and
 * closes immediately. This context keeps the job-id list, polls each one,
 * and shows toasts on terminal state. Lives at app level so polling
 * continues across navigation — slicing can run in the background while
 * the user does other things.
 *
 * Each tracked job also gets a persistent toast (`slice-job-{id}`) with a
 * spinner + elapsed-time counter that updates every second so the user has
 * a continuous visual indicator while a long slice is running. The toast
 * is replaced by a transient success/error toast on terminal state.
 */
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { useQueryClient } from '@tanstack/react-query';
import { api, type SliceJobProgress, type SliceJobState, type SliceJobStatus } from '../api/client';
import { useToast } from './ToastContext';
import { AlertModal } from '../components/AlertModal';

interface TrackedJob {
  id: number;
  kind: 'libraryFile' | 'archive';
  sourceName: string;
}

interface SliceJobTrackerContextValue {
  trackJob: (id: number, kind: 'libraryFile' | 'archive', sourceName: string) => void;
  activeJobs: TrackedJob[];
}

const SliceJobTrackerContext = createContext<SliceJobTrackerContextValue | null>(null);

const POLL_INTERVAL_MS = 1500;
const TICK_INTERVAL_MS = 1000;

const toastIdFor = (jobId: number) => `slice-job-${jobId}`;

/** Decode percent-encoded characters in a filename so the toast doesn't
 * show `stormtrooper-helmet%20h2d.3mf` for files that came from a source
 * with URL-encoded names. Wrapped in try/catch because malformed encodings
 * (`%XY` where XY isn't hex) throw URIError. */
function prettifyFilename(name: string): string {
  try {
    return decodeURIComponent(name);
  } catch {
    return name;
  }
}

function formatElapsed(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const remS = s % 60;
  if (m < 60) return `${m}m ${remS}s`;
  const h = Math.floor(m / 60);
  const remM = m % 60;
  return `${h}h ${remM}m`;
}

export function SliceJobTrackerProvider({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const { showToast, showPersistentToast, dismissToast } = useToast();
  const queryClient = useQueryClient();
  const [activeJobs, setActiveJobs] = useState<TrackedJob[]>([]);
  // A failed slice surfaces as an acknowledge-only modal (see the failed
  // branch below) rather than an auto-dismissing toast.
  const [sliceError, setSliceError] = useState<{ name: string; detail: string } | null>(null);

  // Stable mutable ref so the polling effect can read the current list
  // without re-subscribing every time it changes.
  const activeJobsRef = useRef<TrackedJob[]>([]);
  activeJobsRef.current = activeJobs;

  // Per-job start time, latest phase, and latest progress snapshot, kept
  // in refs so the 1s tick doesn't need to re-render on every update.
  const startedAtRef = useRef<Map<number, number>>(new Map());
  const phaseRef = useRef<Map<number, SliceJobStatus>>(new Map());
  const progressRef = useRef<Map<number, SliceJobProgress | null>>(new Map());

  const renderProgressToast = useCallback(
    (job: TrackedJob) => {
      const startedAt = startedAtRef.current.get(job.id);
      if (startedAt == null) return;
      const elapsedSecs = (Date.now() - startedAt) / 1000;
      const phase = phaseRef.current.get(job.id) ?? 'pending';
      const elapsedStr = formatElapsed(elapsedSecs);
      const progress = progressRef.current.get(job.id) ?? null;
      // When the sidecar has emitted at least one progress frame, weave
      // the stage label + percent into the toast — that's what makes the
      // wait feel professional ("Generating G-code 75%" beats "Slicing X
      // — 47s"). Falls back to the elapsed-time-only message when there's
      // no progress yet (queued/Initializing) or sidecar is older without
      // --pipe support.
      const hasUseful = progress && progress.stage && progress.total_percent > 0;
      if (phase === 'running' && hasUseful) {
        showPersistentToast(
          toastIdFor(job.id),
          t('slice.runningWithProgress', '{{name}} — {{stage}} ({{percent}}%) — {{elapsed}}', {
            name: prettifyFilename(job.sourceName),
            stage: progress.stage,
            percent: Math.min(100, Math.max(0, Math.round(progress.total_percent))),
            elapsed: elapsedStr,
          }),
          'loading',
        );
        return;
      }
      const messageKey = phase === 'pending' ? 'slice.queuedToast' : 'slice.runningToast';
      const fallback =
        phase === 'pending'
          ? 'Queued: {{name}} — {{elapsed}}'
          : 'Slicing {{name}} — {{elapsed}}';
      showPersistentToast(
        toastIdFor(job.id),
        t(messageKey, fallback, { name: prettifyFilename(job.sourceName), elapsed: elapsedStr }),
        'loading',
      );
    },
    [showPersistentToast, t],
  );

  const trackJob = useCallback(
    (id: number, kind: 'libraryFile' | 'archive', sourceName: string) => {
      setActiveJobs((prev) => (prev.some((j) => j.id === id) ? prev : [...prev, { id, kind, sourceName }]));
      startedAtRef.current.set(id, Date.now());
      phaseRef.current.set(id, 'pending');
      progressRef.current.set(id, null);
      // Render the initial frame immediately so the user sees the toast
      // before the first tick lands (~1s delay otherwise).
      renderProgressToast({ id, kind, sourceName });
    },
    [renderProgressToast],
  );

  const completeJob = useCallback(
    (job: TrackedJob, state: SliceJobState) => {
      setActiveJobs((prev) => prev.filter((j) => j.id !== job.id));
      startedAtRef.current.delete(job.id);
      phaseRef.current.delete(job.id);
      progressRef.current.delete(job.id);

      // Replace the persistent progress toast with a transient
      // success/error toast (auto-dismisses, same as showToast).
      dismissToast(toastIdFor(job.id));

      if (state.status === 'completed') {
        showToast(
          t('slice.completedToast', 'Sliced {{name}}', { name: prettifyFilename(job.sourceName) }),
          'success',
        );
      } else if (state.status === 'failed') {
        // A failed slice surfaces as an acknowledge-only modal, not a toast:
        // the slicer's reason (e.g. "objects over the bed boundary") is
        // actionable and a 3s toast hides it before it can be read.
        setSliceError({
          name: prettifyFilename(job.sourceName),
          detail: state.error_detail || t('slice.failed', 'Slice failed'),
        });
      }

      // Refresh whichever list owns the result. Both are cheap to invalidate.
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      queryClient.invalidateQueries({ queryKey: ['archives'] });
    },
    [dismissToast, queryClient, showToast, t],
  );

  // Status polling. Updates phase on each successful poll and triggers
  // completeJob on terminal states.
  useEffect(() => {
    if (activeJobs.length === 0) return;
    let cancelled = false;
    const interval = setInterval(async () => {
      if (cancelled) return;
      const snapshot = [...activeJobsRef.current];
      for (const job of snapshot) {
        try {
          const state = await api.getSliceJob(job.id);
          phaseRef.current.set(job.id, state.status);
          // Capture the latest progress snapshot if the sidecar fed one
          // through. The 1s tick re-renders the toast off this ref.
          if (state.progress) {
            progressRef.current.set(job.id, state.progress);
          }
          if (state.status === 'completed' || state.status === 'failed') {
            completeJob(job, state);
          }
        } catch {
          // Transient poll failure — stay tracked, retry next tick.
        }
      }
    }, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [activeJobs.length, completeJob]);

  // 1Hz tick that re-renders each persistent progress toast with the
  // current elapsed time. Independent of the status poll so the counter
  // stays smooth even while the backend is slow to respond.
  useEffect(() => {
    if (activeJobs.length === 0) return;
    const tick = setInterval(() => {
      for (const job of activeJobsRef.current) {
        renderProgressToast(job);
      }
    }, TICK_INTERVAL_MS);
    return () => clearInterval(tick);
  }, [activeJobs.length, renderProgressToast]);

  return (
    <SliceJobTrackerContext.Provider value={{ trackJob, activeJobs }}>
      {children}
      {sliceError && (
        <AlertModal
          title={t('slice.failedTitle', 'Slice failed')}
          subtitle={sliceError.name}
          message={sliceError.detail}
          onClose={() => setSliceError(null)}
        />
      )}
    </SliceJobTrackerContext.Provider>
  );
}

export function useSliceJobTracker(): SliceJobTrackerContextValue {
  const ctx = useContext(SliceJobTrackerContext);
  if (!ctx) {
    throw new Error('useSliceJobTracker must be used inside SliceJobTrackerProvider');
  }
  return ctx;
}
