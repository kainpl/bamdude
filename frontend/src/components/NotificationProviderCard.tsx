import {useState} from 'react';
import {useMutation, useQueryClient, useQuery} from '@tanstack/react-query';
import {useTranslation} from 'react-i18next';
import {
    Bell,
    Trash2,
    Settings2,
    Edit2,
    Send,
    Loader2,
    CheckCircle,
    XCircle,
    Moon,
    Clock,
    ChevronDown,
    ChevronUp,
    Calendar,
    MessageCircle,
    Plus
} from 'lucide-react';
import {api} from '../api/client';
import {formatDateOnly, parseUTCDate, type DateFormat} from '../utils/date';
import type {NotificationProvider, NotificationProviderUpdate, TelegramChat} from '../api/client';
import {Card, CardContent} from './Card';
import {Button} from './Button';
import {ConfirmModal} from './ConfirmModal';
import {Toggle} from './Toggle';
import {ProviderEventToggles} from './ProviderEventToggles';
import {useEventLabel, useProviderEvents} from './providerEvents';
import {TelegramChatCard} from './TelegramChatCard';
import {AddTelegramChatModal} from './AddTelegramChatModal';

function RegistrationModeToggle() {
    const { t } = useTranslation();
    const queryClient = useQueryClient();

    const { data: settings } = useQuery({
        queryKey: ['settings'],
        queryFn: api.getSettings,
    });

    const updateMutation = useMutation({
        mutationFn: (open: boolean) => api.updateSettings({ telegram_registration_open: open }),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['settings'] });
        },
    });

    const isOpen = settings?.telegram_registration_open ?? false;

    return (
        <div className="flex items-center gap-2">
            <span className="text-xs text-bambu-gray">{t('telegram.registrationMode')}</span>
            <Toggle
                checked={isOpen}
                onChange={(checked) => updateMutation.mutate(checked)}
            />
        </div>
    );
}

interface NotificationProviderCardProps {
    provider: NotificationProvider;
    onEdit: (provider: NotificationProvider) => void;
}

/** Chip colour by severity: a chip says how serious, not which event. */
const SEVERITY_CHIP: Record<string, string> = {
    error: 'bg-red-100 dark:bg-red-500/20 text-red-700 dark:text-red-400',
    warning: 'bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-300',
    info: 'bg-blue-100 dark:bg-blue-500/20 text-blue-700 dark:text-blue-400',
};

