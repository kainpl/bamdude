/** Background farm reads share a small FIFO network budget per browser tab.
 * React Query owns the cache and retry policy; this only owns admission and
 * cancellation. Commands and auth refresh never enter this queue. */
type Waiter<T> = {
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
  signal?: AbortSignal;
  onAbort?: () => void;
};

type Job<T> = {
  key: string;
  run: (signal: AbortSignal) => Promise<T>;
  controller: AbortController;
  waiters: Set<Waiter<T>>;
  started: boolean;
};

export type FarmReadCounters = {
  started: number;
  coalesced: number;
  cancelled: number;
  timedOut: number;
  inFlight: number;
  queued: number;
};

const abortError = () => new DOMException('Farm read cancelled', 'AbortError');

export class FarmReadBudget {
  private readonly jobs = new Map<string, Job<unknown>>();
  private readonly queue: Job<unknown>[] = [];
  private active = 0;
  private totals = { started: 0, coalesced: 0, cancelled: 0, timedOut: 0 };
  private readonly limit: number;
  private readonly deadlineMs: number;

  constructor(limit = 4, deadlineMs = 15_000) {
    this.limit = limit;
    this.deadlineMs = deadlineMs;
  }

  snapshot(): FarmReadCounters {
    return { ...this.totals, inFlight: this.active, queued: this.queue.length };
  }

  /** Called only when the identity changes, not for a normal token refresh. */
  reset(): void {
    for (const job of this.jobs.values()) {
      job.controller.abort();
      for (const waiter of job.waiters) {
        waiter.signal?.removeEventListener('abort', waiter.onAbort!);
        waiter.reject(abortError());
      }
      job.waiters.clear();
    }
    this.jobs.clear();
    this.queue.length = 0;
  }

  read<T>(key: string, run: (signal: AbortSignal) => Promise<T>, signal?: AbortSignal): Promise<T> {
    if (signal?.aborted) return Promise.reject(signal.reason ?? abortError());
    let job = this.jobs.get(key) as Job<T> | undefined;
    if (job) this.totals.coalesced += 1;
    else {
      job = { key, run, controller: new AbortController(), waiters: new Set(), started: false };
      this.jobs.set(key, job as Job<unknown>);
      this.queue.push(job as Job<unknown>);
    }
    const owned = job;
    const result = new Promise<T>((resolve, reject) => {
      const waiter: Waiter<T> = { resolve, reject, signal };
      waiter.onAbort = () => {
        owned.waiters.delete(waiter);
        signal?.removeEventListener('abort', waiter.onAbort!);
        this.totals.cancelled += 1;
        reject(signal?.reason ?? abortError());
        if (owned.waiters.size === 0) {
          owned.controller.abort();
          if (this.jobs.get(key) === owned) this.jobs.delete(key);
          if (!owned.started) {
            const index = this.queue.indexOf(owned as Job<unknown>);
            if (index >= 0) this.queue.splice(index, 1);
          }
        }
      };
      owned.waiters.add(waiter);
      signal?.addEventListener('abort', waiter.onAbort, { once: true });
      // Cancellation can race with listener registration.
      if (signal?.aborted) waiter.onAbort();
    });
    this.pump();
    return result;
  }

  private pump(): void {
    while (this.active < this.limit && this.queue.length) {
      const job = this.queue.shift()!;
      if (job.waiters.size === 0) continue;
      job.started = true;
      this.active += 1;
      this.totals.started += 1;
      void this.execute(job);
    }
  }

  private async execute(job: Job<unknown>): Promise<void> {
    const timeout = AbortSignal.timeout(this.deadlineMs);
    const signal = AbortSignal.any([job.controller.signal, timeout]);
    const onAbort = () => {
      if (timeout.aborted && !job.controller.signal.aborted) this.totals.timedOut += 1;
    };
    signal.addEventListener('abort', onAbort, { once: true });
    let rejectForAbort!: (reason: unknown) => void;
    const aborted = new Promise<never>((_, reject) => { rejectForAbort = reject; });
    const stopWaiting = () => rejectForAbort(signal.reason ?? abortError());
    signal.addEventListener('abort', stopWaiting, { once: true });
    try {
      // Race even a mock/transport that ignores AbortSignal: one hung read may
      // not monopolise the fleet budget forever. Fetch itself gets the signal.
      const value = await Promise.race([job.run(signal), aborted]);
      for (const waiter of job.waiters) waiter.resolve(value);
    } catch (error) {
      for (const waiter of job.waiters) waiter.reject(error);
    } finally {
      signal.removeEventListener('abort', onAbort);
      signal.removeEventListener('abort', stopWaiting);
      for (const waiter of job.waiters) waiter.signal?.removeEventListener('abort', waiter.onAbort!);
      job.waiters.clear();
      if (this.jobs.get(job.key) === job) this.jobs.delete(job.key);
      this.active -= 1;
      this.pump();
    }
  }
}

