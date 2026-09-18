import { useMemo, useState } from 'react';
import { useMutation, useQueryClient, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Save, Loader2, Send, CheckCircle, XCircle, MessageCircle, ExternalLink, Plus, Trash2 } from 'lucide-react';
import { Link } from 'react-router';
import { api } from '../api/client';
import type { NotificationProvider, NotificationProviderCreate, NotificationProviderUpdate, ProviderType } from '../api/client';
import { Button } from './Button';
import { Modal } from './Modal';
import { Toggle } from './Toggle';
import { ProviderEventToggles } from './ProviderEventToggles';
import { useEventLabel, useProviderEvents } from './providerEvents';

interface AddNotificationModalProps {
  provider?: NotificationProvider | null;
  onClose: () => void;
}

const PROVIDER_VALUES: ProviderType[] = ['email', 'telegram', 'discord', 'ntfy', 'pushover', 'bark', 'callmebot', 'webhook', 'homeassistant', 'signal'];

export function AddNotificationModal({ provider, onClose }: AddNotificationModalProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const isEditing = !!provider;

  const [name, setName] = useState(provider?.name || '');
  const [providerType, setProviderType] = useState<ProviderType>(provider?.provider_type || 'email');
  // Printer scope (m157 3b): null = all printers, [ids] = only those.
  const [printerIds, setPrinterIds] = useState<number[] | null>(provider?.printer_ids ?? null);
  const [quietHoursEnabled, setQuietHoursEnabled] = useState(provider?.quiet_hours_enabled || false);
  const [quietHoursStart, setQuietHoursStart] = useState(provider?.quiet_hours_start || '22:00');
  const [quietHoursEnd, setQuietHoursEnd] = useState(provider?.quiet_hours_end || '07:00');

  // Daily digest
  const [dailyDigestEnabled, setDailyDigestEnabled] = useState(provider?.daily_digest_enabled || false);
  const [dailyDigestTime, setDailyDigestTime] = useState(provider?.daily_digest_time || '08:00');

  // Event subscriptions, keyed by the provider flag.
  //
  // ⚠️ NOT a fixed list. It used to be eighteen `useState` written out by hand,
  // and the backend grew to thirty-four flags without them: AMS alarms, the
  // whole queue group, the sensor aggregates and two print events were simply
  // missing from this form. Six of them default to ON, so a new provider began
  // sending events its creator was never shown. The list now comes from
  // `GET /notifications/events` (`useProviderEvents`), and an unset flag falls
  // back to that row's `default` — see `applyEventDefaults` below.
  const [events, setEvents] = useState<Record<string, boolean>>(() => {
    if (!provider) return {};
    const seeded: Record<string, boolean> = {};
    for (const [key, value] of Object.entries(provider as unknown as Record<string, unknown>)) {
      if (key.startsWith('on_') && typeof value === 'boolean') seeded[key] = value;
    }
    return seeded;
  });
  const { data: providerEvents, isSuccess: eventsReady } = useProviderEvents();
  const eventLabel = useEventLabel();
  // A NEW provider shows the defaults it would be saved with, rather than
  // showing everything off and letting the backend quietly fill six of them in.
  // An EXISTING one falls back to off instead: a flag its row does not carry is
  // one nobody subscribed to, and opening the form must never switch it on.
  const eventsWithDefaults = useMemo(() => {
    const merged: Record<string, boolean> = {};
    for (const event of providerEvents ?? []) {
      merged[event.flag] = events[event.flag] ?? (provider ? false : event.default);
    }
    return merged;
  }, [providerEvents, events, provider]);

  // Provider-specific config (scalar fields only — event_priorities is split out
  // into its own state because it's an object, not a string).
  const [config, setConfig] = useState<Record<string, string>>(
    provider?.config
      ? Object.fromEntries(
          Object.entries(provider.config)
            .filter(([k]) => k !== 'event_priorities')
            .map(([k, v]) => [k, String(v)]),
        )
      : {},
  );

  // Per-event ntfy priority (#990). Map of event key → 1-5. Persisted into
  // config.event_priorities on save; only sent when the provider is ntfy.
  const initialEventPriorities = (() => {
    const raw = provider?.config?.event_priorities;
    if (!raw || typeof raw !== 'object') return {} as Record<string, number>;
    const out: Record<string, number> = {};
    for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
      const n = Number(v);
      if (Number.isInteger(n) && n >= 1 && n <= 5) out[k] = n;
    }
    return out;
  })();
  const [eventPriorities, setEventPriorities] = useState<Record<string, number>>(initialEventPriorities);

  // Signal recipients (recipient_type/numbers/group_id) don't fit the generic
  // scalar-field config model: signal-cli-rest-api can't mix individual
  // numbers and a group in one request, so this is a type switch rather than
  // one generic recipients list.
  const initialSignalNumbers = (() => {
    const raw = provider?.config?.numbers;
    if (typeof raw !== 'string' || !raw.trim()) return [''];
    const parts = raw.split(',').map((n) => n.trim()).filter(Boolean);
    return parts.length > 0 ? parts : [''];
  })();
  const [signalRecipientType, setSignalRecipientType] = useState<'numbers' | 'group'>(
    provider?.config?.recipient_type === 'group' ? 'group' : 'numbers',
  );
  const [signalNumbers, setSignalNumbers] = useState<string[]>(initialSignalNumbers);
  const [signalGroupId, setSignalGroupId] = useState(String(provider?.config?.group_id || ''));

  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Merges the config fields that live outside the generic `config` blob
  // (ntfy's event_priorities, Signal's recipient_type/numbers/group_id) back
  // in. Shared by the test button and the actual save so a test always
  // reflects what would be persisted.
  const buildFinalConfig = (): Record<string, unknown> => {
    if (providerType === 'ntfy' && Object.keys(eventPriorities).length > 0) {
      return { ...config, event_priorities: eventPriorities };
    }
    if (providerType === 'signal') {
      return {
        ...config,
        recipient_type: signalRecipientType,
        ...(signalRecipientType === 'numbers'
          ? { numbers: signalNumbers.map((n) => n.trim()).filter(Boolean).join(',') }
          : { group_id: signalGroupId.trim() }),
      };
    }
    return config;
  };

  // Fetch printers for linking
  const { data: printers } = useQuery({
    queryKey: ['printers'],
    queryFn: api.getPrinters,
  });

  // Test configuration mutation
  const testMutation = useMutation({
    mutationFn: () => api.testNotificationConfig({ provider_type: providerType, config: buildFinalConfig() }),
    onSuccess: (result) => {
      setTestResult(result);
      setError(null);
    },
    onError: (err: Error) => {
      setTestResult({ success: false, message: err.message });
    },
  });

  // Create mutation
  const createMutation = useMutation({
    mutationFn: (data: NotificationProviderCreate) => api.createNotificationProvider(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['notification-providers'] });
      onClose();
    },
    onError: (err: Error) => {
      setError(err.message);
    },
  });

  // Update mutation
  const updateMutation = useMutation({
    mutationFn: (data: NotificationProviderUpdate) => api.updateNotificationProvider(provider!.id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['notification-providers'] });
      onClose();
    },
    onError: (err: Error) => {
      setError(err.message);
    },
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (!name.trim()) {
      setError(t('notifications.nameRequired'));
      return;
    }

    // Validate provider-specific config
    const requiredFields = getRequiredFields(providerType);
    for (const field of requiredFields) {
      if (!config[field.key]?.trim()) {
        setError(t('notifications.fieldRequired', { field: field.label }));
        return;
      }
    }

    // HA custom service-data has to be a JSON object (#1441). Checked here as
    // well as in the sender: a malformed blob saved now would only surface as a
    // failed notification later, at the moment it was needed.
    if (providerType === 'homeassistant' && config.data?.trim()) {
      let parsed: unknown;
      try {
        parsed = JSON.parse(config.data);
      } catch {
        setError(t('notifications.haDataInvalid'));
        return;
      }
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
        setError(t('notifications.haDataInvalid'));
        return;
      }
    }

    // signal-cli-rest-api can't mix individual numbers and a group in one
    // request, so exactly one recipient shape must be filled in.
    if (providerType === 'signal') {
      if (signalRecipientType === 'numbers') {
        if (!signalNumbers.some((n) => n.trim())) {
          setError(t('notifications.signalRecipientsRequired'));
          return;
        }
      } else if (!signalGroupId.trim()) {
        setError(t('notifications.signalGroupRequired'));
        return;
      }
    }

    const finalConfig = buildFinalConfig();

    const data = {
      name: name.trim(),
      provider_type: providerType,
      config: finalConfig,
      printer_ids: printerIds,
      quiet_hours_enabled: quietHoursEnabled,
      quiet_hours_start: quietHoursEnabled ? quietHoursStart : null,
      quiet_hours_end: quietHoursEnabled ? quietHoursEnd : null,
      // Daily digest
      daily_digest_enabled: dailyDigestEnabled,
      daily_digest_time: dailyDigestEnabled ? dailyDigestTime : null,
      // Every flag the API knows about, so the backend never has to fill one
      // in silently — that is how six events arrived switched on unseen.
      ...eventsWithDefaults,
    };

    if (isEditing) {
      updateMutation.mutate(data);
    } else {
      createMutation.mutate(data);
    }
  };

  const isPending = createMutation.isPending || updateMutation.isPending;

  // Get config fields for each provider type
  const getConfigFields = (type: ProviderType) => {
    switch (type) {
      case 'callmebot':
        return [
          { key: 'phone', label: 'Phone Number', placeholder: '+1234567890', type: 'text', required: true },
          { key: 'apikey', label: 'API Key', placeholder: 'Your CallMeBot API key', type: 'text', required: true },
        ];
      case 'ntfy':
        return [
          { key: 'server', label: 'Server URL', placeholder: 'https://ntfy.sh', type: 'text', required: false },
          { key: 'topic', label: 'Topic', placeholder: 'my-bamdude', type: 'text', required: true },
          { key: 'auth_token', label: 'Auth Token', placeholder: 'Optional authentication', type: 'password', required: false },
        ];
      case 'pushover':
        return [
          { key: 'user_key', label: 'User Key', placeholder: 'Your Pushover user key', type: 'text', required: true },
          { key: 'app_token', label: 'App Token', placeholder: 'Your Pushover app token', type: 'text', required: true },
          { key: 'priority', label: 'Priority', placeholder: '0 (normal)', type: 'number', required: false },
          // Emergency priority (2) requires retry/expire — Pushover rejects the
          // message otherwise. Only shown when priority is set to 2.
          {
            key: 'retry',
            label: t('notifications.pushoverRetry'),
            placeholder: '60',
            type: 'number',
            required: false,
            showIf: (cfg: Record<string, string>) => cfg.priority === '2',
          },
          {
            key: 'expire',
            label: t('notifications.pushoverExpire'),
            placeholder: '3600',
            type: 'number',
            required: false,
            showIf: (cfg: Record<string, string>) => cfg.priority === '2',
          },
        ];
      case 'bark':
        return [
          { key: 'device_key', label: t('notifications.barkDeviceKey'), placeholder: 'Your Bark device key', type: 'text', required: true },
          { key: 'server', label: 'Server URL', placeholder: 'https://api.day.app', type: 'text', required: false },
          { key: 'group', label: t('notifications.barkGroup'), placeholder: 'BamDude', type: 'text', required: false },
          { key: 'sound', label: t('notifications.barkSound'), placeholder: 'minuet', type: 'text', required: false },
          { key: 'level', label: t('notifications.barkLevel'), type: 'select', required: false, options: [
            { value: '', label: t('notifications.barkLevelDefault') },
            { value: 'active', label: t('notifications.barkLevelActive') },
            { value: 'timeSensitive', label: t('notifications.barkLevelTimeSensitive') },
            { value: 'critical', label: t('notifications.barkLevelCritical') },
            { value: 'passive', label: t('notifications.barkLevelPassive') },
          ]},
        ];
      case 'telegram':
        return [
          { key: 'bot_token', label: 'Bot Token', placeholder: 'Bot token from @BotFather', type: 'password', required: true },
        ];
      case 'email':
        return [
          { key: 'smtp_server', label: 'SMTP Server', placeholder: 'smtp.gmail.com', type: 'text', required: true },
          { key: 'smtp_port', label: 'SMTP Port', placeholder: '587', type: 'number', required: false },
          { key: 'security', label: 'Security', type: 'select', required: false, options: [
            { value: 'starttls', label: 'STARTTLS (Port 587)' },
            { value: 'ssl', label: 'SSL/TLS (Port 465)' },
            { value: 'none', label: 'None (Port 25)' },
          ]},
          { key: 'auth_enabled', label: 'Authentication', type: 'select', required: false, options: [
            { value: 'true', label: 'Enabled' },
            { value: 'false', label: 'Disabled' },
          ]},
          { key: 'username', label: 'Username', placeholder: 'your@email.com', type: 'text', required: false },
          { key: 'password', label: 'Password', placeholder: 'App password', type: 'password', required: false },
          { key: 'from_email', label: 'From Email', placeholder: 'your@email.com', type: 'text', required: true },
          { key: 'to_email', label: 'To Email', placeholder: 'recipient@email.com', type: 'text', required: true },
        ];
      case 'discord':
        return [
          { key: 'webhook_url', label: 'Webhook URL', placeholder: 'https://discord.com/api/webhooks/...', type: 'text', required: true },
        ];
      case 'webhook':
        return [
          { key: 'webhook_url', label: 'Webhook URL', placeholder: 'https://example.com/webhook', type: 'text', required: true },
          { key: 'payload_format', label: 'Payload Format', type: 'select', required: false, options: [
            { value: 'generic', label: 'Generic JSON' },
            { value: 'slack', label: 'Slack / Mattermost' },
          ]},
          { key: 'auth_header', label: 'Authorization', placeholder: 'Bearer token (optional)', type: 'password', required: false },
          { key: 'field_title', label: 'Title Field Name', placeholder: 'title', type: 'text', required: false, showIf: (cfg: Record<string, string>) => cfg.payload_format !== 'slack' },
          { key: 'field_message', label: 'Message Field Name', placeholder: 'message', type: 'text', required: false, showIf: (cfg: Record<string, string>) => cfg.payload_format !== 'slack' },
        ];
      case 'homeassistant':
        return [
          { key: 'service', label: 'Home Assistant Service', placeholder: 'notify.mobile_app_myphone', type: 'text', required: false },
          { key: 'data', label: t('notifications.haDataLabel'), placeholder: '{"priority": "high", "ttl": 0, "channel": "3D Printing"}', type: 'textarea', required: false },
        ];
      case 'signal':
        return [
          { key: 'server', label: 'Signal API URL', placeholder: 'http://localhost:8080 (base URL, not /v2/send)', type: 'text', required: true },
          { key: 'sender_number', label: 'Sender Number', placeholder: '+15551234567', type: 'text', required: true },
          { key: 'auth_header', label: 'Authorization', placeholder: 'Bearer token (optional)', type: 'password', required: false },
        ];
      default:
        return [];
    }
  };

  const getRequiredFields = (type: ProviderType) => {
    return getConfigFields(type).filter(f => f.required);
  };

  const configFields = getConfigFields(providerType);

  return (
    <Modal
      onClose={onClose}
      title={isEditing ? t('notifications.editTitle') : t('notifications.addTitle')}
      size="lg"
    >
      {/* Form */}
      <form onSubmit={handleSubmit} className="p-6 space-y-4">
        {error && (
          <div className="p-3 bg-red-100 dark:bg-red-500/20 border border-red-300 dark:border-red-500/50 rounded-lg text-sm text-red-700 dark:text-red-400">
            {error}
          </div>
        )}

        {/* Name */}
        <div>
          <label className="block text-sm text-bambu-gray mb-1">{t('notifications.nameLabel')}</label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={t('notifications.namePlaceholder')}
            className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
          />
        </div>

        {/* Provider Type */}
        <div>
          <label className="block text-sm text-bambu-gray mb-1">{t('notifications.providerTypeLabel')}</label>
          <select
            value={providerType}
            onChange={(e) => {
              setProviderType(e.target.value as ProviderType);
              setConfig({}); // Reset config when changing type
              setSignalRecipientType('numbers');
              setSignalNumbers(['']);
              setSignalGroupId('');
              setTestResult(null);
            }}
            disabled={isEditing}
            className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none disabled:opacity-50"
          >
            {PROVIDER_VALUES.map((value) => (
              <option key={value} value={value}>
                {t(`notifications.providerTypes.${value}`, value)}
              </option>
            ))}
          </select>
          <p className="text-xs text-bambu-gray mt-1">
            {t(`notifications.providerDescriptions.${providerType}`, '')}
          </p>
        </div>

        {/* Provider-specific configuration */}
        <div className="space-y-3">
          <p className="text-sm text-bambu-gray">{t('notifications.configuration')}</p>
          {configFields
            .filter((field) => !('showIf' in field) || (field as { showIf?: (cfg: Record<string, string>) => boolean }).showIf?.(config) !== false)
            .map((field) => (
            <div key={field.key}>
              <label className="block text-sm text-bambu-gray mb-1">
                {field.label} {field.required && '*'}
              </label>
              {field.type === 'select' && 'options' in field && field.options ? (
                <select
                  value={config[field.key] || field.options[0]?.value || ''}
                  onChange={(e) => {
                    setConfig({ ...config, [field.key]: e.target.value });
                    setTestResult(null);
                  }}
                  className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                >
                  {field.options.map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {opt.label}
                    </option>
                  ))}
                </select>
              ) : field.type === 'textarea' ? (
                <textarea
                  value={config[field.key] || ''}
                  onChange={(e) => {
                    setConfig({ ...config, [field.key]: e.target.value });
                    setTestResult(null);
                  }}
                  placeholder={field.placeholder}
                  rows={3}
                  className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none font-mono text-sm"
                />
              ) : (
                <input
                  type={field.type}
                  value={config[field.key] || ''}
                  onChange={(e) => {
                    setConfig({ ...config, [field.key]: e.target.value });
                    setTestResult(null);
                  }}
                  placeholder={field.placeholder}
                  className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                />
              )}
            </div>
          ))}
        </div>

        {/* Signal recipients - signal-cli-rest-api can't mix individual
            numbers and a group in one request, so this is a type switch
            rather than one generic recipients list. */}
        {providerType === 'signal' && (
          <div className="space-y-3 p-3 bg-bambu-dark rounded-lg">
            <div>
              <label className="block text-sm text-bambu-gray mb-1">{t('notifications.signalRecipientType')}</label>
              <select
                value={signalRecipientType}
                onChange={(e) => {
                  setSignalRecipientType(e.target.value as 'numbers' | 'group');
                  setTestResult(null);
                }}
                className="w-full px-3 py-2 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
              >
                <option value="numbers">{t('notifications.signalRecipientTypeNumbers')}</option>
                <option value="group">{t('notifications.signalRecipientTypeGroup')}</option>
              </select>
            </div>

            {signalRecipientType === 'numbers' ? (
              <div className="space-y-2">
                <label className="block text-sm text-bambu-gray">{t('notifications.signalNumbers')} *</label>
                {signalNumbers.map((number, index) => (
                  <div key={index} className="flex gap-2">
                    <input
                      type="text"
                      value={number}
                      onChange={(e) => {
                        const next = [...signalNumbers];
                        next[index] = e.target.value;
                        setSignalNumbers(next);
                        setTestResult(null);
                      }}
                      placeholder="+15551234567"
                      className="flex-1 px-3 py-2 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                    />
                    <button
                      type="button"
                      onClick={() => setSignalNumbers(signalNumbers.filter((_, i) => i !== index))}
                      disabled={signalNumbers.length <= 1}
                      className="px-2 text-bambu-gray hover:text-red-400 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                      aria-label={t('notifications.signalRemoveNumber')}
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
                ))}
                <button
                  type="button"
                  onClick={() => setSignalNumbers([...signalNumbers, ''])}
                  className="flex items-center gap-1 text-sm text-bambu-green hover:opacity-80 transition-opacity"
                >
                  <Plus className="w-4 h-4" />
                  {t('notifications.signalAddNumber')}
                </button>
              </div>
            ) : (
              <div>
                <label className="block text-sm text-bambu-gray mb-1">{t('notifications.signalGroupId')} *</label>
                <input
                  type="text"
                  value={signalGroupId}
                  onChange={(e) => {
                    setSignalGroupId(e.target.value);
                    setTestResult(null);
                  }}
                  placeholder="group.XXXXXXXX=="
                  className="w-full px-3 py-2 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                />
              </div>
            )}
          </div>
        )}

        {/* Test Button (not shown for Telegram - bot restarts automatically) */}
        {providerType !== 'telegram' && (
          <div className="flex gap-2">
            <Button
              type="button"
              variant="secondary"
              onClick={() => {
                setTestResult(null);
                testMutation.mutate();
              }}
              disabled={testMutation.isPending || (getRequiredFields(providerType).length > 0 && !config[getRequiredFields(providerType)[0]?.key])}
              className="flex-1"
            >
              {testMutation.isPending ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Send className="w-4 h-4" />
              )}
              {t('notifications.testConfiguration')}
            </Button>
          </div>
        )}

        {/* Test Result */}
        {testResult && (
          <div className={`p-3 rounded-lg flex items-center gap-2 ${
            testResult.success
              ? 'bg-bambu-green/20 border border-bambu-green/50 text-bambu-green'
              : 'bg-red-100 dark:bg-red-500/20 border border-red-300 dark:border-red-500/50 text-red-700 dark:text-red-400'
          }`}>
            {testResult.success ? (
              <>
                <CheckCircle className="w-5 h-5" />
                <span>{testResult.message}</span>
              </>
            ) : (
              <>
                <XCircle className="w-5 h-5" />
                <span>{testResult.message}</span>
              </>
            )}
          </div>
        )}

        {/* Printer scope — all / one / several (m157 3b). Not for
            telegram: the scope lives on each chat there, like every other
            telegram knob. */}
        {providerType !== 'telegram' && (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <div>
              <label className="block text-sm text-white">{t('notifications.printerFilter')}</label>
              <p className="text-xs text-bambu-gray">{t('notifications.onlyFromPrinter')}</p>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-xs text-bambu-gray">{t('notifications.allPrinters')}</span>
              <Toggle
                checked={printerIds === null}
                onChange={(all) => setPrinterIds(all ? null : [])}
              />
            </div>
          </div>
          {printerIds !== null && (
            <div className="space-y-1 bg-bambu-dark rounded border border-bambu-dark-tertiary p-3 max-h-40 overflow-y-auto">
              {(printers ?? []).map((p) => (
                <label
                  key={p.id}
                  className="flex items-center gap-2 text-xs text-white cursor-pointer hover:bg-bambu-dark-tertiary rounded px-1 py-0.5"
                >
                  <input
                    type="checkbox"
                    checked={printerIds.includes(p.id)}
                    onChange={() =>
                      setPrinterIds(
                        printerIds.includes(p.id)
                          ? printerIds.filter((id) => id !== p.id)
                          : [...printerIds, p.id]
                      )
                    }
                    className="accent-bambu-green w-3.5 h-3.5 rounded border-bambu-dark-tertiary bg-bambu-dark text-bambu-green focus:ring-bambu-green"
                  />
                  {p.name}
                </label>
              ))}
              {printerIds.length === 0 && (
                <p className="text-[10px] text-amber-500">{t('telegram.printerScopeEmpty')}</p>
              )}
            </div>
          )}
        </div>
        )}

        {/* Telegram-only hint: per-event opt-ins and quiet hours live on
            each chat now (m045). Provider keeps only enabled + digest. */}
        {providerType === 'telegram' && (
          <div className="p-3 bg-blue-50 dark:bg-blue-500/10 border border-blue-300 dark:border-blue-500/30 rounded-lg flex items-start gap-2">
            <MessageCircle className="w-4 h-4 text-blue-600 dark:text-blue-400 flex-shrink-0 mt-0.5" />
            <div className="text-xs text-bambu-gray space-y-1">
              <p className="text-white">{t('notifications.telegram.perChatHintTitle')}</p>
              <p>{t('notifications.telegram.perChatHintBody')}</p>
              <Link to="/telegram" className="inline-flex items-center gap-1 text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300">
                {t('notifications.telegram.openChatsPage')}
                <ExternalLink className="w-3 h-3" />
              </Link>
            </div>
          </div>
        )}

        {/* Quiet Hours — provider-level. Telegram skips this; per-chat
            quiet hours are configured on each TelegramChat row. */}
        {providerType !== 'telegram' && (
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <label className="text-sm text-white">{t('notifications.quietHoursDnd')}</label>
              <Toggle
                checked={quietHoursEnabled}
                onChange={setQuietHoursEnabled}
              />
            </div>
            {quietHoursEnabled && (
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs text-bambu-gray mb-1">{t('notifications.quietStart')}</label>
                  <input
                    type="time"
                    value={quietHoursStart}
                    onChange={(e) => setQuietHoursStart(e.target.value)}
                    className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                  />
                </div>
                <div>
                  <label className="block text-xs text-bambu-gray mb-1">{t('notifications.quietEnd')}</label>
                  <input
                    type="time"
                    value={quietHoursEnd}
                    onChange={(e) => setQuietHoursEnd(e.target.value)}
                    className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                  />
                </div>
              </div>
            )}
          </div>
        )}

        {/* Daily Digest */}
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <div>
              <label className="text-sm text-white">{t('notifications.dailyDigestLabel')}</label>
              <p className="text-xs text-bambu-gray">{t('notifications.batchNotifications')}</p>
            </div>
            <Toggle
              checked={dailyDigestEnabled}
              onChange={setDailyDigestEnabled}
            />
          </div>
          {dailyDigestEnabled && (
            <div>
              <label className="block text-xs text-bambu-gray mb-1">{t('notifications.sendDigestAt')}</label>
              <input
                type="time"
                value={dailyDigestTime}
                onChange={(e) => setDailyDigestTime(e.target.value)}
                className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
              />
              <p className="text-xs text-bambu-gray mt-1">
                {t('notifications.digestCollected')}
              </p>
            </div>
          )}
        </div>

        {/* Event Toggles — provider-level. Telegram lives per-chat instead.
            The list itself comes from the API; see ProviderEventToggles. */}
        {providerType !== 'telegram' && (
        <div className="space-y-3">
          <p className="text-sm text-bambu-gray">{t('notifications.notificationEvents')}</p>

          <ProviderEventToggles
            value={eventsWithDefaults}
            onChange={(flag, on) => setEvents((prev) => ({ ...prev, [flag]: on }))}
            columns={2}
          />

          {/* Per-event ntfy priority (#990) */}
          {providerType === 'ntfy' && (() => {
            const enabledEvents = (providerEvents ?? [])
              .filter((event) => eventsWithDefaults[event.flag])
              .map((event) => ({ key: event.flag, label: eventLabel(event) }));

            if (enabledEvents.length === 0) return null;

            return (
              <div className="space-y-2 p-3 bg-bambu-dark rounded-lg">
                <p className="text-xs text-bambu-gray uppercase tracking-wide mb-1">
                  {t('notifications.eventPriority.sectionTitle')}
                </p>
                <p className="text-xs text-bambu-gray mb-2">{t('notifications.eventPriority.helpNtfy')}</p>
                <div className="space-y-2">
                  {enabledEvents.map((ev) => (
                    <div key={ev.key} className="flex items-center justify-between gap-3">
                      <span className="text-sm text-white">{ev.label}</span>
                      <select
                        value={eventPriorities[ev.key] ?? 3}
                        onChange={(e) => {
                          const next = Number(e.target.value);
                          setEventPriorities((prev) => ({ ...prev, [ev.key]: next }));
                        }}
                        className="px-2 py-1 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded text-sm text-white focus:border-bambu-green focus:outline-none"
                      >
                        <option value={1}>{t('notifications.eventPriority.min')}</option>
                        <option value={2}>{t('notifications.eventPriority.low')}</option>
                        <option value={3}>{t('notifications.eventPriority.default')}</option>
                        <option value={4}>{t('notifications.eventPriority.high')}</option>
                        <option value={5}>{t('notifications.eventPriority.urgent')}</option>
                      </select>
                    </div>
                  ))}
                </div>
              </div>
            );
          })()}
        </div>
        )}

        {/* Actions */}
        <div className="flex gap-3 pt-2">
          <Button
            type="button"
            variant="secondary"
            onClick={onClose}
            className="flex-1"
          >
            {t('notifications.cancel')}
          </Button>
          {/* ⚠️ Saving waits for the event list, and that is not cosmetic:
              without it the payload would carry no on_* fields at all and the
              backend would apply its own defaults — six of which are ON, which
              is the exact bug this form was changed to end. */}
          <Button
            type="submit"
            disabled={isPending || !eventsReady}
            className="flex-1"
          >
            {isPending ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Save className="w-4 h-4" />
            )}
            {isEditing ? t('notifications.save') : t('notifications.add')}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