export function NotificationProviderCard({provider, onEdit}: NotificationProviderCardProps) {
    const {t} = useTranslation();
    const queryClient = useQueryClient();
    const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
    const [isExpanded, setIsExpanded] = useState(false);
    const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null);
    const [showTelegramChatModal, setShowTelegramChatModal] = useState(false);
    const [editingTelegramChat, setEditingTelegramChat] = useState<TelegramChat | null>(null);

    // Pull system date_format so the "last success" timestamp follows the
    // user's preference. Cached globally — shared with other settings reads.
    const {data: settings} = useQuery({
        queryKey: ['settings'],
        queryFn: api.getSettings,
        staleTime: 60_000,
    });
    const dateFormat = (settings?.date_format ?? 'system') as DateFormat;

    const isTelegram = provider.provider_type === 'telegram';

    // Flag -> subscribed, read off the provider row, and the subset to show as
    // chips. Both walk the API's list rather than a list written here.
    const { data: providerEvents } = useProviderEvents();
    const eventLabel = useEventLabel();
    const eventValues: Record<string, boolean> = {};
    for (const event of providerEvents ?? []) {
        eventValues[event.flag] = Boolean((provider as unknown as Record<string, unknown>)[event.flag]);
    }
    const subscribedEvents = (providerEvents ?? []).filter((event) => eventValues[event.flag]);

    // Fetch telegram chats only for telegram providers
    const {data: telegramChats, isLoading: telegramChatsLoading} = useQuery({
        queryKey: ['telegram-chats'],
        queryFn: api.getTelegramChats,
        enabled: isTelegram,
    });
    // A chat belongs to the bot it wrote to (m180): this card shows only ITS
    // chats, and a chat it adds is registered under this provider.
    const providerChats = (telegramChats ?? []).filter((chat) => chat.provider_id === provider.id);

    // Fetch printers for linking
    const {data: printers} = useQuery({
        queryKey: ['printers'],
        queryFn: api.getPrinters,
    });

    // m157 3b: the scope is a list — show the names it resolves to.
    const linkedPrinters = provider.printer_ids == null
        ? []
        : (printers ?? []).filter(p => provider.printer_ids!.includes(p.id));

    // Update mutation
    const updateMutation = useMutation({
        mutationFn: (data: NotificationProviderUpdate) => api.updateNotificationProvider(provider.id, data),
        onSuccess: () => {
            queryClient.invalidateQueries({queryKey: ['notification-providers']});
        },
    });

    // Delete mutation
    const deleteMutation = useMutation({
        mutationFn: () => api.deleteNotificationProvider(provider.id),
        onSuccess: () => {
            queryClient.invalidateQueries({queryKey: ['notification-providers']});
        },
    });

    // Test mutation
    const testMutation = useMutation({
        mutationFn: () => api.testNotificationProvider(provider.id),
        onSuccess: (result) => {
            setTestResult(result);
            queryClient.invalidateQueries({queryKey: ['notification-providers']});
        },
        onError: (err: Error) => {
            setTestResult({success: false, message: err.message});
        },
    });

    // Format time for display
    const formatTime = (time: string | null) => {
        if (!time) return '';
        return time;
    };

    return (
        <>
            <Card className="relative">
                <CardContent className="p-4">
                    {/* Header Row */}
                    <div className="flex items-start justify-between mb-3">
                        <div className="flex items-center gap-3">
                            <div
                                className={`p-2 rounded-lg ${provider.enabled ? 'bg-bambu-green/20' : 'bg-bambu-dark'}`}>
                                <Bell
                                    className={`w-5 h-5 ${provider.enabled ? 'text-bambu-green' : 'text-bambu-gray'}`}/>
                            </div>
                            <div>
                                <h3 className="font-medium text-white">{provider.name}</h3>
                                <p className="text-sm text-bambu-gray">{t(`notifications.providerTypes.${provider.provider_type}`, provider.provider_type)}</p>
                            </div>
                        </div>

                        {/* Quick enable/disable toggle + Status indicator */}
                        <div className="flex items-center gap-3">
                            {provider.last_success && (
                                <span
                                    className="text-xs text-status-ok hidden sm:inline">{t('notifications.lastSuccess', {date: formatDateOnly(provider.last_success, undefined, dateFormat)})}</span>
                            )}
                            {/* Only show error if it's more recent than last success */}
                            {provider.last_error && provider.last_error_at && (
                                !provider.last_success || (parseUTCDate(provider.last_error_at)?.getTime() || 0) > (parseUTCDate(provider.last_success)?.getTime() || 0)
                            ) && (
                                <span className="text-xs text-status-error"
                                      title={provider.last_error}>{t('notifications.error')}</span>
                            )}
                            <Toggle
                                checked={provider.enabled}
                                onChange={(checked) => updateMutation.mutate({enabled: checked})}
                            />
                            <button
                                onClick={() => onEdit(provider)}
                                className="p-1.5 text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary rounded transition-colors"
                                title={t('notifications.edit')}
                            >
                                <Edit2 className="w-4 h-4"/>
                            </button>
                            <button
                                onClick={() => setShowDeleteConfirm(true)}
                                className="p-1.5 text-bambu-gray hover:text-red-600 dark:hover:text-red-400 hover:bg-bambu-dark-tertiary rounded transition-colors"
                                title={t('notifications.delete')}
                            >
                                <Trash2 className="w-4 h-4"/>
                            </button>
                        </div>
                    </div>

                    {/* Printer scope — all / one / several (m157 3b) */}
                    {provider.printer_ids != null ? (
                        <div className="mb-3 px-2 py-1.5 bg-bambu-dark rounded-lg">
                            <span className="text-xs text-bambu-gray">{t('notifications.printer')} </span>
                            <span className="text-sm text-white">
                                {linkedPrinters.length > 0
                                    ? linkedPrinters.map((p) => p.name).join(', ')
                                    : `#${provider.printer_ids.join(', #')}`}
                            </span>
                        </div>
                    ) : (
                        <div className="mb-3 px-2 py-1.5 bg-bambu-dark rounded-lg">
                            <span className="text-xs text-bambu-gray">{t('notifications.allPrinters')}</span>
                        </div>
                    )}

                    {/* Event summary - show all event tags. For Telegram the
                        on_* flags are forced True after m045 (per-chat
                        notify_events is the authority), so rendering them
                        here as 16 always-on badges would just add visual
                        noise — replace with a single hint chip. */}
                    <div className="mb-3 flex flex-wrap gap-1">
                        {isTelegram && (
                            <span className="px-2 py-0.5 bg-blue-500/15 text-blue-700 dark:text-blue-300 text-xs rounded inline-flex items-center gap-1">
                                <MessageCircle className="w-3 h-3" />
                                {t('notifications.telegram.eventsPerChatChip')}
                            </span>
                        )}
                        {/* One chip per subscribed event, from the same API list
                            the toggles below render. Hand-written chips covered
                            25 of the 34 flags — queue and sensor events had none
                            at all — and the colour said nothing a reader could
                            use, so it now says how serious the event is. */}
                        {!isTelegram && subscribedEvents.map((event) => (
                            <span
                                key={event.flag}
                                className={`px-2 py-0.5 text-xs rounded ${SEVERITY_CHIP[event.severity] ?? SEVERITY_CHIP.info}`}>
                                {eventLabel(event)}
                            </span>
                        ))}
                        {!isTelegram && provider.quiet_hours_enabled && (
                            <span
                                className="px-2 py-0.5 bg-indigo-100 dark:bg-indigo-500/20 text-indigo-700 dark:text-indigo-400 text-xs rounded flex items-center gap-1">
                <Moon className="w-3 h-3"/>
                                {t('notifications.quiet')}
              </span>
                        )}
                        {provider.daily_digest_enabled && (
                            <span
                                className="px-2 py-0.5 bg-emerald-100 dark:bg-emerald-500/20 text-emerald-700 dark:text-emerald-400 text-xs rounded flex items-center gap-1">
                <Calendar className="w-3 h-3"/>
                                {t('notifications.digest', {time: provider.daily_digest_time})}
              </span>
                        )}
                    </div>

                    {/* Test Button (not for Telegram) */}
                    {!isTelegram && (
                        <div className="mb-3">
                            <Button
                                size="sm"
                                variant="secondary"
                                disabled={testMutation.isPending}
                                onClick={() => {
                                    setTestResult(null);
                                    testMutation.mutate();
                                }}
                                className="w-full"
                            >
                                {testMutation.isPending ? (
                                    <Loader2 className="w-4 h-4 animate-spin"/>
                                ) : (
                                    <Send className="w-4 h-4"/>
                                )}
                                {t('notifications.sendTestNotification')}
                            </Button>
                        </div>
                    )}

                    {/* Test Result */}
                    {testResult && (
                        <div className={`mb-3 p-2 rounded-lg flex items-center gap-2 text-sm ${
                            testResult.success
                                ? 'bg-bambu-green/20 text-bambu-green'
                                : 'bg-red-100 dark:bg-red-500/20 text-red-700 dark:text-red-400'
                        }`}>
                            {testResult.success ? (
                                <CheckCircle className="w-4 h-4"/>
                            ) : (
                                <XCircle className="w-4 h-4"/>
                            )}
                            <span>{testResult.message}</span>
                        </div>
                    )}

                    {/* Toggle Settings Panel */}
                    <button
                        onClick={() => setIsExpanded(!isExpanded)}
                        className="w-full flex items-center justify-between py-2 text-sm text-bambu-gray hover:text-white transition-colors border-t border-bambu-dark-tertiary"
                    >
            <span className="flex items-center gap-2">
              <Settings2 className="w-4 h-4"/>
                {t('notifications.eventSettings')}
            </span>
                        {isExpanded ? (
                            <ChevronUp className="w-4 h-4"/>
                        ) : (
                            <ChevronDown className="w-4 h-4"/>
                        )}
                    </button>

                    {/* Expanded Settings */}
                    {isExpanded && (
                        <div className="pt-3 border-t border-bambu-dark-tertiary space-y-4">
                            {/* Enabled Toggle */}
                            <div className="flex items-center justify-between">
                                <div>
                                    <p className="text-sm text-white">{t('notifications.enabled')}</p>
                                    <p className="text-xs text-bambu-gray">{t('notifications.sendFromProvider')}</p>
                                </div>
                                <Toggle
                                    checked={provider.enabled}
                                    onChange={(checked) => updateMutation.mutate({enabled: checked})}
                                />
                            </div>

                            {/* Telegram: per-event opt-ins + quiet hours live on
                                each TelegramChat row after m045 — show a hint
                                here so the operator knows where to look. */}
                            {isTelegram && (
                                <div className="p-3 bg-blue-50 dark:bg-blue-500/10 border border-blue-300 dark:border-blue-500/30 rounded-lg flex items-start gap-2">
                                    <MessageCircle className="w-4 h-4 text-blue-600 dark:text-blue-400 flex-shrink-0 mt-0.5"/>
                                    <div className="text-xs text-bambu-gray space-y-1">
                                        <p className="text-white">{t('notifications.telegram.perChatHintTitle')}</p>
                                        <p>{t('notifications.telegram.perChatHintBody')}</p>
                                    </div>
                                </div>
                            )}

                            {/* Event toggles — provider-level, hidden for Telegram
                                (per-chat is the authority). The list comes from
                                the API, so a new flag appears here by itself;
                                the progress duration floor rides along as this
                                one flag's extra control. */}
                            {!isTelegram && (
                            <ProviderEventToggles
                                value={eventValues}
                                onChange={(flag, on) => updateMutation.mutate({[flag]: on})}
                                extras={{
                                    on_print_progress: (
                                        /* Per-provider duration floor (#28): each provider
                                           carries its own value, empty or 0 = always send. */
                                        <p className="mt-0.5 flex items-center gap-1.5 text-xs text-bambu-gray">
                                            {t('notifications.progressFloorLabel')}
                                            <input
                                                key={`floor-${provider.id}-${provider.progress_min_duration_minutes ?? 'inherit'}`}
                                                type="number"
                                                min={0}
                                                max={10080}
                                                step={5}
                                                defaultValue={provider.progress_min_duration_minutes ?? ''}
                                                placeholder="0"
                                                onKeyDown={(e) => {
                                                    if (e.key === 'Enter') e.currentTarget.blur();
                                                }}
                                                onBlur={(e) => {
                                                    const raw = e.currentTarget.value;
                                                    const next = raw === '' ? null : Math.min(10080, Math.max(0, parseInt(raw) || 0));
                                                    if (next !== (provider.progress_min_duration_minutes ?? null)) {
                                                        updateMutation.mutate({progress_min_duration_minutes: next});
                                                    }
                                                }}
                                                className="w-16 px-1.5 py-0.5 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-xs text-center focus:border-bambu-green focus:outline-none"
                                            />
                                            {t('notifications.progressFloorUnit')}
                                        </p>
                                    ),
                                }}
                            />
                            )}

                            {/* Quiet Hours - hidden for Telegram (per-chat setting) */}
                            {!isTelegram && (
                            <div className="space-y-2">
                                <div className="flex items-center justify-between">
                                    <div className="flex items-center gap-2">
                                        <Moon className="w-4 h-4 text-purple-600 dark:text-purple-400"/>
                                        <p className="text-sm text-white">{t('notifications.quietHours')}</p>
                                    </div>
                                    <Toggle
                                        checked={provider.quiet_hours_enabled}
                                        onChange={(checked) => updateMutation.mutate({quiet_hours_enabled: checked})}
                                    />
                                </div>

                                {provider.quiet_hours_enabled && (
                                    <div className="pl-4 border-l-2 border-bambu-dark-tertiary space-y-2">
                                        <p className="text-xs text-bambu-gray">{t('notifications.noNotificationsDuring')}</p>
                                        <div className="flex items-center gap-2">
                                            <Clock className="w-4 h-4 text-bambu-gray"/>
                                            <span className="text-sm text-white">
                        {formatTime(provider.quiet_hours_start) || '22:00'} - {formatTime(provider.quiet_hours_end) || '07:00'}
                      </span>
                                        </div>
                                        <p className="text-xs text-bambu-gray">{t('notifications.editProviderToChangeQuietHours')}</p>
                                    </div>
                                )}
                            </div>
                            )}

                            {/* Daily Digest */}
                            <div className="space-y-2">
                                <div className="flex items-center justify-between">
                                    <div className="flex items-center gap-2">
                                        <Calendar className="w-4 h-4 text-emerald-600 dark:text-emerald-400"/>
                                        <p className="text-sm text-white">{t('notifications.dailyDigest')}</p>
                                    </div>
                                    <Toggle
                                        checked={provider.daily_digest_enabled}
                                        onChange={(checked) => updateMutation.mutate({daily_digest_enabled: checked})}
                                    />
                                </div>

                                {provider.daily_digest_enabled && (
                                    <div className="pl-4 border-l-2 border-bambu-dark-tertiary space-y-2">
                                        <p className="text-xs text-bambu-gray">{t('notifications.batchNotifications')}</p>
                                        <div className="flex items-center gap-2">
                                            <Clock className="w-4 h-4 text-bambu-gray"/>
                                            <span className="text-sm text-white">
                        {t('notifications.sendAt', {time: formatTime(provider.daily_digest_time) || '08:00'})}
                      </span>
                                        </div>
                                        <p className="text-xs text-bambu-gray">{t('notifications.editProviderToChangeDigestTime')}</p>
                                    </div>
                                )}
                            </div>

                            {/* Action Buttons */}
                            <div className="flex gap-2 pt-2">
                                <Button
                                    size="sm"
                                    variant="secondary"
                                    onClick={() => onEdit(provider)}
                                    className="flex-1"
                                >
                                    <Edit2 className="w-4 h-4"/>
                                    {t('notifications.edit')}
                                </Button>
                                <Button
                                    size="sm"
                                    variant="secondary"
                                    onClick={() => setShowDeleteConfirm(true)}
                                    className="text-red-700 dark:text-red-400 hover:text-red-800 dark:hover:text-red-300"
                                >
                                    <Trash2 className="w-4 h-4"/>
                                </Button>
                            </div>
                        </div>
                    )}
                </CardContent>

                {/* Telegram Chats - inside the same card */}
                {isTelegram && (
                    <div className="p-4 p-4 pt-0">
                        <div className="pt-3 border-t border-bambu-dark-tertiary">
                            <div className="flex items-center justify-between mb-3">
                                <h4 className="text-sm font-medium text-white flex items-center gap-2">
                                    <MessageCircle className="w-3.5 h-3.5 text-bambu-green"/>
                                    {t('telegram.title')}
                                </h4>
                                <div className="flex items-center gap-2">
                                    <RegistrationModeToggle />
                                    <Button size="sm" onClick={() => {
                                        setEditingTelegramChat(null);
                                        setShowTelegramChatModal(true);
                                    }}>
                                        <Plus className="w-3.5 h-3.5"/>
                                        {t('telegram.addChat')}
                                    </Button>
                                </div>
                            </div>

                            {telegramChatsLoading ? (
                                <div className="flex justify-center py-4">
                                    <Loader2 className="w-5 h-5 text-bambu-green animate-spin"/>
                                </div>
                            ) : providerChats.length > 0 ? (
                                <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-2">
                                    {providerChats.map((chat) => (
                                        <TelegramChatCard
                                            key={chat.id}
                                            chat={chat}
                                            onEdit={(c) => {
                                                setEditingTelegramChat(c);
                                                setShowTelegramChatModal(true);
                                            }}
                                        />
                                    ))}
                                </div>
                            ) : (
                                <div className="text-center text-bambu-gray py-4">
                                    <p className="text-xs mb-2">{t('telegram.noChatsDescription')}</p>
                                    <Button size="sm" onClick={() => {
                                        setEditingTelegramChat(null);
                                        setShowTelegramChatModal(true);
                                    }}>
                                        <Plus className="w-3.5 h-3.5"/>
                                        {t('telegram.addChat')}
                                    </Button>
                                </div>
                            )}
                        </div>
                    </div>
                )}
            </Card>

            {/* Telegram Chat Modal */}
            {showTelegramChatModal && (
                <AddTelegramChatModal
                    chat={editingTelegramChat}
                    providerId={provider.id}
                    onClose={() => {
                        setShowTelegramChatModal(false);
                        setEditingTelegramChat(null);
                    }}
                />
            )}

            {/* Delete Confirmation */}
            {showDeleteConfirm && (
                <ConfirmModal
                    title={t('notifications.deleteProvider')}
                    message={t('notifications.deleteConfirm', {name: provider.name})}
                    confirmText={t('notifications.delete')}
                    variant="danger"
                    onConfirm={() => {
                        deleteMutation.mutate();
                        setShowDeleteConfirm(false);
                    }}
                    onCancel={() => setShowDeleteConfirm(false)}
                />
            )}
        </>
    );
}
