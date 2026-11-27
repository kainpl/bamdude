import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Loader2, ScanEye, Check, X, Info } from 'lucide-react';
import { api, type OctoEverywhereConfidence } from '../api/client';
import type { SecretAction } from '../api/client';
import { Card, CardContent, CardHeader } from './Card';
import { Button } from './Button';
import { Toggle } from './Toggle';
import { Select } from './Select';
import { SecretInput } from './SecretInput';
import { OctoEverywhereLimitNotice } from './OctoEverywhereLimitNotice';
import { credentialDraft } from '../utils/credentialDraft';
import { useToast } from '../contexts/ToastContext';
import { useAuth } from '../contexts/AuthContext';
import { aiDetectionClass, hasVerdict, OCTOEVERYWHERE_USAGE_LIMIT, type AiDetectionClass } from '../utils/aiDetection';

type TestResult = { ok: boolean; message: string; errorCode?: string | null } | null;

function detectionColor(cls: AiDetectionClass) {
  return cls === 'failure'
    ? 'text-red-700 dark:text-red-400'
    : cls === 'warning' || cls === 'error'
      ? 'text-amber-700 dark:text-amber-400'
      : cls === 'safe'
        ? 'text-green-700 dark:text-green-400'
        : 'text-bambu-gray';
}

/** OctoEverywhere's panel (ported from upstream Bambuddy). The key is a write-only credential,
 * kept, replaced or cleared through the same secret field and Save button as Obico's token; the
 * other fields save themselves. */