export const farmReadBudget = new FarmReadBudget();

export const farmRead = <T>(key: string, signal: AbortSignal | undefined, run: (signal: AbortSignal) => Promise<T>) =>
  farmReadBudget.read(key, run, signal);

/** useWebSocket's visible/online catch-up owns these keys; TanStack must not
 * launch a competing focus/reconnect refetch. Unrelated screens keep defaults. */
export const farmQueryResumeOptions = {
  refetchOnWindowFocus: false,
  refetchOnReconnect: false,
  refetchIntervalInBackground: false,
} as const;

// Drawn once per browser context, not on render/live packet. Different tabs
// spread their periodic GETs over a one-second window without delaying first
// load or operator-triggered actions.
export const farmPollJitterMs = Math.round(Math.random() * 1000);

/** The only retry engine for covered reads is TanStack; the scheduler never
 * retries on its own. A periodic tick after final failure is recovery, not a
 * second immediate retry loop. */
export function farmReadRetry(failureCount: number, error: unknown): boolean {
  const name = (error as { name?: string } | null)?.name;
  if (failureCount > 1 || name === 'AbortError') return false;
  const status = (error as { status?: number } | null)?.status;
  return status === undefined || status === 429 || status >= 500;
}

export function farmReadRetryDelay(_attempt: number, error: unknown): number {
  const retryAfter = (error as { retryAfterMs?: number } | null)?.retryAfterMs;
  return retryAfter ?? 500;
}

const failedPollPeriod = (normalMs: number, fetchFailureCount: number) =>
  Math.min(120_000, normalMs * 2 ** Math.max(1, Math.min(4, fetchFailureCount)));

export function farmPollInterval(
  normalMs: number,
  query: { state: { error: unknown; fetchFailureCount: number } },
): number | false {
  // TanStack's default refetchIntervalInBackground=false pauses network work
  // while hidden and resumes the *same* observer on focus. Returning false
  // here would remove its timer permanently if options changed while hidden.
  if (!query.state.error) return normalMs + farmPollJitterMs;
  return failedPollPeriod(normalMs, query.state.fetchFailureCount) + farmPollJitterMs;
}

/** Printer-status fallback: every card owns its own key, so fifty cards are
 * fifty timers. Each fetch restarts its observer's timer, and a batch resolves
 * its waiters chunk by chunk, so free-running timers drift apart and the batcher
 * receives them one or two at a time. Placing every status timer of the tab on
 * one grid lets each due printer join a single batch per tick. The due time
 * still starts from the key's own last answer, so a live WebSocket write keeps
 * deferring the REST read of that printer, exactly as the timer reset did. */
export function farmStatusPollInterval(
  normalMs: number,
  query: { state: { error: unknown; fetchFailureCount: number; dataUpdatedAt: number; errorUpdatedAt: number } },
  now = Date.now(),
): number {
  const { state } = query;
  const period = state.error ? failedPollPeriod(normalMs, state.fetchFailureCount) : normalMs;
  // Count from the last answer, success or failure. With neither yet (the first
  // read is still in flight) a full period from now: never a due-in-the-past
  // value, which would re-arm a one-millisecond interval against a failing
  // or unanswered endpoint. The slack keeps a reply to a tick's own read on the
  // next tick; rounding its arrival up would silently double the period.
  const last = Math.max(state.dataUpdatedAt, state.errorUpdatedAt) || now;
  const slack = Math.min(1_000, normalMs / 4);
  const due = Math.max(now + 1, last + period - slack);
  const tick = Math.ceil((due - farmPollJitterMs) / normalMs) * normalMs + farmPollJitterMs;
  return tick - now;
}
