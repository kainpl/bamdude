import { useState, useEffect } from 'react';
import { useMutation, useQueryClient, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { X, Save, Loader2, AlertTriangle } from 'lucide-react';
import { api } from '../api/client';
import type { TelegramChat, TelegramChatCreate, TelegramChatUpdate, NotifyEventInfo } from '../api/client';
import { Button } from './Button';
import { Toggle } from './Toggle';

interface AddTelegramChatModalProps {
  chat?: TelegramChat | null;
  onClose: () => void;
}

export function AddTelegramChatModal({ chat, onClose }: AddTelegramChatModalProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const isEditing = !!chat;

  const [chatId, setChatId] = useState(chat?.chat_id?.toString() || '');
  const [label, setLabel] = useState(chat?.label || '');
  const [groupId, setGroupId] = useState<number | null>(chat?.group_id ?? null);
  const [userId, setUserId] = useState<number | null>(chat?.user_id ?? null);
  const [isActive, setIsActive] = useState(chat?.is_active ?? false);
  const [notifyEvents, setNotifyEvents] = useState<string[] | null>(chat?.notify_events ?? null);
  const [dailyDigest, setDailyDigest] = useState(chat?.daily_digest ?? false);
  const [quietHoursEnabled, setQuietHoursEnabled] = useState(chat?.quiet_hours_enabled ?? false);
  const [quietHoursStart, setQuietHoursStart] = useState(chat?.quiet_hours_start ?? '22:00');
  const [quietHoursEnd, setQuietHoursEnd] = useState(chat?.quiet_hours_end ?? '07:00');
  const [error, setError] = useState<string | null>(null);

  // Fetch groups and users for dropdowns
  const { data: groups } = useQuery({ queryKey: ['groups'], queryFn: api.getGroups });
  const { data: users } = useQuery({ queryKey: ['users'], queryFn: api.getUsers });
  const { data: eventTypes } = useQuery({ queryKey: ['telegram-events'], queryFn: api.getTelegramEvents });

  // Pull the telegram provider so we can warn the operator when the
  // chat-side daily_digest opt-in won't take effect (provider digest off).
  const { data: providers } = useQuery({
    queryKey: ['notification-providers'],
    queryFn: api.getNotificationProviders,
  });
  const telegramProvider = providers?.find((p) => p.provider_type === 'telegram') ?? null;
  const providerDigestOn = telegramProvider?.daily_digest_enabled ?? false;
  const showDigestProviderWarning = dailyDigest && telegramProvider != null && !providerDigestOn;

  // Close on Escape
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  // Auto-fill group from user
  const handleUserChange = (newUserId: number | null) => {
    setUserId(newUserId);
    if (newUserId && users) {
      const user = users.find(u => u.id === newUserId);
      if (user && user.groups && user.groups.length > 0) {
        setGroupId(user.groups[0].id);
      }
    }
  };

  const createMutation = useMutation({
    mutationFn: (data: TelegramChatCreate) => api.createTelegramChat(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['telegram-chats'] });
      onClose();
    },
    onError: (err: Error) => setError(err.message),
  });

  const updateMutation = useMutation({
    mutationFn: (data: TelegramChatUpdate) => api.updateTelegramChat(chat!.id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['telegram-chats'] });
      onClose();
    },
    onError: (err: Error) => setError(err.message),
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (!isEditing && !chatId.trim()) {
      setError(t('telegram.chatIdRequired'));
      return;
    }

    const data = {
      ...(isEditing ? {} : { chat_id: parseInt(chatId) }),
      label: label.trim() || null,
      group_id: groupId,
      user_id: userId,
      is_active: isActive,
      notify_events: notifyEvents,
      daily_digest: dailyDigest,
      quiet_hours_enabled: quietHoursEnabled,
      quiet_hours_start: quietHoursEnabled ? quietHoursStart : null,
      quiet_hours_end: quietHoursEnabled ? quietHoursEnd : null,
    };

    if (isEditing) {
      updateMutation.mutate(data);
    } else {
      createMutation.mutate(data as TelegramChatCreate);
    }
  };

  const isPending = createMutation.isPending || updateMutation.isPending;

  // Toggle event in list
  const toggleEvent = (eventType: string) => {
    const current = notifyEvents ?? eventTypes?.filter(e => e.default).map(e => e.event_type) ?? [];
    if (current.includes(eventType)) {
      setNotifyEvents(current.filter(e => e !== eventType));
    } else {
      setNotifyEvents([...current, eventType]);
    }
  };

  const activeEvents = notifyEvents ?? eventTypes?.filter(e => e.default).map(e => e.event_type) ?? [];

  // Group events by category
  const eventsByCategory: Record<string, NotifyEventInfo[]> = {};
  eventTypes?.forEach(e => {
    if (!eventsByCategory[e.category]) eventsByCategory[e.category] = [];
    eventsByCategory[e.category].push(e);
  });

  // i18n mapping for event types and categories
  const EVENT_LABEL_KEYS: Record<string, string> = {
    print_start: 'notifications.printStarted',
    print_complete: 'notifications.printCompleted',
    print_failed: 'notifications.printFailed',
    print_stopped: 'notifications.printStopped',
    print_paused: 'notifications.printPausedLabel',
    print_resumed: 'notifications.printResumedLabel',
    print_progress: 'notifications.progressMilestones',
    print_missing_spool_assignment: 'notifications.missingSpoolAssignmentLabel',
    printer_offline: 'notifications.printerOffline',
    printer_error: 'notifications.printerError',
    filament_low: 'notifications.lowFilamentLabel',
    maintenance_due: 'notifications.maintenanceDue',
    ams_humidity_high: 'notifications.amsHumidityHigh',
    ams_temperature_high: 'notifications.amsTemperatureHigh',
    ams_ht_humidity_high: 'notifications.amsHtHumidityHigh',
    ams_ht_temperature_high: 'notifications.amsHtTemperatureHigh',
    plate_not_empty: 'notifications.plateNotEmpty',
    bed_cooled: 'notifications.bedCooledLabel',
    first_layer_complete: 'notifications.firstLayerCompleteLabel',
    queue_job_added: 'notifications.jobAdded',
    queue_job_started: 'notifications.jobStarted',
    queue_job_waiting: 'notifications.jobWaiting',
    queue_job_skipped: 'notifications.jobSkipped',
    queue_job_failed: 'notifications.jobFailed',
    queue_completed: 'notifications.queueComplete',
    printer_queue_completed: 'notifications.printerQueueComplete',
  };
  const CATEGORY_KEYS: Record<string, string> = {
    'Print Lifecycle': 'notifications.printEvents',
    'Printer Status': 'notifications.printerStatus',
    'AMS Environmental': 'notifications.amsAlarms',
    'Print Events': 'notifications.printEvents',
    'Queue': 'notifications.printQueue',
  };

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4 overflow-y-auto" onClick={onClose}>
      <div
        className="bg-bambu-dark-secondary rounded-xl border border-bambu-dark-tertiary w-full max-w-lg my-8 max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-bambu-dark-tertiary">
          <h2 className="text-lg font-semibold text-white">
            {isEditing ? t('telegram.editChat') : t('telegram.addChat')}
          </h2>
          <button onClick={onClose} className="text-bambu-gray hover:text-white transition-colors">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} className="px-6 py-4 space-y-4">
          {error && (
            <div className="p-3 bg-red-500/20 border border-red-500/50 rounded text-red-400 text-sm">
              {error}
            </div>
          )}

          {/* Chat ID */}
          <div>
            <label className="block text-sm text-bambu-gray mb-1">Chat ID *</label>
            <input
              type="text"
              value={chatId}
              onChange={(e) => setChatId(e.target.value)}
              disabled={isEditing}
              className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-sm disabled:opacity-50 focus:outline-none focus:ring-1 focus:ring-bambu-green"
              placeholder="123456789"
            />
          </div>

          {/* Label */}
          <div>
            <label className="block text-sm text-bambu-gray mb-1">{t('telegram.label')}</label>
            <input
              type="text"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-sm focus:outline-none focus:ring-1 focus:ring-bambu-green"
              placeholder={t('telegram.labelPlaceholder')}
            />
          </div>

          {/* User (optional) */}
          <div>
            <label className="block text-sm text-bambu-gray mb-1">
              {t('telegram.user')} <span className="text-bambu-gray/50">({t('telegram.optional')})</span>
            </label>
            <select
              value={userId ?? ''}
              onChange={(e) => handleUserChange(e.target.value ? parseInt(e.target.value) : null)}
              className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-sm focus:outline-none focus:ring-1 focus:ring-bambu-green"
            >
              <option value="">{t('telegram.noUser')}</option>
              {users?.map(u => (
                <option key={u.id} value={u.id}>{u.username}</option>
              ))}
            </select>
          </div>

          {/* Role (group) */}
          <div>
            <label className="block text-sm text-bambu-gray mb-1">{t('telegram.role')} *</label>
            <select
              value={groupId ?? ''}
              onChange={(e) => setGroupId(e.target.value ? parseInt(e.target.value) : null)}
              className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-sm focus:outline-none focus:ring-1 focus:ring-bambu-green"
            >
              <option value="">{t('telegram.notAssigned')}</option>
              {groups?.map(g => (
                <option key={g.id} value={g.id}>{g.name}</option>
              ))}
            </select>
            {userId && (
              <p className="text-xs text-bambu-gray/60 mt-1">{t('telegram.roleAutoFill')}</p>
            )}
          </div>

          {/* Active toggle */}
          <div className="flex items-center justify-between py-1">
            <label className="text-sm text-white">{t('telegram.active')}</label>
            <Toggle
              checked={isActive}
              onChange={setIsActive}
            />
          </div>

          {/* Quiet Hours */}
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

          {/* Daily Digest — opt-in only; the time itself is configured on
              the bot/provider so all subscribed chats receive at the same
              moment. */}
          <div className="space-y-1">
            <div className="flex items-center justify-between">
              <label className="text-sm text-white">{t('telegram.dailyDigest')}</label>
              <Toggle
                checked={dailyDigest}
                onChange={setDailyDigest}
              />
            </div>
            {dailyDigest && telegramProvider && providerDigestOn && telegramProvider.daily_digest_time && (
              <p className="text-xs text-bambu-gray">
                {t('telegram.dailyDigestProviderTime', { time: telegramProvider.daily_digest_time })}
              </p>
            )}
            {showDigestProviderWarning && (
              <div className="flex items-start gap-1.5 text-xs text-amber-400">
                <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                <span>{t('telegram.dailyDigestProviderOff')}</span>
              </div>
            )}
          </div>

          {/* Notification Events */}
          <div>
            <label className="block text-sm text-bambu-gray mb-2">{t('telegram.notifyEvents')}</label>
            <div className="space-y-3 bg-bambu-dark rounded border border-bambu-dark-tertiary p-3 max-h-60 overflow-y-auto">
              {Object.entries(eventsByCategory).map(([category, events]) => (
                <div key={category}>
                  <p className="text-xs text-bambu-gray font-medium mb-1">{CATEGORY_KEYS[category] ? t(CATEGORY_KEYS[category]) : category}</p>
                  <div className="space-y-1">
                    {events.map(event => (
                      <label key={event.event_type} className="flex items-center gap-2 text-xs text-white cursor-pointer hover:bg-bambu-dark-tertiary rounded px-1 py-0.5">
                        <input
                          type="checkbox"
                          checked={activeEvents.includes(event.event_type)}
                          onChange={() => toggleEvent(event.event_type)}
                          className="w-3.5 h-3.5 rounded border-bambu-dark-tertiary bg-bambu-dark text-bambu-green focus:ring-bambu-green"
                        />
                        {EVENT_LABEL_KEYS[event.event_type] ? t(EVENT_LABEL_KEYS[event.event_type]) : event.label}
                      </label>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* Actions */}
          <div className="flex gap-3 pt-2">
            <Button type="button" variant="secondary" onClick={onClose} className="flex-1">
              {t('telegram.cancel')}
            </Button>
            <Button type="submit" disabled={isPending} className="flex-1">
              {isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
              {isEditing ? t('telegram.save') : t('telegram.add')}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
