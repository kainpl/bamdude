import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api } from '../api/client';
import type {
  AutoResultEditIn,
  BedType,
  CaliMethod,
  CaliMode,
  CalibCapabilities,
  CalibFilamentIn,
  CalibrationSessionOut,
  FilamentCalibrationOut,
  ManualResultIn,
  NozzleVolumeType,
  PresetRef,
  SliceBundleSpec,
} from '../api/client';

const TOWER_MODES: CaliMode[] = [
  'temp_tower',
  'vol_speed_tower',
  'vfa_tower',
  'retraction_tower',
];

function isTowerCaliMode(m: CaliMode | undefined): boolean {
  return m != null && (TOWER_MODES as CaliMode[]).includes(m);
}

/**
 * Wizard step machine for the Filament Calibration modal.
 *
 * start → preset → running → (manualSave | coarseSave) → [running again
 * for Flow stage 2] → fineSave → finish.
 *
 * The hook owns step state; the modal renders the sub-page matching the
 * current step. Backend status changes arrive via WS calibration.*
 * events (see useWebSocket) — those invalidate the session query and the
 * effect below flips the step accordingly.
 */
export type WizardStep =
  | 'start'
  | 'preset'
  | 'verifyDownload'
  | 'running'
  | 'manualSave'
  | 'coarseSave'
  | 'fineSave'
  | 'autoSave'
  | 'towerFinish'
  | 'finish';

export interface ComputeNextStepInput {
  cali_mode?: CaliMode;
  method?: CaliMethod;
  sessionStarted?: boolean;
  sessionStatus?: CalibrationSessionOut['status'];
  stage?: number;
  skipFine?: boolean;
  savedRows?: number;
  nextSessionId?: number | null;
  isTowerMode?: boolean;
}

/** Pure helper for unit tests. */
export function computeNextStep(current: WizardStep, ctx: ComputeNextStepInput): WizardStep {
  switch (current) {
    case 'start':
      return 'preset';
    case 'preset':
      return ctx.sessionStarted ? 'running' : 'preset';
    case 'running':
      if (ctx.sessionStatus === 'saved' && ctx.isTowerMode) return 'towerFinish';
      if (ctx.sessionStatus !== 'awaiting_user_input') return 'running';
      if (ctx.method === 'auto') return 'autoSave';
      if (ctx.cali_mode === 'flow_rate') {
        return ctx.stage === 2 ? 'fineSave' : 'coarseSave';
      }
      return 'manualSave';
    case 'coarseSave':
      if (ctx.skipFine) return ctx.savedRows ? 'finish' : 'coarseSave';
      if (ctx.nextSessionId != null) return 'running';
      return 'coarseSave';
    case 'manualSave':
    case 'fineSave':
    case 'autoSave':
      return ctx.savedRows ? 'finish' : current;
    case 'towerFinish':
    case 'finish':
      return current;
    default:
      return 'start';
  }
}

interface WizardInput {
  cali_mode: CaliMode;
  method: CaliMethod;
  nozzle_diameter: number;
  nozzle_volume_type: NozzleVolumeType;
  extruder_id: number;
  filaments: CalibFilamentIn[];
  // Preset / slicer overrides (mirror CalibSliceOnlyIn). Manual modes
  // that route through the slicer sidecar (PA Tower + later phases)
  // require either bundle OR full PresetRef triplet; AUTO modes ignore.
  spec?: Record<string, number | string | boolean>;
  bundle?: SliceBundleSpec;
  printer_preset?: PresetRef;
  process_preset?: PresetRef;
  filament_presets?: PresetRef[];
  slicer?: 'orcaslicer' | 'bambu_studio';
  bed_type?: BedType;
  // Per-job dispatcher toggles (mirror PrintModal types). Forwarded
  // to PrintQueueItem so swap macros / bed-levelling / etc. run for
  // the calibration print the same way they do for a library job.
  print_options?: {
    bed_levelling: boolean;
    flow_cali: boolean;
    layer_inspect: boolean;
    timelapse: boolean;
    mesh_mode_fast_check: boolean;
    gcode_injection: boolean;
  };
  swap_macros?: {
    execute: boolean;
    events: Array<'swap_mode_start' | 'swap_mode_change_table'>;
  };
}

