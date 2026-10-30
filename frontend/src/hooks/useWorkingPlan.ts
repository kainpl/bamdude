import { useCallback, useEffect, useRef, useState } from 'react';
import { api, type WorkingEntry, type WorkingPlan } from '../api/client';

export const workingSignature = (entries: WorkingEntry[] | undefined) => JSON.stringify(entries?.map(e => [
  e.entry_id, e.line_id, e.line_token ?? null, e.plate_id, e.count, e.target?.kind ?? 'auto', e.target?.kind === 'printer' ? e.target.printer_id : null,
  e.source_token ?? null, e.manual, e.protected_count,
]));

function identified(entries: WorkingEntry[], stored: WorkingEntry[]) {
  const known = new Map(stored.map(e => [e.entry_id, e]));
  return entries.map(entry => {
    const old = known.get(entry.entry_id);
    if (!old || old.line_id !== entry.line_id || old.plate_id !== entry.plate_id) return entry;
    return { ...entry, line_token: entry.line_token ?? old.line_token, source_token: entry.source_token ?? old.source_token };
  });
}

/** One serial writer per editor. A conflict keeps local input and stops writes. */
export function useWorkingPlan(orderId: number, enabled: boolean, onCommit?: () => void) {
  const [state, setState] = useState<WorkingPlan>();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<Error>();
  const [baselineChanged, setBaselineChanged] = useState(false);
  const [generation, setGeneration] = useState(0);
  const [recovering, setRecovering] = useState(false);
  const [, renderLocalChange] = useState(0);
  const current = useRef<WorkingPlan | undefined>(undefined);
  const desired = useRef<WorkingEntry[] | undefined>(undefined);
  const editSequence = useRef(0);
  const inflight = useRef<Promise<WorkingPlan> | null>(null);
  const reading = useRef<Promise<WorkingPlan | undefined> | null>(null);
  const recovery = useRef<Promise<WorkingPlan> | null>(null);
  const readSequence = useRef(0);
  const blocked = useRef(false);
  const confirming = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const mounted = useRef(true);
  const committed = useRef(onCommit);
  committed.current = onCommit;
  const accept = useCallback((value: WorkingPlan, confirmed = false, ownWrite = false) => {
    if (!confirmed && current.current && (value.revision < current.current.revision ||
      value.revision === current.current.revision && value.saved_revision < current.current.saved_revision)) return value;
    if (!confirmed && current.current && value.saved_revision !== current.current.saved_revision) setBaselineChanged(true);
    if (!ownWrite && current.current && (confirmed || value.revision !== current.current.revision || value.epoch !== current.current.epoch)) {
      if (mounted.current) setGeneration(value => value + 1);
    }
    current.current = value;
    if (desired.current) desired.current = identified(desired.current, value.entries);
    if (mounted.current) setState(value);
    return value;
  }, []);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; desired.current = undefined; clearTimeout(timer.current); };
  }, []);
  const reload = useCallback((): Promise<WorkingPlan> => {
    if (recovery.current) return recovery.current;
    clearTimeout(timer.current);
    setRecovering(true);
    const task = (async () => {
      if (inflight.current) await inflight.current.catch(() => undefined);
      const sequence = ++readSequence.current;
      reading.current = null; // A superseded background promise cannot be reused.
      const value = await api.getWorkingPlan(orderId);
      if (!mounted.current || sequence !== readSequence.current) throw new Error('workingPlan.conflict');
      // Explicit recovery is authoritative even after restore lowered revision.
      // accept's generation disables old overlays in this same React commit.
      blocked.current = false;
      desired.current = undefined;
      setError(undefined);
      setBaselineChanged(false);
      return accept(value, true);
    })().finally(() => {
      if (recovery.current === task) recovery.current = null;
      if (mounted.current) setRecovering(false);
    });
    recovery.current = task;
    return task;
  }, [orderId, accept]);
  const refreshInputs = useCallback(({ reconcile = false } = {}): Promise<WorkingPlan | undefined> => {
    if (recovery.current) return Promise.resolve(undefined);
    if (reading.current && !reconcile) return reading.current;
    const previous = reading.current;
    const sequence = ++readSequence.current;
    const read = () => {
      if (!mounted.current || sequence !== readSequence.current || recovery.current) return Promise.resolve(undefined);
      const before = current.current?.revision;
      return api.getWorkingPlan(orderId).then(value => {
        if (!mounted.current || sequence !== readSequence.current) return;
        if (current.current && current.current.revision !== before && value.revision <= current.current.revision) return;
        if (current.current && value.revision !== current.current.revision) {
          if (!desired.current) { accept(value); return current.current; }
          blocked.current = true;
          setError(new Error('workingPlan.conflict'));
          return;
        }
        accept(value); // Keep unacknowledged local input; refresh its context.
        return current.current;
      });
    };
    // A reconciliation owns a read started after the observed mismatch, not
    // an older focus snapshot. Keep the writer fenced across both reads.
    const task = (previous ? previous.catch(() => undefined).then(read) : read()).catch((e: Error) => {
      if (reconcile && mounted.current && sequence === readSequence.current) {
        blocked.current = true;
        setError(e);
      }
      throw e;
    }).finally(() => { if (reading.current === task) reading.current = null; });
    reading.current = task;
    return task;
  }, [orderId, accept]);
  useEffect(() => {
    if (!enabled) return;
    const focus = () => { if (!inflight.current && !confirming.current) void refreshInputs().catch(() => undefined); };
    window.addEventListener('focus', focus);
    return () => window.removeEventListener('focus', focus);
  }, [enabled, refreshInputs]);
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    const sequence = ++readSequence.current;
    void api.getWorkingPlan(orderId).then(value => { if (alive && sequence === readSequence.current) accept(value); })
      .catch(e => { if (alive && sequence === readSequence.current) setError(e); });
    return () => { alive = false; };
  }, [orderId, enabled, accept]);
  const flush = useCallback(async (): Promise<WorkingPlan> => {
    clearTimeout(timer.current);
    if (inflight.current) {
      await inflight.current;
      if (!mounted.current) return current.current!;
      return flush();
    }
    // A known input read fences only its own lifetime, not a token mismatch
    // with another cached reader. Resume the same desired entries afterwards.
    if (reading.current) {
      await reading.current.catch(() => undefined);
      return flush();
    }
    if (!mounted.current) return current.current!;
    if (!current.current || blocked.current || recovery.current) throw new Error('workingPlan.conflict');
    if (confirming.current) return current.current;
    const entries = desired.current;
    if (!entries) return current.current;
    if (current.current.revision > 0 && workingSignature(entries) === workingSignature(current.current.entries)) {
      desired.current = undefined;
      if (mounted.current) renderLocalChange(value => value + 1);
      return current.current;
    }
    const before = current.current;
    setSaving(true);
    const task = api.writeWorkingPlan(orderId, { expected_revision: before.revision, entries, inputs_token: before.inputs_token })
      .then(value => {
        const acknowledged = desired.current === entries;
        accept(value, false, true);
        if (acknowledged) desired.current = undefined;
        committed.current?.(); return value;
      }).catch((e: Error) => {
        blocked.current = true;
        if (mounted.current) setError(e);
        throw e;
      }).finally(() => {
        inflight.current = null;
        if (mounted.current) setSaving(false);
      });
    inflight.current = task;
    await task;
    return flush();
  }, [accept, orderId]);
  const change = useCallback((entries: WorkingEntry[]) => {
    if (recovery.current) return;
    editSequence.current += 1;
    desired.current = identified(entries, current.current?.entries ?? []);
    renderLocalChange(value => value + 1);
    clearTimeout(timer.current);
    if (blocked.current) return;
    timer.current = setTimeout(() => { void flush().catch(() => undefined); }, 600);
  }, [flush]);
  const save = useCallback(async (entries: WorkingEntry[]) => {
    // The click names one payload. Later input stays desired and is autosaved
    // after this acknowledgment, never substituted into the baseline.
    if (confirming.current || blocked.current || recovery.current) throw new Error('workingPlan.conflict');
    confirming.current = true;
    clearTimeout(timer.current);
    const captured = structuredClone(entries);
    const saveSequence = editSequence.current;
    const savesDesired = workingSignature(desired.current) === workingSignature(captured);
    const expected = current.current;
    try {
      if (inflight.current) await inflight.current;
      if (reading.current) await reading.current.catch(() => undefined);
      if (!current.current || blocked.current || recovery.current) throw new Error('workingPlan.conflict');
      let value = current.current;
      if (!value.revision || workingSignature(value.entries) !== workingSignature(captured)) {
        value = accept(await api.writeWorkingPlan(orderId, { expected_revision: value.revision, inputs_token: value.inputs_token, entries: captured }), false, true);
      }
      const result = accept(await api.actWorkingPlan(orderId, 'save', { ...value,
        saved_revision: expected?.saved_revision ?? value.saved_revision,
        epoch: expected?.epoch ?? value.epoch,
      }), true, true);
      if (editSequence.current === saveSequence && savesDesired) desired.current = undefined;
      committed.current?.();
      return result;
    } catch (e) {
      blocked.current = true;
      setError(e as Error);
      throw e;
    } finally {
      confirming.current = false;
      if (!blocked.current && mounted.current) timer.current = setTimeout(() => { void flush().catch(() => undefined); }, 0);
    }
  }, [accept, orderId, flush]);
  const dirty = !!desired.current && workingSignature(desired.current) !== workingSignature(state?.entries);
  return { state, saving, recovering, error, dirty, baselineChanged, generation, change, flush, save, accept, reload, refreshInputs };
}