export function OctoEverywhereSettings() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { isAdmin, hasPermission } = useAuth();
  const canUpdate = hasPermission('settings:update');
  const canConfigure = isAdmin && canUpdate;

  const [enabled, setEnabled] = useState(false);
  const [apiKey, setApiKey] = useState('');
  const [keyAction, setKeyAction] = useState<SecretAction>('keep');
  const [confidence, setConfidence] = useState<OctoEverywhereConfidence>('medium');
  const [action, setAction] = useState<'notify' | 'pause' | 'pause_and_off'>('notify');
  const [pollInterval, setPollInterval] = useState(20);
  const [enabledPrinters, setEnabledPrinters] = useState<number[] | null>(null); // null = all
  const [testResult, setTestResult] = useState<TestResult>(null);
  const [testing, setTesting] = useState(false);
  const [initialized, setInitialized] = useState(false);

  const { data: settings } = useQuery({
    queryKey: ['settings'],
    queryFn: api.getSettings,
  });

  const { data: status, refetch: refetchStatus } = useQuery({
    queryKey: ['octoeverywhere-status'],
    queryFn: api.getOctoEverywhereStatus,
    refetchInterval: 10000,
  });

  const { data: printers } = useQuery({
    queryKey: ['printers'],
    queryFn: api.getPrinters,
  });

  useEffect(() => {
    if (!settings) return;
    setEnabled(settings.octoeverywhere_enabled ?? false);
    setConfidence(settings.octoeverywhere_confidence ?? 'medium');
    setAction(settings.octoeverywhere_action ?? 'notify');
    setPollInterval(settings.octoeverywhere_poll_interval ?? 20);
    try {
      const list = settings.octoeverywhere_enabled_printers
        ? (JSON.parse(settings.octoeverywhere_enabled_printers) as number[])
        : null;
      setEnabledPrinters(Array.isArray(list) ? list : null);
    } catch {
      setEnabledPrinters(null);
    }
    setInitialized(true);
  }, [settings]);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['settings'] });
    queryClient.invalidateQueries({ queryKey: ['octoeverywhere-status'] });
    queryClient.invalidateQueries({ queryKey: ['octoEverywherePrinterStatus'] });
    // Enabling OctoEverywhere switches Obico off on the server.
    queryClient.invalidateQueries({ queryKey: ['obico-status'] });
    queryClient.invalidateQueries({ queryKey: ['obicoPrinterStatus'] });
  };

  const saveMutation = useMutation({
    mutationFn: () =>
      api.updateSettings({
        ...(isAdmin ? { octoeverywhere_enabled: enabled } : {}),
        octoeverywhere_confidence: confidence,
        octoeverywhere_action: action,
        octoeverywhere_poll_interval: pollInterval,
        octoeverywhere_enabled_printers: enabledPrinters === null ? '' : JSON.stringify(enabledPrinters),
      }),
    onSuccess: () => {
      invalidate();
      showToast(t('settings.toast.settingsSaved'));
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const keyConfigured = settings?.octoeverywhere_api_key_configured ?? false;
  const keyDraft = () => credentialDraft('octoeverywhere_api_key', apiKey, keyAction, keyConfigured);
  const keyChanged = apiKey !== '' || keyAction !== 'keep';

  const saveKey = useMutation({
    mutationFn: () => api.updateSettings(keyDraft()),
    onSuccess: () => {
      setApiKey('');
      setKeyAction('keep');
      invalidate();
      showToast(t('settings.toast.settingsSaved'));
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const hasUnsavedChanges =
    initialized &&
    !!settings &&
    ((settings.octoeverywhere_enabled ?? false) !== enabled ||
      (settings.octoeverywhere_confidence ?? 'medium') !== confidence ||
      (settings.octoeverywhere_action ?? 'notify') !== action ||
      (settings.octoeverywhere_poll_interval ?? 20) !== pollInterval ||
      (settings.octoeverywhere_enabled_printers ?? '') !== (enabledPrinters === null ? '' : JSON.stringify(enabledPrinters)));

  // Auto-save on change (debounced), matching Obico's panel.
  useEffect(() => {
    if (!hasUnsavedChanges || !canUpdate) return;
    const id = setTimeout(() => saveMutation.mutate(), 500);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, confidence, action, pollInterval, enabledPrinters, initialized, isAdmin]);

  const handleTest = async () => {
    setTestResult(null);
    setTesting(true);
    try {
      const draft = keyDraft();
      const res = await api.testOctoEverywhereConnection(draft.octoeverywhere_api_key, confidence, draft.secret_actions);
      setTestResult({
        ok: res.ok,
        message: res.ok ? t('octoeverywhere.testSuccess') : res.error || t('failureDetection.testFailed'),
        errorCode: res.error_code,
      });
    } catch (e: unknown) {
      setTestResult({ ok: false, message: e instanceof Error ? e.message : String(e) });
    } finally {
      setTesting(false);
    }
  };

  const togglePrinter = (printerId: number, checked: boolean) => {
    const selected = enabledPrinters ?? printers?.map((p) => p.id) ?? [];
    setEnabledPrinters(checked ? [...selected, printerId] : selected.filter((id) => id !== printerId));
  };

  const monitoringState = !status?.enabled
    ? 'monitoringDisabled'
    : !status.api_key_configured
      ? 'monitoringMissingKey'
      : !status.is_running
        ? 'monitoringStopped'
        : status.last_error || status.last_error_code || Object.values(status.per_printer).some((info) => info.class === 'error')
          ? 'monitoringError'
          : Object.values(status.per_printer).some((info) => hasVerdict(aiDetectionClass(info)))
            ? 'monitoringObserved'
            : 'monitoringWaiting';

  return (
    <div className="flex flex-col lg:flex-row gap-4">
      <div className="space-y-3 flex-1 lg:max-w-xl">
        <Card id="card-fd-ml">
          <CardHeader>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <ScanEye className="w-5 h-5 text-bambu-green" />
                <h2 className="text-lg font-semibold text-white">{t('octoeverywhere.title')}</h2>
              </div>
              <Toggle checked={enabled} onChange={setEnabled} disabled={!initialized || !canConfigure} />
            </div>
            <p className="text-sm text-bambu-gray mt-2">
              {t('octoeverywhere.description')}{' '}
              <a
                href="https://docs.octoeverywhere.com/ai-failure-detection-apis/overview/"
                target="_blank"
                rel="noopener noreferrer"
                className="text-bambu-green hover:underline"
              >
                {t('octoeverywhere.learnMore')}
              </a>
            </p>
          </CardHeader>
          <CardContent className="space-y-4">
            <div>
              <label className="block text-sm text-bambu-gray mb-1">{t('octoeverywhere.apiKey')}</label>
              <SecretInput value={apiKey} action={keyAction} configured={keyConfigured}
                onChange={(value) => { setApiKey(value); setTestResult(null); }}
                onActionChange={(value) => { setKeyAction(value); setTestResult(null); }}
                disabled={!canConfigure} />
              <div className="flex gap-2 flex-wrap mt-2">
                <Button
                  onClick={handleTest}
                  disabled={!canConfigure || testing || (!apiKey.trim() && !keyConfigured) || keyAction === 'clear'}
                  variant="secondary"
                >
                  {testing && <Loader2 className="w-4 h-4 animate-spin" />}
                  {t('failureDetection.test')}
                </Button>
                <Button onClick={() => saveKey.mutate()} disabled={!canConfigure || !keyChanged || saveKey.isPending}>
                  {t('octoeverywhere.saveKey')}
                </Button>
              </div>
              <p className="text-xs text-bambu-gray mt-1">
                {t('octoeverywhere.apiKeyHint')}{' '}
                <a
                  href="https://octoeverywhere.com/privacy#gadget-developer-api"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-bambu-green hover:underline"
                >
                  {t('octoeverywhere.privacyPolicy')}
                </a>
              </p>
              <a
                href="https://octoeverywhere.com/gadgetapi"
                target="_blank"
                rel="noopener noreferrer"
                className="inline-block text-xs text-bambu-green hover:underline mt-1"
              >
                {t('octoeverywhere.getApiKey')}
              </a>
              {testResult?.errorCode === OCTOEVERYWHERE_USAGE_LIMIT ? (
                <div className="mt-2"><OctoEverywhereLimitNotice /></div>
              ) : testResult && (
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
              <label htmlFor="oe-confidence" className="block text-sm text-bambu-gray mb-1">
                {t('octoeverywhere.confidence')}
              </label>
              <Select
                id="oe-confidence"
                className="w-full"
                value={confidence}
                onChange={(e) => { setConfidence(e.target.value as OctoEverywhereConfidence); setTestResult(null); }}
                disabled={!enabled || !canUpdate}
              >
                <option value="lowest">{t('octoeverywhere.confidenceLowest')}</option>
                <option value="low">{t('octoeverywhere.confidenceLow')}</option>
                <option value="medium">{t('octoeverywhere.confidenceMedium')}</option>
                <option value="high">{t('octoeverywhere.confidenceHigh')}</option>
                <option value="highest">{t('octoeverywhere.confidenceHighest')}</option>
              </Select>
              <p className="text-xs text-bambu-gray mt-1">{t('octoeverywhere.confidenceHint')}</p>
            </div>

            <div>
              <label htmlFor="oe-action" className="block text-sm text-bambu-gray mb-1">
                {t('failureDetection.action')}
              </label>
              <Select
                id="oe-action"
                className="w-full"
                value={action}
                onChange={(e) => setAction(e.target.value as 'notify' | 'pause' | 'pause_and_off')}
                disabled={!enabled || !canUpdate}
              >
                <option value="notify">{t('failureDetection.actionNotify')}</option>
                <option value="pause">{t('failureDetection.actionPause')}</option>
                <option value="pause_and_off">{t('failureDetection.actionPauseOff')}</option>
              </Select>
            </div>

            <div>
              <div className="flex items-center justify-between gap-2 mb-2">
                <label htmlFor="oe-poll-interval" className="text-sm text-bambu-gray">
                  {t('octoeverywhere.inspectionInterval')}
                </label>
                <span className="text-sm text-white tabular-nums">{t('octoeverywhere.seconds', { count: pollInterval })}</span>
              </div>
              <input
                id="oe-poll-interval"
                type="range"
                value={pollInterval}
                onChange={(e) => setPollInterval(Math.max(5, Math.min(30, Math.round(Number(e.target.value) || 20))))}
                min={5}
                max={30}
                step={1}
                aria-valuetext={t('octoeverywhere.seconds', { count: pollInterval })}
                aria-describedby="oe-interval-hint"
                disabled={!enabled || !canUpdate}
                className="w-full h-1 accent-bambu-green cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
              />
              <p id="oe-interval-hint" className="text-xs text-bambu-gray mt-2">
                {t('octoeverywhere.inspectionIntervalHint')}
              </p>
            </div>
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
                disabled={!enabled || !canUpdate}
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
                      disabled={!enabled || !canUpdate}
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
                  <span className="text-bambu-gray">{t('octoeverywhere.monitoringStatus')}</span>
                  <span className={monitoringState === 'monitoringObserved' ? 'text-green-700 dark:text-green-400' : 'text-bambu-gray'}>
                    {t(`octoeverywhere.${monitoringState}`)}
                  </span>
                </div>
                {status.last_error_code === OCTOEVERYWHERE_USAGE_LIMIT ? (
                  <OctoEverywhereLimitNotice />
                ) : status.last_error && (
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
                    <div className="space-y-2">
                      {Object.entries(status.per_printer).map(([pid, info]) => {
                        const printer = printers?.find((p) => String(p.id) === pid);
                        const cls = aiDetectionClass(info);
                        return (
                          <div key={pid}>
                            <div className="flex justify-between gap-2">
                              <span className="text-white">{printer?.name ?? `#${pid}`}</span>
                              <span className={detectionColor(cls)}>{t(`printers.ai.${cls}`)}</span>
                            </div>
                            {hasVerdict(cls) && info.print_quality !== null && (
                              <div className="text-xs text-bambu-gray">
                                {t('octoeverywhere.printQuality')}: {info.print_quality}/10
                              </div>
                            )}
                            {info.error_code === OCTOEVERYWHERE_USAGE_LIMIT ? (
                              <OctoEverywhereLimitNotice />
                            ) : info.error && <p className="text-xs text-amber-700 dark:text-amber-400 break-words">{info.error}</p>}
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

        <Card id="card-fd-history">
          <CardHeader>
            <div className="flex items-center justify-between">
              <h2 className="text-lg font-semibold text-white">{t('failureDetection.historyTitle')}</h2>
              <button onClick={() => refetchStatus()} className="text-xs text-bambu-gray hover:text-white">
                {t('common.refresh')}
              </button>
            </div>
          </CardHeader>
          <CardContent>
            {!status || status.history.length === 0 ? (
              <div className="flex items-center gap-2 text-bambu-gray text-sm">
                <Info className="w-4 h-4" />
                <span>{t('failureDetection.noHistory')}</span>
              </div>
            ) : (
              <div className="space-y-1 max-h-96 overflow-y-auto text-xs font-mono">
                {status.history.map((ev, idx) => {
                  const printer = printers?.find((p) => p.id === ev.printer_id);
                  return (
                    <div key={idx} className="flex justify-between gap-2 py-1 border-b border-gray-800">
                      <span className="text-bambu-gray">{new Date(ev.timestamp).toLocaleTimeString()}</span>
                      <span className="text-white truncate">{printer?.name ?? `#${ev.printer_id}`}</span>
                      <span className={detectionColor(ev.class)}>
                        {t(`printers.ai.${ev.class}`)}
                        {ev.print_quality !== null && ` ${ev.print_quality}/10`}
                      </span>
                    </div>
                  );
                })}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
