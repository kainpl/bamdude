import { useState, useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Loader2, ScanEye, Check, X, AlertTriangle } from 'lucide-react';
import { api } from '../api/client';
import type { SecretAction } from '../api/client';
import { SecretInput } from './SecretInput';
import { credentialDraft } from '../utils/credentialDraft';
import { Card, CardContent, CardHeader } from './Card';
import { Button } from './Button';
import { Toggle } from './Toggle';
import { useToast } from '../contexts/ToastContext';
import { Select } from './Select';
import { ObicoHistoryPanel } from './ObicoHistoryPanel';
import { useAuth } from '../contexts/AuthContext';

type TestResult = { ok: boolean; message: string } | null;

export function FailureDetectionSettings() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { isAdmin, hasPermission } = useAuth();
  const canConfigure = isAdmin && hasPermission('settings:update');

  const [enabled, setEnabled] = useState(false);
  const [saveDetections, setSaveDetections] = useState(false);
  const [mlUrl, setMlUrl] = useState('');
  const [mlToken, setMlToken] = useState('');
  const [tokenAction, setTokenAction] = useState<SecretAction>('keep');
  const [urlAction, setUrlAction] = useState<SecretAction>('keep');
  const [sensitivity, setSensitivity] = useState<'low' | 'medium' | 'high'>('medium');
  const [action, setAction] = useState<'notify' | 'pause' | 'pause_and_off'>('notify');
  const [pollInterval, setPollInterval] = useState(10);
  const [enabledPrinters, setEnabledPrinters] = useState<number[] | null>(null); // null = all
  const [testResult, setTestResult] = useState<TestResult>(null);
  const [initialized, setInitialized] = useState(false);
  const connectionVersion = useRef(0);
  const savedConnectionVersion = useRef(0);
  const editConnection = (edit: () => void) => { connectionVersion.current += 1; edit(); };

  const { data: settings } = useQuery({
    queryKey: ['settings'],
    queryFn: api.getSettings,
  });

  const { data: status, refetch: refetchStatus } = useQuery({
    queryKey: ['obico-status'],
    queryFn: api.getObicoStatus,
    refetchInterval: 10000,
  });

  const { data: printers } = useQuery({
    queryKey: ['printers'],
    queryFn: api.getPrinters,
  });

  useEffect(() => {
    if (!settings) return;
    setEnabled(settings.obico_enabled ?? false);
    setSaveDetections(settings.obico_save_detections ?? false);
    if (connectionVersion.current === savedConnectionVersion.current) {
      setMlUrl(settings.obico_ml_url ?? '');
      setMlToken('');
      setTokenAction('keep');
      setUrlAction('keep');
    }
    setSensitivity(settings.obico_sensitivity ?? 'medium');
    setAction(settings.obico_action ?? 'notify');
    setPollInterval(settings.obico_poll_interval ?? 10);
    try {
      const list = settings.obico_enabled_printers
        ? (JSON.parse(settings.obico_enabled_printers) as number[])
        : null;
      setEnabledPrinters(Array.isArray(list) ? list : null);
    } catch {
      setEnabledPrinters(null);
    }
    setInitialized(true);
  }, [settings]);

  const connectionDraft = () => {
    const token = credentialDraft('obico_ml_token', mlToken, tokenAction, settings?.obico_ml_token_configured);
    const url = settings?.obico_ml_url_opaque ? credentialDraft('obico_ml_url', mlUrl, urlAction, true) : { obico_ml_url: mlUrl };
    return { ...token, ...url, secret_actions: { ...token.secret_actions, ...('secret_actions' in url ? url.secret_actions : {}) } };
  };

  const saveMutation = useMutation({
    mutationFn: () =>
      api.updateSettings({
        ...(isAdmin ? { obico_enabled: enabled } : {}),
        obico_save_detections: saveDetections,
        obico_sensitivity: sensitivity,
        obico_action: action,
        obico_poll_interval: pollInterval,
        obico_enabled_printers: enabledPrinters === null ? '' : JSON.stringify(enabledPrinters),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['settings'] });
      queryClient.invalidateQueries({ queryKey: ['obico-status'] });
      showToast(t('settings.toast.settingsSaved'));
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const saveConnection = useMutation({
    mutationFn: ({ draft }: { draft: ReturnType<typeof connectionDraft>; version: number }) => api.updateSettings(draft),
    onSuccess: (data, submitted) => {
      if (connectionVersion.current === submitted.version) {
        savedConnectionVersion.current = submitted.version;
        setMlUrl(data.obico_ml_url ?? '');
        setMlToken('');
        setTokenAction('keep');
        setUrlAction('keep');
      }
      queryClient.invalidateQueries({ queryKey: ['settings'] });
      queryClient.invalidateQueries({ queryKey: ['obico-status'] });
      showToast(t('settings.toast.settingsSaved'));
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });
  const connectionChanged = initialized && !!settings && (settings.obico_ml_url !== mlUrl ||
    mlToken !== '' || tokenAction !== 'keep' || urlAction !== 'keep');

  const hasUnsavedChanges =
    initialized &&
    !!settings &&
    (settings.obico_enabled !== enabled ||
      (settings.obico_save_detections ?? false) !== saveDetections ||
      settings.obico_sensitivity !== sensitivity ||
      settings.obico_action !== action ||
      settings.obico_poll_interval !== pollInterval ||
      settings.obico_enabled_printers !== (enabledPrinters === null ? '' : JSON.stringify(enabledPrinters)));

  // Auto-save on change (debounced)
  useEffect(() => {
    if (!hasUnsavedChanges || !hasPermission('settings:update')) return;
    const id = setTimeout(() => saveMutation.mutate(), 500);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, saveDetections, sensitivity, action, pollInterval, enabledPrinters, initialized, isAdmin]);

  const handleTest = async () => {
    setTestResult(null);
    try {
      const draft = connectionDraft();
      const res = await api.testObicoConnection(draft.obico_ml_url, draft.obico_ml_token, draft.secret_actions);
      if (res.ok) {
        setTestResult({ ok: true, message: t('failureDetection.testSuccess') });
      } else {
        setTestResult({
          ok: false,
          message: res.error || `HTTP ${res.status_code ?? '?'} — ${res.body ?? t('failureDetection.testFailed')}`,
        });
      }
    } catch (e: unknown) {
      setTestResult({ ok: false, message: e instanceof Error ? e.message : String(e) });
    }
  };

  const togglePrinter = (printerId: number, checked: boolean) => {
    if (enabledPrinters === null) {
      // switch from "all" to an explicit list
      const allIds = printers?.map((p) => p.id) ?? [];
      const next = checked ? allIds : allIds.filter((id) => id !== printerId);
      setEnabledPrinters(next);
      return;
    }
    if (checked) {
      setEnabledPrinters([...enabledPrinters, printerId]);
    } else {
      setEnabledPrinters(enabledPrinters.filter((id) => id !== printerId));
    }
  };

  return (
    <div className="flex flex-col lg:flex-row gap-4">
      <div className="space-y-3 flex-1 lg:max-w-xl">
        <Card id="card-fd-ml">
          <CardHeader>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <ScanEye className="w-5 h-5 text-bambu-green" />
                <h2 className="text-lg font-semibold text-white">{t('failureDetection.title')}</h2>
              </div>
              <Toggle checked={enabled} onChange={setEnabled} disabled={!canConfigure} />
            </div>
            <p className="text-sm text-bambu-gray mt-2">{t('failureDetection.description')}</p>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-start justify-between gap-3">
              <div><div className="text-sm text-white">{t('failureDetection.evidence.save')}</div>
                <p className="text-xs text-bambu-gray mt-1">{t('failureDetection.evidence.saveHint')}</p></div>
              <Toggle label={t('failureDetection.evidence.save')} checked={saveDetections} onChange={setSaveDetections} disabled={!hasPermission('settings:update')} />
            </div>
            <div>
              <label className="block text-sm text-bambu-gray mb-1">
                {t('failureDetection.mlUrl')}
              </label>
              <div className="flex gap-2 flex-wrap">
                {settings?.obico_ml_url_opaque ? <SecretInput value={mlUrl} action={urlAction} configured
                  onActionChange={value => editConnection(() => setUrlAction(value))}
                  onChange={value => editConnection(() => setMlUrl(value))} disabled={!canConfigure} /> : <input
                  type="text"
                  value={mlUrl}
                  onChange={(e) => editConnection(() => setMlUrl(e.target.value))}
                  placeholder="http://192.168.1.10:3333"
                  className="flex-1 bg-gray-800 border border-gray-700 rounded px-3 py-2 text-white text-sm"
                  disabled={!canConfigure}
                />}
                <Button
                  onClick={handleTest}
                  disabled={!canConfigure || !(mlUrl || settings?.obico_ml_url_configured) || saveMutation.isPending}
                  variant="secondary"
                >
                  {t('failureDetection.test')}
                </Button>
                <Button onClick={() => saveConnection.mutate({ draft: connectionDraft(), version: connectionVersion.current })}
                  disabled={!canConfigure || !connectionChanged || saveConnection.isPending}>
                  {t('settings.credentials.saveConnection')}
                </Button>
              </div>
              <p className="text-xs text-bambu-gray mt-1">{t('failureDetection.mlUrlHint')}</p>
              <p className="text-xs text-bambu-gray mt-1">{t('settings.credentials.draftHint')}</p>
            </div>

            <div>
              <label className="block text-sm text-bambu-gray mb-1">
                {t('failureDetection.mlToken')}
              </label>
              <SecretInput value={mlToken} action={tokenAction} configured={settings?.obico_ml_token_configured}
                onChange={value => editConnection(() => setMlToken(value))}
                onActionChange={value => editConnection(() => setTokenAction(value))} disabled={!canConfigure} />
              <p className="text-xs text-bambu-gray mt-1">{t('failureDetection.mlTokenHint')}</p>
              {testResult && (
                <div
                  className={`flex items-start gap-2 mt-2 text-sm ${
                    testResult.ok ? 'text-green-700 dark:text-green-400' : 'text-red-700 dark:text-red-400'
                  }`}
                >
                  {testResult.ok ? <Check className="w-4 h-4 mt-0.5" /> : <X className="w-4 h-4 mt-0.5" />}
                  <span>{testResult.message}</span>
                </div>
              )}
            </div>

            <div>
              <label className="block text-sm text-bambu-gray mb-1">
                {t('failureDetection.sensitivity')}
              </label>
              <Select
                className="w-full"
                value={sensitivity}
                onChange={(e) => setSensitivity(e.target.value as 'low' | 'medium' | 'high')}
                disabled={!enabled}
              >
                <option value="low">{t('failureDetection.sensitivityLow')}</option>
                <option value="medium">{t('failureDetection.sensitivityMedium')}</option>
                <option value="high">{t('failureDetection.sensitivityHigh')}</option>
              </Select>
              <p className="text-xs text-bambu-gray mt-1">{t('failureDetection.sensitivityHint')}</p>
            </div>

            <div>
              <label className="block text-sm text-bambu-gray mb-1">
                {t('failureDetection.action')}
              </label>
              <Select
                className="w-full"
                value={action}
                onChange={(e) => setAction(e.target.value as 'notify' | 'pause' | 'pause_and_off')}
                disabled={!enabled}
              >
                <option value="notify">{t('failureDetection.actionNotify')}</option>
                <option value="pause">{t('failureDetection.actionPause')}</option>
                <option value="pause_and_off">{t('failureDetection.actionPauseOff')}</option>
              </Select>
            </div>

            <div>
              <label className="block text-sm text-bambu-gray mb-1">
                {t('failureDetection.pollInterval')}
              </label>
              <input
                type="number"
                value={pollInterval}
                onChange={(e) => setPollInterval(Math.max(5, Math.min(120, Number(e.target.value) || 10)))}
                min={5}
                max={120}
                disabled={!enabled}
                className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-white text-sm"
              />
              <p className="text-xs text-bambu-gray mt-1">{t('failureDetection.pollIntervalHint')}</p>
            </div>

            {status && !status.external_url_configured && enabled && (
              <div className="flex items-start gap-2 p-3 bg-amber-50 dark:bg-amber-900/30 border border-amber-300 dark:border-amber-700 rounded text-sm text-amber-800 dark:text-amber-200">
                <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" />
                <div>
                  <div className="font-medium">{t('failureDetection.externalUrlMissing')}</div>
                  <div className="text-xs mt-1">{t('failureDetection.externalUrlHint')}</div>
                </div>
              </div>
            )}
          </CardContent>
        </Card>

        <Card id="card-fd-perprinter">
          <CardHeader>
            <h2 className="text-lg font-semibold text-white">{t('failureDetection.perPrinterTitle')}</h2>
            <p className="text-sm text-bambu-gray mt-1">{t('failureDetection.perPrinterHint')}</p>
          </CardHeader>
          <CardContent className="space-y-2">
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={enabledPrinters === null}
                onChange={(e) => setEnabledPrinters(e.target.checked ? null : printers?.map((p) => p.id) ?? [])}
                disabled={!enabled}
              />
              <span className="accent-bambu-green text-white">{t('failureDetection.monitorAll')}</span>
            </label>
            {enabledPrinters !== null && printers && (
              <div className="pl-5 space-y-1 border-l border-gray-700">
                {printers.map((p) => (
                  <label key={p.id} className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={enabledPrinters.includes(p.id)}
                      onChange={(e) => togglePrinter(p.id, e.target.checked)}
                      disabled={!enabled}
                    />
                    <span className="accent-bambu-green text-white">{p.name}</span>
                  </label>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      <div className="space-y-3 flex-1 lg:max-w-xl">
        <Card id="card-fd-status">
          <CardHeader>
            <h2 className="text-lg font-semibold text-white">{t('failureDetection.statusTitle')}</h2>
          </CardHeader>
          <CardContent>
            {!status ? (
              <div className="flex items-center gap-2 text-bambu-gray">
                <Loader2 className="w-4 h-4 animate-spin" />
                <span>{t('common.loading')}</span>
              </div>
            ) : (
              <div className="space-y-3 text-sm">
                <div className="flex justify-between">
                  <span className="text-bambu-gray">{t('failureDetection.serviceRunning')}</span>
                  <span className={status.is_running ? 'text-green-700 dark:text-green-400' : 'text-red-700 dark:text-red-400'}>
                    {status.is_running ? t('common.yes') : t('common.no')}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-bambu-gray">{t('failureDetection.thresholds')}</span>
                  <span className="text-white font-mono">
                    {status.thresholds.low.toFixed(2)} / {status.thresholds.high.toFixed(2)}
                  </span>
                </div>
                {status.last_error && (
                  <div className="flex items-start gap-2 text-red-700 dark:text-red-400">
                    <X className="w-4 h-4 mt-0.5 flex-shrink-0" />
                    <span className="break-words">{status.last_error}</span>
                  </div>
                )}
                <div>
                  <div className="text-bambu-gray mb-1">{t('failureDetection.activePrinters')}</div>
                  {Object.keys(status.per_printer).length === 0 ? (
                    <div className="text-bambu-gray italic text-xs">{t('failureDetection.noActivePrints')}</div>
                  ) : (
                    <div className="space-y-1">
                      {Object.entries(status.per_printer).map(([pid, info]) => {
                        const printer = printers?.find((p) => String(p.id) === pid);
                        // Only a verdict is green: "no result yet" is grey and
                        // "the last poll produced none" amber, with its reason
                        // and without a score (upstream #2952).
                        const verdict = ['failure', 'warning', 'safe'].includes(info.class);
                        const colorClass =
                          info.class === 'failure'
                            ? 'text-red-700 dark:text-red-400'
                            : info.class === 'warning' || info.class === 'error'
                              ? 'text-amber-700 dark:text-amber-400'
                              : info.class === 'safe'
                                ? 'text-green-700 dark:text-green-400'
                                : 'text-bambu-gray';
                        return (
                          <div key={pid} className="flex justify-between gap-2">
                            <span className="text-white">{printer?.name ?? `Printer ${pid}`}</span>
                            <span className={`font-mono text-right ${colorClass}`} title={info.error ?? undefined}>
                              {verdict
                                ? `${info.class} (${info.score.toFixed(3)}, ${info.frame_count}f)`
                                : info.class === 'error'
                                  ? t('failureDetection.notChecking')
                                  : t('failureDetection.starting')}
                            </span>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              </div>
            )}
          </CardContent>
        </Card>

        <ObicoHistoryPanel recent={status?.history ?? []} printers={printers ?? []}
          storage={status?.storage} onRefresh={() => { void refetchStatus(); }} />
      </div>
    </div>
  );
}
