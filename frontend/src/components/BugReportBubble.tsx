import { useState, useRef, useCallback, useEffect } from 'react';
import { Bug, X, Loader2, CheckCircle, AlertCircle, AlertTriangle, Trash2, Upload, Circle, CheckCircle2, Stethoscope } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { api, bugReportApi, supportApi, type PrinterDiagnosticResult } from '../api/client';
import { DiagnosticChecklist } from './ConnectionDiagnostic';
import { SystemHealthPanel } from './SystemHealthPanel';
import { Collapsible } from './Collapsible';

/** One scanned printer paired with its name — the diagnostic result alone
 *  carries no name, and the bug-report panel lists affected printers by name. */
type DiagnosticEntry = { name: string; result: PrinterDiagnosticResult };

/**
 * `review` sits between stopping the recording and sending the report (upstream #3363): Stop used
 * to send at once, and so did the five-minute cap, with nobody clicking anything. A report goes to
 * a public issue tracker, so it leaves only on its own explicit click.
 */
type ViewState = 'form' | 'logging' | 'stopping' | 'review' | 'submitting' | 'success' | 'error';

const REPORT_STATUS_STYLES: Record<string, string> = {
  submitted: 'bg-bambu-dark-tertiary text-bambu-gray',
  open: 'bg-blue-500/20 text-blue-600 dark:text-blue-400',
  closed: 'bg-green-500/20 text-green-700 dark:text-green-400',
  not_planned: 'bg-bambu-dark-tertiary text-bambu-gray-dark line-through',
  failed: 'bg-red-500/20 text-red-600 dark:text-red-400',
};


const MAX_DIMENSION = 1920;
const JPEG_QUALITY = 0.7;
const MAX_LOG_SECONDS = 300; // 5 minutes

/**
 * A recording outlives the panel that started it (upstream #2847).
 *
 * Step 2 asks the user to reproduce the problem, and the panel sits over the
 * part of the app they have to reach to do that. Closing it has to be allowed,
 * so the run is written down rather than held only in component state: the
 * panel reopens on the step it left, and a reload lands there too instead of
 * leaving the server at DEBUG with nothing in the UI still tracking it.
 *
 * The screenshot is deliberately not stored — a 1920px JPEG runs to hundreds of
 * kilobytes against an origin-wide budget — and it survives a close anyway.
 */
const SESSION_KEY = 'bamdude-bug-report-session';

interface LoggingSession {
  description: string;
  email: string;
  /** Debug logging was already on before this run, so stopping must leave it on. */
  wasDebug: boolean;
  /** Wall clock. Elapsed is derived from it rather than counted in ticks, which
   *  a background tab throttles — the 5-minute cap has to mean five minutes. */
  startedAt: number;
}

function readSession(): LoggingSession | null {
  try {
    const raw = window.localStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<LoggingSession>;
    if (typeof parsed?.startedAt !== 'number') return null;
    return {
      description: typeof parsed.description === 'string' ? parsed.description : '',
      email: typeof parsed.email === 'string' ? parsed.email : '',
      wasDebug: parsed.wasDebug === true,
      startedAt: parsed.startedAt,
    };
  } catch {
    // Unparseable or unreadable: no session, rather than a panel that cannot restore.
    return null;
  }
}

function writeSession(session: LoggingSession): void {
  try {
    window.localStorage.setItem(SESSION_KEY, JSON.stringify(session));
  } catch {
    // Quota, or storage refused. The run still survives a close; only a reload loses it.
  }
}

function clearSession(): void {
  try {
    window.localStorage.removeItem(SESSION_KEY);
  } catch {
    // See writeSession.
  }
}

function compressImage(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      let { width, height } = img;
      if (width > MAX_DIMENSION || height > MAX_DIMENSION) {
        const scale = MAX_DIMENSION / Math.max(width, height);
        width = Math.round(width * scale);
        height = Math.round(height * scale);
      }
      const canvas = document.createElement('canvas');
      canvas.width = width;
      canvas.height = height;
      const ctx = canvas.getContext('2d');
      if (!ctx) { reject(new Error('No canvas context')); return; }
      ctx.drawImage(img, 0, 0, width, height);
      const dataUrl = canvas.toDataURL('image/jpeg', JPEG_QUALITY);
      resolve(dataUrl.replace(/^data:[^;]+;base64,/, ''));
    };
    img.onerror = reject;
    img.src = URL.createObjectURL(file);
  });
}