export function useFilamentCalibration(printerId: number, enabled: boolean) {
  const qc = useQueryClient();
  const [step, setStep] = useState<WizardStep>('start');
  const [input, setInputState] = useState<Partial<WizardInput>>({ extruder_id: 0 });
  const [sessionId, setSessionId] = useState<number | null>(null);
  const [savedRows, setSavedRows] = useState<FilamentCalibrationOut[]>([]);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const capQuery = useQuery<CalibCapabilities>({
    queryKey: ['calibration', 'capabilities', printerId],
    queryFn: () => api.getCalibrationCapabilities(printerId),
    enabled,
    staleTime: 30_000,
  });

  // "Active" sessions = running (printing) OR awaiting_user_input
  // (print done, user picks the best line / saves K). Either one
  // should surface in the resume banner after a modal reopen so the
  // operator can rejoin the wizard mid-flow. Frontend filters on
  // both statuses so we don't have to add a multi-status query
  // param to the backend.
  const awaitingQuery = useQuery<CalibrationSessionOut[]>({
    queryKey: ['calibration', 'active', printerId],
    queryFn: () => api.listActiveSessions(printerId),
    enabled,
    staleTime: 5_000,
    select: (rows) =>
      rows.filter((s) => s.status === 'running' || s.status === 'awaiting_user_input'),
  });

  // ``getCalibrationSession`` triggers ``reconcile_session_status`` on
  // the backend — running → awaiting_user_input | saved | failed when
  // the linked PrintQueueItem flips to completed/failed. Without an
  // active poll the wizard wouldn't see the flip after print finish
  // (the WS calibration-event invalidates this query, but only when
  // the reconciler has already run, which itself only runs on a
  // GET — chicken-egg). Polling every 4 s while a session is bound
  // unblocks the flip; staleTime=1s keeps it cheap when WS does
  // arrive first.
  const sessionQuery = useQuery<CalibrationSessionOut>({
    queryKey: ['calibration', 'session', sessionId],
    queryFn: () => api.getCalibrationSession(sessionId!),
    enabled: sessionId != null,
    staleTime: 1_000,
    refetchInterval: (query) => {
      const s = query.state.data;
      if (!s) return 4_000;
      // Stop polling once we're past the running stage — save / fine
      // / finish steps don't need server reconciliation, the user
      // drives them through explicit submit mutations.
      return s.status === 'running' || s.status === 'awaiting_user_input' ? 4_000 : false;
    },
  });

  const startMutation = useMutation({
    mutationFn: (body: WizardInput) =>
      api.startCalibrationSession(printerId, {
        cali_mode: body.cali_mode,
        method: body.method,
        nozzle_diameter: body.nozzle_diameter,
        nozzle_volume_type: body.nozzle_volume_type,
        extruder_id: body.extruder_id,
        filaments: body.filaments,
        ...(body.spec ? { spec: body.spec } : {}),
        ...(body.bundle ? { bundle: body.bundle } : {}),
        ...(body.printer_preset ? { printer_preset: body.printer_preset } : {}),
        ...(body.process_preset ? { process_preset: body.process_preset } : {}),
        ...(body.filament_presets ? { filament_presets: body.filament_presets } : {}),
        ...(body.slicer ? { slicer: body.slicer } : {}),
        ...(body.bed_type ? { bed_type: body.bed_type } : {}),
        ...(body.print_options ? { print_options: body.print_options } : {}),
        ...(body.swap_macros ? { swap_macros: body.swap_macros } : {}),
      }),
    onSuccess: (session) => {
      setSessionId(session.id);
      setStep('running');
      setErrorMsg(null);
      qc.invalidateQueries({ queryKey: ['calibration', 'active', printerId] });
    },
    onError: (e: Error) => setErrorMsg(e.message),
  });

  const submitManualMutation = useMutation({
    mutationFn: (body: ManualResultIn) => {
      if (sessionId == null) throw new Error('No active session');
      return api.submitManualResult(sessionId, body);
    },
    onSuccess: (out) => {
      if (out.next_session_id != null) {
        setSessionId(out.next_session_id);
        setStep('running');
      } else {
        setSavedRows(out.saved_rows);
        setStep('finish');
      }
      qc.invalidateQueries({ queryKey: ['filament-calibrations'] });
      qc.invalidateQueries({ queryKey: ['calibration', 'active', printerId] });
    },
    onError: (e: Error) => setErrorMsg(e.message),
  });

  const submitAutoMutation = useMutation({
    mutationFn: (body: { results: AutoResultEditIn[] }) => {
      if (sessionId == null) throw new Error('No active session');
      return api.submitAutoResult(sessionId, body);
    },
    onSuccess: (rows) => {
      setSavedRows(rows);
      setStep('finish');
      qc.invalidateQueries({ queryKey: ['filament-calibrations'] });
      qc.invalidateQueries({ queryKey: ['calibration', 'active', printerId] });
    },
    onError: (e: Error) => setErrorMsg(e.message),
  });

  const cancelMutation = useMutation({
    mutationFn: () =>
      sessionId != null ? api.cancelCalibrationSession(sessionId) : Promise.resolve(),
    onSuccess: () => {
      // Reset every piece of wizard context: session + step + input
      // + per-session error/saved-rows + the cached "active session"
      // query data. Without the optimistic ``setQueryData([])`` the
      // resume banner re-renders the just-cancelled session until the
      // refetch returns, which looks like nothing happened on-click.
      setSessionId(null);
      setStep('start');
      setInputState({ extruder_id: 0 });
      setSavedRows([]);
      setErrorMsg(null);
      qc.setQueryData(['calibration', 'active', printerId], []);
      qc.invalidateQueries({ queryKey: ['calibration', 'active', printerId] });
    },
    onError: (e: Error) => setErrorMsg(e.message),
  });

  // WS auto-advance: invalidate session query when calibration.* arrives
  useEffect(() => {
    if (sessionId == null) return;
    const handler = (e: Event) => {
      const ce = e as CustomEvent<{ type: string; data?: Record<string, unknown> }>;
      const sid = ce.detail?.data?.session_id;
      if (sid !== sessionId) return;
      if (ce.detail.type === 'calibration.completed') {
        qc.invalidateQueries({ queryKey: ['calibration', 'session', sessionId] });
      }
      if (ce.detail.type === 'calibration.failed') {
        setErrorMsg((ce.detail.data?.error as string) ?? 'Calibration failed');
      }
    };
    window.addEventListener('calibration-event', handler);
    return () => window.removeEventListener('calibration-event', handler);
  }, [sessionId, qc]);

  // Auto-advance running → save/towerFinish when session flips to
  // awaiting_user_input (manual/auto) or saved (tower modes go straight there).
  // Cancelled / failed sessions return the wizard to its start screen so
  // the operator doesn't get stuck at an "in progress…" page that never
  // resolves — they can kick off a fresh attempt or close the modal.
  useEffect(() => {
    const s = sessionQuery.data;
    if (!s) return;
    if (step !== 'running') return;
    if (s.status === 'cancelled' || s.status === 'failed') {
      setErrorMsg(
        s.status === 'failed'
          ? 'Calibration print failed — start a new attempt.'
          : 'Calibration print was cancelled.',
      );
      setSessionId(null);
      setStep('start');
      qc.invalidateQueries({ queryKey: ['calibration', 'active', printerId] });
      return;
    }
    if (s.status !== 'awaiting_user_input' && s.status !== 'saved') return;
    const mode = s.cali_mode as CaliMode;
    // When resuming from a prior session (user reopened the modal
    // after a reload), ``input.method`` is undefined — fall back to
    // the session's stored method so the save-step branch picks the
    // right form (auto vs manual).
    const method = input.method ?? (s.method as CaliMethod);
    setStep(
      computeNextStep('running', {
        cali_mode: mode,
        method,
        stage: s.stage,
        sessionStatus: s.status,
        isTowerMode: isTowerCaliMode(mode),
      }),
    );
  }, [sessionQuery.data, step, input.method, qc, printerId]);

  const setInput = (patch: Partial<WizardInput>) =>
    setInputState((prev) => ({ ...prev, ...patch }));

  return useMemo(
    () => ({
      step,
      setStep,
      input,
      setInput,
      capabilities: capQuery.data,
      awaitingSession: awaitingQuery.data?.[0] ?? null,
      session: sessionQuery.data,
      sessionId,
      setSessionId,
      savedRows,
      errorMsg,
      isStarting: startMutation.isPending,
      isSubmitting: submitManualMutation.isPending,
      startSession: (body: WizardInput) => startMutation.mutateAsync(body),
      submitManualResult: (body: ManualResultIn) => submitManualMutation.mutateAsync(body),
      submitAutoResult: (body: { results: AutoResultEditIn[] }) =>
        submitAutoMutation.mutateAsync(body),
      cancelSession: () => cancelMutation.mutateAsync(),
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      step,
      input,
      capQuery.data,
      awaitingQuery.data,
      sessionQuery.data,
      sessionId,
      savedRows,
      errorMsg,
      startMutation.isPending,
      submitManualMutation.isPending,
      submitAutoMutation.isPending,
    ],
  );
}