function formatElapsed(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
}

interface BugReportBubbleProps {
  /**
   * Render the floating disc in the bottom-right corner. False when the
   * trigger lives somewhere else — the compact header does this, so the panel
   * still mounts here while the button that opens it sits in the header.
   *
   * ⚠️ The panel deliberately stays at the Layout root rather than moving into
   * the header with its button: the header is `fixed z-40` and therefore its
   * own stacking context, so a panel nested inside it would be capped at the
   * header's level whatever its own z-index says. At the Layout root it sits at
   * `z-[49]` — above the header, just below the modal layer (z = 50 + stack
   * position), because it lives inside `#root`, which the modal stack marks
   * inert while a dialog is open: a panel painting over a dialog would be
   * visible but dead.
   */
  showTrigger?: boolean;
  /** Controlled open state. Falls back to internal state when omitted. */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  /**
   * Fired when a recording starts or ends. The floating disc shows a live run
   * itself, but the compact layout replaces it with a header button, so Layout
   * uses this to mark that button and to offer a way back into the run from the
   * debug-logging banner (upstream #2847).
   */
  onLoggingChange?: (active: boolean) => void;
}

export function BugReportBubble({ showTrigger = true, open, onOpenChange, onLoggingChange }: BugReportBubbleProps = {}) {
  const { t } = useTranslation();
  const [internalOpen, setInternalOpen] = useState(false);
  const isControlled = open !== undefined;
  const isOpen = isControlled ? open : internalOpen;
  const setIsOpen = useCallback(
    (next: boolean) => {
      if (!isControlled) setInternalOpen(next);
      onOpenChange?.(next);
    },
    [isControlled, onOpenChange],
  );
  const [viewState, setViewState] = useState<ViewState>('form');
  const [description, setDescription] = useState('');
  const [email, setEmail] = useState('');
  const [screenshot, setScreenshot] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [issueUrl, setIssueUrl] = useState<string | null>(null);
  const [issueNumber, setIssueNumber] = useState<number | null>(null);
  const [errorMessage, setErrorMessage] = useState('');
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [wasDebug, setWasDebug] = useState(false);
  // The logs a stopped recording returned, held until the report is sent or discarded. Kept in
  // memory only (size, and they are the user's to send): a reload loses the review.
  const [heldLogs, setHeldLogs] = useState<string | null>(null);
  const heldLogsRef = useRef(heldLogs);
  heldLogsRef.current = heldLogs;
  const [reportsOpen, setReportsOpen] = useState(false);
  // Lazy: the list (and the relay status sync behind it) is fetched only
  // when the reporter actually expands "My reports".
  const { data: myReports, isLoading: reportsLoading } = useQuery({
    queryKey: ['bug-reports'],
    queryFn: bugReportApi.listReports,
    enabled: isOpen && viewState === 'form' && reportsOpen,
    staleTime: 60_000,
  });

  const modalRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const handleStopLoggingRef = useRef<() => void>(() => {});
  // Read inside effects that must not re-run when the view changes.
  const viewStateRef = useRef(viewState);
  viewStateRef.current = viewState;

  const isLogging = viewState === 'logging';
  useEffect(() => {
    onLoggingChange?.(isLogging);
  }, [isLogging, onLoggingChange]);

  // Before the user files a report, diagnose configured printers. Most bug
  // reports are setup issues — surfacing a connection problem inline lets the
  // user self-fix instead of waiting on a triage round-trip. The result is
  // always shown (healthy or not) so the user can see the check ran.
  const diagnosticScan = useQuery({
    queryKey: ['bugReportDiagnostic'],
    enabled: isOpen && viewState === 'form',
    staleTime: 30_000,
    queryFn: async (): Promise<DiagnosticEntry[]> => {
      const printers = await api.getPrinters();
      const entries = await Promise.all(
        printers.map(async (p) => {
          const result = await api.diagnosePrinter(p.id).catch(() => null);
          return result ? { name: p.name, result } : null;
        }),
      );
      return entries.filter((e): e is DiagnosticEntry => e !== null);
    },
  });
  const diagnosticEntries = diagnosticScan.data ?? [];
  const diagnosticProblems = diagnosticEntries.filter((e) => e.result.overall === 'problems');

  // Scan recent logs against the known-issue catalog. Like the diagnostic
  // above, this surfaces user-fixable ("layer 8") problems before a report is
  // filed. Only shown when something matched — a clean scan stays silent so
  // the form is uncluttered.
  const logHealthScan = useQuery({
    queryKey: ['bugReportLogHealth'],
    enabled: isOpen && viewState === 'form',
    staleTime: 30_000,
    queryFn: api.getSystemHealth,
  });
  const logFindings = logHealthScan.data?.findings ?? [];

  // Elapsed timer for the logging phase — auto-stop at 5 minutes. Measured
  // against the run's start time, not counted in ticks: the run goes on while
  // the panel is closed and while the tab is in the background, where timers
  // are throttled hard enough that a tick count is not a clock.
  useEffect(() => {
    if (viewState !== 'logging' || startedAt === null) return;
    const tick = () => {
      const elapsed = Math.floor((Date.now() - startedAt) / 1000);
      setElapsedSeconds(elapsed);
      if (elapsed >= MAX_LOG_SECONDS) handleStopLoggingRef.current();
    };
    tick();
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, [viewState, startedAt]);

  // ⚠️ Reset ON OPEN rather than in the click handler: the panel now has two
  // possible triggers — the floating disc here, and the compact header's
  // button, which only flips the controlled flag — and a stale half-filled
  // form reappearing for one of them would be a nasty little inconsistency.
  //
  // A run in progress is the exception (upstream #2847): the only thing that
  // stops debug logging is the Stop button on the step this reset would discard.
  // So is a stopped run waiting to be sent, and a failed send that still holds
  // its logs: reopening the panel must not throw the recording away.
  useEffect(() => {
    if (!isOpen) return;
    if (['logging', 'stopping', 'review', 'submitting'].includes(viewStateRef.current)) return;
    if (viewStateRef.current === 'error' && heldLogsRef.current !== null) return;
    setViewState('form');
    setDescription('');
    setEmail('');
    setScreenshot(null);
    setIssueUrl(null);
    setIssueNumber(null);
    setErrorMessage('');
    setElapsedSeconds(0);
    setStartedAt(null);
    setWasDebug(false);
  }, [isOpen]);

  // Pick a run back up after a reload — an ordinary step in reproducing a bug.
  // The panel's state is gone by then but the server still has the log level
  // raised, and nothing else in the report flow points at it.
  useEffect(() => {
    const session = readSession();
    if (!session) return;
    let cancelled = false;

    (async () => {
      let stillLogging: boolean;
      try {
        stillLogging = (await supportApi.getDebugLoggingState()).enabled;
      } catch {
        // Can't tell: keep it for the next load rather than drop a live run.
        return;
      }
      if (cancelled || viewStateRef.current !== 'form') return;

      if (!stillLogging) {
        // Switched off on the System page, or finished in another tab.
        clearSession();
        return;
      }

      const elapsed = Math.floor((Date.now() - session.startedAt) / 1000);
      if (elapsed >= MAX_LOG_SECONDS) {
        // Past the cap with nobody watching. Put the log level back, but do not
        // file it: a description written that long ago is not a report anyone
        // still expects, and no one is here to see it happen.
        try {
          await bugReportApi.stopLogging(session.wasDebug);
        } catch {
          // The debug-logging banner still shows the raised level.
        }
        clearSession();
        return;
      }

      setDescription(session.description);
      setEmail(session.email);
      setWasDebug(session.wasDebug);
      setStartedAt(session.startedAt);
      setElapsedSeconds(elapsed);
      setViewState('logging');
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  const handleOpen = () => setIsOpen(true);

  const handleClose = () => {
    setIsOpen(false);
  };

  const handleFile = useCallback(async (file: File) => {
    if (!file.type.startsWith('image/')) return;
    try {
      const b64 = await compressImage(file);
      setScreenshot(b64);
    } catch {
      // Ignore read errors
    }
  }, []);

  const handlePaste = useCallback((e: React.ClipboardEvent) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    for (const item of items) {
      if (item.type.startsWith('image/')) {
        const file = item.getAsFile();
        if (file) handleFile(file);
        break;
      }
    }
  }, [handleFile]);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(true);
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
  }, []);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
    const file = e.dataTransfer.files?.[0];
    if (file) handleFile(file);
  }, [handleFile]);

  const handleStartLogging = async () => {
    if (!description.trim()) return;
    try {
      const result = await bugReportApi.startLogging();
      const runStartedAt = Date.now();
      setWasDebug(result.was_debug);
      setStartedAt(runStartedAt);
      setElapsedSeconds(0);
      setViewState('logging');
      writeSession({
        description: description.trim(),
        email: email.trim(),
        wasDebug: result.was_debug,
        startedAt: runStartedAt,
      });
    } catch (err) {
      setErrorMessage(err instanceof Error ? err.message : t('bugReport.unexpectedError'));
      setViewState('error');
    }
  };

  const handleStopLogging = async () => {
    // The cap can fire while the panel is closed: show the panel, so the user
    // finds the review waiting instead of a recording that silently ended.
    setIsOpen(true);
    // Over from here whichever way it goes — including a failed stop, where the
    // debug-logging banner surfaces a level that did not come back down.
    clearSession();
    setStartedAt(null);
    setViewState('stopping');
    try {
      const stopResult = await bugReportApi.stopLogging(wasDebug);
      // Stopping sends nothing. The logs wait for the user's own Submit.
      setHeldLogs(stopResult.logs ?? '');
      setViewState('review');
    } catch (err) {
      setErrorMessage(err instanceof Error ? err.message : t('bugReport.unexpectedError'));
      setViewState('error');
    }
  };
  handleStopLoggingRef.current = handleStopLogging;

  const handleDiscard = () => {
    setHeldLogs(null);
    setDescription('');
    setEmail('');
    setScreenshot(null);
    setErrorMessage('');
    setViewState('form');
  };

  const handleSubmitReport = async () => {
    const debugLogs = heldLogs ?? '';
    setViewState('submitting');
    try {
      const result = await bugReportApi.submit({
        description: description.trim(),
        email: email.trim() || undefined,
        screenshot_base64: screenshot || undefined,
        include_support_info: true,
        debug_logs: debugLogs || undefined,
      });
      if (result.success) {
        setHeldLogs(null);
        setIssueUrl(result.issue_url || null);
        setIssueNumber(result.issue_number || null);
        setViewState('success');
      } else {
        setErrorMessage(result.message);
        setViewState('error');
      }
    } catch (err) {
      setErrorMessage(err instanceof Error ? err.message : t('bugReport.unexpectedError'));
      setViewState('error');
    }
  };

  return (
    <>
      {/* Floating bubble. Absent below the sidebar-compact breakpoint, where
          the compact header carries the trigger instead — see Layout. */}
      {showTrigger && (
        <button
          onClick={handleOpen}
          className={`fixed bottom-4 right-4 z-40 w-12 h-12 rounded-full text-white shadow-lg hover:shadow-xl transition-all duration-200 hover:scale-110 flex items-center justify-center ${
            // Amber while a run is going, like the debug-logging banner, so a
            // closed panel still says the recording is live and clickable.
            isLogging ? 'bg-amber-500 hover:bg-amber-600' : 'bg-red-500 hover:bg-red-600'
          }`}
          title={isLogging ? t('bugReport.resumeRecording', { elapsed: formatElapsed(elapsedSeconds) }) : t('bugReport.title')}
        >
          {isLogging && <span className="absolute inset-0 rounded-full bg-amber-400 opacity-75 animate-ping" />}
          <Bug className="w-5 h-5 relative" />
        </button>
      )}

      {/* Slide-in panel anchored to bottom-right */}
      {isOpen && (
        <div
          id="bug-report-modal"
          className="fixed bottom-20 right-4 left-4 z-[49] w-auto max-w-md ml-auto"
          onPaste={handlePaste}
        >
          <div
            ref={modalRef}
            className="bg-bambu-dark-secondary rounded-xl shadow-2xl border border-bambu-dark-tertiary max-h-[80vh] overflow-y-auto"
          >
            {/* Header */}
            <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary sticky top-0 bg-bambu-dark-secondary z-10">
              <h2 className="text-lg font-semibold text-white flex items-center gap-2">
                <Bug className="w-5 h-5 text-red-500" />
                {t('bugReport.title')}
              </h2>
              <button
                onClick={handleClose}
                className="text-bambu-gray hover:text-white transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="p-4 space-y-4">
              {viewState === 'form' && (
                <>
                  {/* Connection diagnostic — scanned on form-open. The result
                      is always shown: a problem panel when a printer has a
                      detected setup issue, otherwise a healthy confirmation. */}
                  {diagnosticScan.isLoading && (
                    <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
                      <Loader2 className="w-3.5 h-3.5 animate-spin" />
                      {t('bugReport.diagnosticChecking')}
                    </div>
                  )}
                  {!diagnosticScan.isLoading && diagnosticProblems.length > 0 && (
                    <div className="rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 p-3 space-y-3">
                      <div className="flex items-start gap-2">
                        <Stethoscope className="w-4 h-4 mt-0.5 flex-shrink-0 text-amber-600 dark:text-amber-400" />
                        <div>
                          <p className="text-sm font-medium text-amber-700 dark:text-amber-300">
                            {t('bugReport.diagnosticSummary', {
                              problems: diagnosticProblems.length,
                              total: diagnosticEntries.length,
                            })}
                          </p>
                          <p className="text-xs text-amber-800 dark:text-amber-200 mt-0.5">
                            {t('bugReport.diagnosticIntro')}
                          </p>
                        </div>
                      </div>
                      <div className="space-y-2">
                        {diagnosticProblems.map((entry) => (
                          <Collapsible
                            key={entry.result.printer_id ?? entry.result.ip_address}
                            defaultOpen={diagnosticProblems.length === 1}
                            className="rounded-lg bg-amber-100/60 dark:bg-amber-900/30 px-3 py-2"
                            summary={
                              <div className="flex items-center gap-2 min-w-0">
                                <AlertTriangle className="w-4 h-4 flex-shrink-0 text-amber-600 dark:text-amber-400" />
                                <span className="text-sm font-medium text-amber-800 dark:text-amber-200 truncate">
                                  {entry.name}
                                </span>
                              </div>
                            }
                          >
                            <DiagnosticChecklist result={entry.result} />
                          </Collapsible>
                        ))}
                      </div>
                    </div>
                  )}
                  {!diagnosticScan.isLoading &&
                    diagnosticEntries.length > 0 &&
                    diagnosticProblems.length === 0 && (
                      <div className="flex items-start gap-2 rounded-lg bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 p-3">
                        <CheckCircle className="w-4 h-4 mt-0.5 flex-shrink-0 text-green-600 dark:text-green-400" />
                        <p className="text-xs text-green-800 dark:text-green-200">
                          {t('bugReport.diagnosticHealthy')}
                        </p>
                      </div>
                    )}

                  {/* Log-health scan — known issues found in recent logs.
                      Shown only when something matched. */}
                  {!logHealthScan.isLoading && logFindings.length > 0 && logHealthScan.data && (
                    <div className="rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 p-3 space-y-3">
                      <div className="flex items-start gap-2">
                        <Stethoscope className="w-4 h-4 mt-0.5 flex-shrink-0 text-amber-600 dark:text-amber-400" />
                        <div>
                          <p className="text-sm font-medium text-amber-700 dark:text-amber-300">
                            {t('bugReport.logHealthSummary')}
                          </p>
                          <p className="text-xs text-amber-800 dark:text-amber-200 mt-0.5">
                            {t('bugReport.logHealthIntro')}
                          </p>
                        </div>
                      </div>
                      <SystemHealthPanel result={logHealthScan.data} />
                    </div>
                  )}

                  {/* Description */}
                  <div>
                    <label className="block text-sm text-bambu-gray mb-1">
                      {t('bugReport.description')} *
                    </label>
                    <textarea
                      value={description}
                      onChange={(e) => setDescription(e.target.value)}
                      placeholder={t('bugReport.descriptionPlaceholder')}
                      rows={3}
                      className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white placeholder-bambu-gray-dark focus:border-bambu-green focus:outline-none resize-vertical"
                    />
                  </div>

                  {/* Email (optional) */}
                  <div>
                    <label className="block text-sm text-bambu-gray mb-1">
                      {t('bugReport.email')}
                    </label>
                    <input
                      type="email"
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                      placeholder={t('bugReport.emailPlaceholder')}
                      className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white placeholder-bambu-gray-dark focus:border-bambu-green focus:outline-none"
                    />
                    <p className="mt-1 text-xs text-bambu-gray-dark">
                      {t('bugReport.emailPrivacy')}
                    </p>
                  </div>

                  {/* Screenshot — upload, paste, or drag */}
                  <div>
                    <label className="block text-sm text-bambu-gray mb-1">
                      {t('bugReport.screenshot')}
                    </label>
                    {screenshot ? (
                      <div className="relative">
                        <img
                          src={`data:image/jpeg;base64,${screenshot}`}
                          alt={t('bugReport.screenshot')}
                          className="w-full max-h-40 object-contain rounded-lg border border-bambu-dark-tertiary"
                        />
                        <button
                          onClick={() => setScreenshot(null)}
                          className="absolute top-2 right-2 p-1 bg-red-500 hover:bg-red-600 text-white rounded-full shadow"
                          title={t('common.delete')}
                        >
                          <Trash2 className="w-3 h-3" />
                        </button>
                      </div>
                    ) : (
                      <button
                        type="button"
                        onClick={() => fileInputRef.current?.click()}
                        onDragOver={handleDragOver}
                        onDragLeave={handleDragLeave}
                        onDrop={handleDrop}
                        className={`w-full flex flex-col items-center gap-2 px-4 py-4 border-2 border-dashed rounded-lg transition-colors cursor-pointer ${
                          isDragging
                            ? 'border-bambu-green bg-bambu-green/10 text-bambu-green'
                            : 'border-bambu-dark-tertiary text-bambu-gray hover:border-bambu-green hover:text-white bg-bambu-dark'
                        }`}
                      >
                        <Upload className="w-5 h-5" />
                        <span className="text-sm">{t('bugReport.uploadOrPaste')}</span>
                      </button>
                    )}
                    <input
                      ref={fileInputRef}
                      type="file"
                      accept="image/*"
                      className="hidden"
                      onChange={(e) => {
                        const file = e.target.files?.[0];
                        if (file) handleFile(file);
                        e.target.value = '';
                      }}
                    />
                  </div>

                  {/* Data collection notice */}
                  <details className="text-xs bg-bambu-dark border border-bambu-dark-tertiary rounded-lg p-3">
                    <summary className="cursor-pointer font-medium text-status-warning hover:text-yellow-700 dark:hover:text-yellow-300">
                      {t('bugReport.dataCollectedSummary')}
                    </summary>
                    <div className="mt-2 space-y-2 pl-2 border-l-2 border-status-warning/40 text-bambu-gray-light">
                      <p className="font-medium text-bambu-gray">{t('bugReport.dataIncluded')}</p>
                      <p>{t('bugReport.dataIncludedList')}</p>
                      <p className="font-medium text-bambu-gray">{t('bugReport.dataNeverIncluded')}</p>
                      <p>{t('bugReport.dataNeverIncludedList')}</p>
                    </div>
                  </details>

                  {/* Previously submitted reports, statuses synced through the relay */}
                  <details
                    className="text-xs bg-bambu-dark border border-bambu-dark-tertiary rounded-lg p-3"
                    onToggle={(e) => setReportsOpen((e.target as HTMLDetailsElement).open)}
                  >
                    <summary className="cursor-pointer font-medium text-bambu-gray hover:text-white">
                      {t('bugReport.myReports')}
                    </summary>
                    <div className="mt-2 space-y-1.5">
                      {reportsLoading && <Loader2 className="w-4 h-4 animate-spin text-bambu-gray" />}
                      {myReports && myReports.reports.length === 0 && (
                        <p className="text-bambu-gray-dark">{t('bugReport.noReports')}</p>
                      )}
                      {myReports && !myReports.synced && (
                        <p className="text-status-warning">{t('bugReport.staleStatuses')}</p>
                      )}
                      {myReports?.reports.map((r) => (
                        <div key={r.id} className="flex items-center gap-2">
                          <span
                            className={`px-1.5 py-0.5 rounded text-[10px] leading-tight whitespace-nowrap ${REPORT_STATUS_STYLES[r.status] ?? REPORT_STATUS_STYLES.submitted}`}
                          >
                            {t(`bugReport.status.${r.status}`)}
                          </span>
                          {r.github_issue_url ? (
                            <a
                              href={r.github_issue_url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="truncate text-bambu-green hover:text-bambu-green-light hover:underline"
                              title={r.description}
                            >
                              #{r.github_issue_number} {r.description}
                            </a>
                          ) : (
                            <span className="truncate text-bambu-gray" title={r.description}>
                              {r.description}
                            </span>
                          )}
                          <span className="ml-auto whitespace-nowrap text-bambu-gray-dark">
                            {new Date(r.created_at).toLocaleDateString()}
                          </span>
                        </div>
                      ))}
                    </div>
                  </details>

                  {/* Buttons */}
                  <div className="flex justify-end gap-2 pt-2">
                    <button
                      onClick={handleClose}
                      className="px-4 py-2 text-sm font-medium text-bambu-gray hover:text-white bg-bambu-dark-tertiary hover:bg-bambu-dark rounded-lg transition-colors"
                    >
                      {t('common.cancel')}
                    </button>
                    <button
                      onClick={handleStartLogging}
                      disabled={!description.trim()}
                      className="px-4 py-2 text-sm font-medium text-white bg-red-500 hover:bg-red-600 disabled:opacity-50 disabled:cursor-not-allowed rounded-lg transition-colors"
                    >
                      {t('bugReport.startLogging')}
                    </button>
                  </div>
                </>
              )}

              {viewState === 'logging' && (
                <div className="py-4 space-y-4">
                  {/* 3-step progress indicator */}
                  <div className="space-y-3 px-2">
                    {/* Step 1: Completed */}
                    <div className="flex items-center gap-3">
                      <CheckCircle2 className="w-5 h-5 text-status-ok flex-shrink-0" />
                      <span className="text-sm text-status-ok">{t('bugReport.stepEnableLogging')}</span>
                    </div>
                    {/* Step 2: Active */}
                    <div className="flex items-center gap-3">
                      <span className="relative flex h-5 w-5 flex-shrink-0 items-center justify-center">
                        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-bambu-green opacity-50"></span>
                        <span className="relative inline-flex rounded-full h-3 w-3 bg-bambu-green"></span>
                      </span>
                      <span className="text-sm font-medium text-bambu-green">{t('bugReport.stepReproduce')}</span>
                    </div>
                    {/* Step 3: Upcoming */}
                    <div className="flex items-center gap-3">
                      <Circle className="w-5 h-5 text-bambu-gray-dark flex-shrink-0" />
                      <span className="text-sm text-bambu-gray-dark">{t('bugReport.stepStopLogging')}</span>
                    </div>
                  </div>

                  {/* Elapsed timer */}
                  <div className="text-center">
                    <p className="text-3xl font-mono text-bambu-green">{formatElapsed(elapsedSeconds)}</p>
                    <p className="text-xs text-bambu-gray-dark mt-1">{t('bugReport.maxDuration', { minutes: 5 })}</p>
                    {/* The panel covers whatever has to be clicked to reproduce
                        the problem, so say plainly that closing it is fine. */}
                    <p className="text-xs text-bambu-gray-dark mt-2">{t('bugReport.closeKeepsRecording')}</p>
                  </div>

                  {/* Stop button — ends the recording, sends nothing */}
                  <div className="flex justify-center">
                    <button
                      onClick={handleStopLogging}
                      className="px-4 py-2.5 text-sm font-medium text-white bg-red-500 hover:bg-red-600 rounded-lg transition-colors"
                    >
                      {t('bugReport.stopLogging')}
                    </button>
                  </div>
                </div>
              )}

              {viewState === 'review' && (
                <div className="py-2 space-y-4">
                  <div className="space-y-3 px-2">
                    <div className="flex items-center gap-3">
                      <CheckCircle2 className="w-5 h-5 text-status-ok flex-shrink-0" />
                      <span className="text-sm text-status-ok">{t('bugReport.stepEnableLogging')}</span>
                    </div>
                    <div className="flex items-center gap-3">
                      <CheckCircle2 className="w-5 h-5 text-status-ok flex-shrink-0" />
                      <span className="text-sm text-status-ok">{t('bugReport.stepRecorded')}</span>
                    </div>
                    <div className="flex items-center gap-3">
                      <span className="relative flex h-5 w-5 flex-shrink-0 items-center justify-center">
                        <span className="relative inline-flex rounded-full h-3 w-3 bg-bambu-green"></span>
                      </span>
                      <span className="text-sm font-medium text-bambu-green">{t('bugReport.stepStopLogging')}</span>
                    </div>
                  </div>

                  <div className="rounded-lg bg-bambu-dark border border-bambu-dark-tertiary p-3 space-y-2">
                    <p className="text-sm font-medium text-white">{t('bugReport.reviewTitle')}</p>
                    <p className="text-xs text-bambu-gray">{t('bugReport.reviewBody')}</p>
                    <p className="text-xs text-bambu-gray-light whitespace-pre-wrap break-words max-h-24 overflow-y-auto">
                      {description}
                    </p>
                    <p className="text-xs text-bambu-gray-dark">
                      {t('bugReport.reviewLogLines', {
                        count: heldLogs ? heldLogs.split('\n').filter((line) => line.trim()).length : 0,
                      })}
                    </p>
                  </div>

                  <div className="flex justify-end gap-2">
                    <button
                      onClick={handleDiscard}
                      className="px-4 py-2 text-sm font-medium text-bambu-gray hover:text-white bg-bambu-dark-tertiary hover:bg-bambu-dark rounded-lg transition-colors"
                    >
                      {t('bugReport.discard')}
                    </button>
                    <button
                      onClick={handleSubmitReport}
                      className="px-4 py-2 text-sm font-medium text-white bg-red-500 hover:bg-red-600 rounded-lg transition-colors"
                    >
                      {t('bugReport.submitReport')}
                    </button>
                  </div>
                </div>
              )}

              {(viewState === 'stopping' || viewState === 'submitting') && (
                <div className="flex flex-col items-center justify-center py-4 gap-3">
                  <Loader2 className="w-8 h-8 animate-spin text-bambu-green" />
                  <p className="text-sm text-bambu-gray text-center">
                    {viewState === 'stopping' ? t('bugReport.stoppingLogs') : t('bugReport.submitting')}
                  </p>
                  {viewState === 'submitting' && (
                    // Diagnostics are run server-side inside the submit call
                    // (#1506 follow-up): the bubble already displays current
                    // results inline, but the submitted report now also
                    // includes a snapshot. Wait is bounded but noticeable —
                    // list what's running so the user knows why.
                    <ul className="text-xs text-bambu-gray list-disc list-inside space-y-0.5">
                      <li>{t('bugReport.submittingStepConnection')}</li>
                      <li>{t('bugReport.submittingStepVirtualPrinters')}</li>
                      <li>{t('bugReport.submittingStepLogScan')}</li>
                      <li>{t('bugReport.submittingStepSubmit')}</li>
                    </ul>
                  )}
                </div>
              )}

              {viewState === 'success' && (
                <div className="flex flex-col items-center justify-center py-8 gap-3">
                  <CheckCircle className="w-12 h-12 text-status-ok" />
                  <p className="text-lg font-semibold text-white">{t('bugReport.thankYou')}</p>
                  <p className="text-sm text-bambu-gray">{t('bugReport.submitted')}</p>
                  {issueUrl && (
                    <a
                      href={issueUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-sm text-bambu-green hover:text-bambu-green-light underline"
                    >
                      {t('bugReport.viewIssue')} #{issueNumber}
                    </a>
                  )}
                  <button
                    onClick={handleClose}
                    className="mt-4 px-4 py-2 text-sm font-medium text-bambu-gray hover:text-white bg-bambu-dark-tertiary hover:bg-bambu-dark rounded-lg transition-colors"
                  >
                    {t('common.close')}
                  </button>
                </div>
              )}

              {viewState === 'error' && (
                <div className="flex flex-col items-center justify-center py-8 gap-3">
                  <AlertCircle className="w-12 h-12 text-status-error" />
                  <p className="text-lg font-semibold text-white">{t('bugReport.submitFailed')}</p>
                  <p className="text-sm text-bambu-gray text-center">{errorMessage}</p>
                  <div className="flex gap-2 mt-4">
                    {/* A failed send keeps the recording: back to the review, where it
                        can be sent again. Without held logs (a failed start or stop)
                        there is nothing to resend, so back to the form. */}
                    <button
                      onClick={() => setViewState(heldLogs !== null ? 'review' : 'form')}
                      className="px-4 py-2 text-sm font-medium text-white bg-red-500 hover:bg-red-600 rounded-lg transition-colors"
                    >
                      {heldLogs !== null ? t('bugReport.backToReview') : t('bugReport.submit')}
                    </button>
                    <button
                      onClick={handleClose}
                      className="px-4 py-2 text-sm font-medium text-bambu-gray hover:text-white bg-bambu-dark-tertiary hover:bg-bambu-dark rounded-lg transition-colors"
                    >
                      {t('common.close')}
                    </button>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
