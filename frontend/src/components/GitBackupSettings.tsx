import { useState, useEffect, useRef, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery, useQueries, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Play,
  Clock,
  CheckCircle,
  XCircle,
  Loader2,
  ExternalLink,
  RefreshCw,
  Download,
  Upload,
  Database,
  History,
  SkipForward,
  AlertTriangle,
  Trash2,
  RotateCcw,
} from 'lucide-react';
import { GitHubIcon, GitLabIcon } from './BrandIcons';
import { api } from '../api/client';
import type {
  GitBackupConfig,
  GitBackupConfigCreate,
  GitBackupLog,
  GitBackupStatus,
  GitBackupTriggerResponse,
  ScheduleType,
  CloudAuthStatus,
  Printer,
  LocalBackupStatus,
  LocalBackupFile,
} from '../api/client';
import { Card, CardContent, CardHeader } from './Card';
import { Button } from './Button';
import { Toggle } from './Toggle';
import { ConfirmModal } from './ConfirmModal';
import { useToast } from '../contexts/ToastContext';
import { formatDateTime as fmtDateTime, formatRelativeTime, type DateFormat, type TimeFormat } from '../utils/date';

type GitProvider = 'github' | 'gitlab' | 'gitea' | 'forgejo';

/**
 * Wrapper that returns ``-`` for null and threads the user's date/time
 * preferences through. Replaces the old local helper that called
 * ``date.toLocaleString()`` and ignored settings entirely.
 */
function formatDateTime(dateStr: string | null, timeFormat: TimeFormat = 'system', dateFormat: DateFormat = 'system'): string {
  if (!dateStr) return '-';
  return fmtDateTime(dateStr, timeFormat, dateFormat) || '-';
}

interface StatusBadgeProps {
  status: string | null;
}

function StatusBadge({ status }: StatusBadgeProps) {
  if (!status) return null;

  const styles: Record<string, string> = {
    success: 'bg-green-500/20 text-green-400',
    failed: 'bg-red-500/20 text-red-400',
    skipped: 'bg-yellow-500/20 text-yellow-400',
    running: 'bg-blue-500/20 text-blue-400',
  };

  const icons: Record<string, React.ReactNode> = {
    success: <CheckCircle className="w-3 h-3" />,
    failed: <XCircle className="w-3 h-3" />,
    skipped: <SkipForward className="w-3 h-3" />,
    running: <Loader2 className="w-3 h-3 animate-spin" />,
  };

  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium ${styles[status] || 'bg-gray-500/20 text-gray-400'}`}>
      {icons[status]}
      {status.charAt(0).toUpperCase() + status.slice(1)}
    </span>
  );
}

export function GitBackupSettings() {
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { t } = useTranslation();

  // Pull system time/date format so backup status timestamps follow the
  // user's preference instead of falling through to the browser locale.
  const { data: settings } = useQuery({
    queryKey: ['settings'],
    queryFn: api.getSettings,
    staleTime: 60_000,
  });
  const timeFormat = (settings?.time_format ?? 'system') as TimeFormat;
  const dateFormat = (settings?.date_format ?? 'system') as DateFormat;

  // Local state for form
  const [provider, setProvider] = useState<GitProvider>('github');
  const [apiBaseUrl, setApiBaseUrl] = useState('');
  const [repoUrl, setRepoUrl] = useState('');
  const [accessToken, setAccessToken] = useState('');
  const [branch, setBranch] = useState('main');
  const [scheduleEnabled, setScheduleEnabled] = useState(false);
  const [scheduleType, setScheduleType] = useState<ScheduleType>('daily');
  const [backupKProfiles, setBackupKProfiles] = useState(true);
  const [backupCloudProfiles, setBackupCloudProfiles] = useState(true);
  const [backupSettings, setBackupSettings] = useState(false);
  const [backupSpools, setBackupSpools] = useState(false);
  const [backupArchives, setBackupArchives] = useState(false);
  const [enabled, setEnabled] = useState(true);

  // Local backup (manual download) state
  const [isExporting, setIsExporting] = useState(false);
  const [isRestoring, setIsRestoring] = useState(false);

  // Scheduled local backup state (#884)
  const [localBackupPath, setLocalBackupPath] = useState('');
  const [scheduledRestoreFile, setScheduledRestoreFile] = useState<string | null>(null);
  const [scheduledDeleteFile, setScheduledDeleteFile] = useState<string | null>(null);
  const { data: localBackupStatus, refetch: refetchLocalStatus } = useQuery<LocalBackupStatus>({
    queryKey: ['local-backup-status'],
    queryFn: () => api.getLocalBackupStatus(),
    refetchInterval: 30000,
  });
  const { data: localBackups, refetch: refetchLocalBackups } = useQuery<LocalBackupFile[]>({
    queryKey: ['local-backup-files'],
    queryFn: () => api.listLocalBackups(),
    refetchInterval: 30000,
  });
  useEffect(() => {
    if (localBackupStatus?.path !== undefined) {
      setLocalBackupPath(localBackupStatus.path);
    }
  }, [localBackupStatus?.path]);
  const [operationStatus, setOperationStatus] = useState<string>('');
  const [showRestoreConfirm, setShowRestoreConfirm] = useState(false);
  const [restoreFile, setRestoreFile] = useState<File | null>(null);
  const [restoreResult, setRestoreResult] = useState<{ success: boolean; message: string } | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Block navigation while backup/restore is in progress
  useEffect(() => {
    const isOperationInProgress = isExporting || isRestoring;

    if (isOperationInProgress) {
      const handleBeforeUnload = (e: BeforeUnloadEvent) => {
        e.preventDefault();
        e.returnValue = 'A backup operation is in progress. Are you sure you want to leave?';
        return e.returnValue;
      };

      window.addEventListener('beforeunload', handleBeforeUnload);
      return () => window.removeEventListener('beforeunload', handleBeforeUnload);
    }
  }, [isExporting, isRestoring]);

  // Test connection state
  const [testLoading, setTestLoading] = useState(false);
  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null);

  // Auto-save debounce
  const autoSaveTimerRef = useRef<NodeJS.Timeout | null>(null);
  const isInitializedRef = useRef(false);

  // Provider display name
  const providerName =
    provider === 'github'
      ? 'GitHub'
      : provider === 'gitlab'
        ? 'GitLab'
        : provider === 'gitea'
          ? 'Gitea'
          : 'Forgejo';
  // Gitea + Forgejo render with the GitHub icon as a placeholder until we ship
  // dedicated brand icons — both projects use a Git Data API derived from
  // GitHub's, so the visual cue is closer to GitHub than to GitLab.
  const ProviderIcon =
    provider === 'github'
      ? GitHubIcon
      : provider === 'gitlab'
        ? GitLabIcon
        : GitHubIcon;

  // Queries
  const { data: config, isLoading: configLoading } = useQuery<GitBackupConfig | null>({
    queryKey: ['git-backup-config'],
    queryFn: api.getGitBackupConfig,
  });

  const { data: status } = useQuery<GitBackupStatus>({
    queryKey: ['git-backup-status'],
    queryFn: api.getGitBackupStatus,
    refetchInterval: (query) => query.state.data?.is_running ? 500 : 10000, // Poll fast during backup
  });

  const { data: logs } = useQuery<GitBackupLog[]>({
    queryKey: ['git-backup-logs'],
    queryFn: () => api.getGitBackupLogs(20),
  });

  const { data: cloudStatus } = useQuery<CloudAuthStatus>({
    queryKey: ['cloud-status'],
    queryFn: api.getCloudStatus,
  });

  // Fetch printers and their statuses for K-profile availability
  const { data: printers } = useQuery<Printer[]>({
    queryKey: ['printers'],
    queryFn: api.getPrinters,
  });

  // Fetch printer statuses from API (not just cache) to get accurate connection status
  const printerStatusQueries = useQueries({
    queries: (printers ?? []).map(printer => ({
      queryKey: ['printerStatus', printer.id],
      queryFn: () => api.getPrinterStatus(printer.id),
      staleTime: 10000, // Consider stale after 10s
      refetchInterval: 30000, // Refresh every 30s
    })),
  });

  const printerStatuses = (printers ?? []).map((printer, index) => ({
    printer,
    connected: printerStatusQueries[index]?.data?.connected ?? false,
  }));

  const totalPrinters = printerStatuses.length;
  const connectedPrinters = printerStatuses.filter(p => p.connected).length;
  const noPrintersConnected = totalPrinters > 0 && connectedPrinters === 0;
  const somePrintersDisconnected = connectedPrinters > 0 && connectedPrinters < totalPrinters;

  // Initialize form from config
  useEffect(() => {
    if (config) {
      setProvider((config.provider as GitProvider) || 'github');
      setApiBaseUrl(config.api_base_url || '');
      setRepoUrl(config.repository_url);
      setBranch(config.branch);
      setScheduleEnabled(config.schedule_enabled);
      setScheduleType(config.schedule_type);
      setBackupKProfiles(config.backup_kprofiles);
      setBackupCloudProfiles(config.backup_cloud_profiles);
      setBackupSettings(config.backup_settings);
      setBackupSpools(config.backup_spools);
      setBackupArchives(config.backup_archives);
      setEnabled(config.enabled);
      setAccessToken(''); // Don't show stored token
      // Mark as initialized after a tick to avoid auto-save on initial load
      setTimeout(() => { isInitializedRef.current = true; }, 100);
    }
  }, [config]);

  // Auto-save function for existing configs
  const autoSave = useCallback(async (includeToken: boolean = false) => {
    if (!config?.has_token) return; // Only auto-save if config already exists

    try {
      if (includeToken && accessToken) {
        // Full save with new token
        await api.saveGitBackupConfig({
          repository_url: repoUrl,
          access_token: accessToken,
          branch,
          schedule_enabled: scheduleEnabled,
          schedule_type: scheduleType,
          backup_kprofiles: backupKProfiles,
          backup_cloud_profiles: backupCloudProfiles,
          backup_settings: backupSettings,
          backup_spools: backupSpools,
          backup_archives: backupArchives,
          enabled,
          provider,
          api_base_url: apiBaseUrl || null,
        });
        setAccessToken(''); // Clear after save
        showToast(t('backup.tokenUpdated'));
      } else {
        // Update without token
        await api.updateGitBackupConfig({
          repository_url: repoUrl,
          branch,
          schedule_enabled: scheduleEnabled,
          schedule_type: scheduleType,
          backup_kprofiles: backupKProfiles,
          backup_cloud_profiles: backupCloudProfiles,
          backup_settings: backupSettings,
          backup_spools: backupSpools,
          backup_archives: backupArchives,
          enabled,
          provider,
          api_base_url: apiBaseUrl || null,
        });
        showToast(t('backup.settingsSaved'));
      }
      queryClient.invalidateQueries({ queryKey: ['git-backup-config'] });
      queryClient.invalidateQueries({ queryKey: ['git-backup-status'] });
    } catch (error) {
      showToast(t('backup.failedToSave', { message: (error as Error).message }), 'error');
    }
  }, [config?.has_token, repoUrl, accessToken, branch, scheduleEnabled, scheduleType, backupKProfiles, backupCloudProfiles, backupSettings, backupSpools, backupArchives, enabled, provider, apiBaseUrl, queryClient, showToast, t]);

  // Auto-save effect for existing configs (debounced)
  useEffect(() => {
    if (!isInitializedRef.current || !config?.has_token) return;

    if (autoSaveTimerRef.current) {
      clearTimeout(autoSaveTimerRef.current);
    }

    autoSaveTimerRef.current = setTimeout(() => {
      autoSave(false);
    }, 500);

    return () => {
      if (autoSaveTimerRef.current) {
        clearTimeout(autoSaveTimerRef.current);
      }
    };
  }, [repoUrl, branch, scheduleEnabled, scheduleType, backupKProfiles, backupCloudProfiles, backupSettings, backupSpools, backupArchives, enabled, provider, apiBaseUrl, autoSave, config?.has_token]);

  // Auto-save token when it changes (with longer debounce)
  useEffect(() => {
    if (!isInitializedRef.current || !config?.has_token || !accessToken) return;

    if (autoSaveTimerRef.current) {
      clearTimeout(autoSaveTimerRef.current);
    }

    autoSaveTimerRef.current = setTimeout(() => {
      autoSave(true);
    }, 1000);

    return () => {
      if (autoSaveTimerRef.current) {
        clearTimeout(autoSaveTimerRef.current);
      }
    };
  }, [accessToken, autoSave, config?.has_token]);

  // Mutations
  const saveConfigMutation = useMutation({
    mutationFn: (data: GitBackupConfigCreate) => api.saveGitBackupConfig(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['git-backup-config'] });
      queryClient.invalidateQueries({ queryKey: ['git-backup-status'] });
      showToast(t('backup.gitBackupEnabled', { provider: providerName }));
      setAccessToken('');
      isInitializedRef.current = true;
    },
    onError: (error: Error) => {
      showToast(t('backup.failedToSave', { message: error.message }), 'error');
    },
  });

  const triggerBackupMutation = useMutation<GitBackupTriggerResponse, Error>({
    mutationFn: api.triggerGitBackup,
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['git-backup-status'] });
      queryClient.invalidateQueries({ queryKey: ['git-backup-logs'] });
      if (result.success) {
        if (result.files_changed > 0) {
          showToast(t('backup.backupCompleteFiles', { count: result.files_changed }));
        } else {
          showToast(t('backup.backupSkippedNoChanges'));
        }
      } else {
        showToast(t('backup.backupFailed2', { message: result.message }), 'error');
      }
    },
    onError: (error: Error) => {
      showToast(t('backup.backupFailed2', { message: error.message }), 'error');
    },
  });

  const clearLogsMutation = useMutation<{ deleted: number; message: string }, Error>({
    mutationFn: () => api.clearGitBackupLogs(0),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['git-backup-logs'] });
      showToast(t('backup.clearedLogs', { count: result.deleted }));
    },
    onError: (error: Error) => {
      showToast(t('backup.failedToClearLogs', { message: error.message }), 'error');
    },
  });

  const handleTestConnection = async () => {
    setTestLoading(true);
    setTestResult(null);
    try {
      let result;
      // If user entered a new token, test with those credentials
      if (accessToken) {
        if (!repoUrl) {
          showToast(t('backup.enterRepoUrl'), 'error');
          setTestLoading(false);
          return;
        }
        result = await api.testGitConnection(repoUrl, accessToken, provider, apiBaseUrl || undefined);
      } else if (config?.has_token) {
        // Use stored credentials
        result = await api.testStoredGitConnection();
      } else {
        showToast(t('backup.enterRepoAndToken'), 'error');
        setTestLoading(false);
        return;
      }
      setTestResult({ success: result.success, message: result.message });
    } catch (error) {
      setTestResult({ success: false, message: (error as Error).message });
    } finally {
      setTestLoading(false);
    }
  };

  // Initial setup save (only for new configs)
  const handleInitialSetup = () => {
    if (!repoUrl) {
      showToast(t('backup.repoRequired'), 'error');
      return;
    }
    if (!accessToken) {
      showToast(t('backup.tokenRequired'), 'error');
      return;
    }

    saveConfigMutation.mutate({
      repository_url: repoUrl,
      access_token: accessToken,
      branch,
      schedule_enabled: scheduleEnabled,
      schedule_type: scheduleType,
      backup_kprofiles: backupKProfiles,
      backup_cloud_profiles: backupCloudProfiles,
      backup_settings: backupSettings,
      backup_spools: backupSpools,
      backup_archives: backupArchives,
      enabled,
      provider,
      api_base_url: apiBaseUrl || null,
    });
  };

  if (configLoading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="w-8 h-8 animate-spin text-bambu-green" />
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
      {/* Left Column - Git Backup */}
      <div className="space-y-4">
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <ProviderIcon className="w-5 h-5 text-gray-400" />
                <h2 className="text-lg font-semibold text-white">{t('backup.gitBackup')}</h2>
              </div>
              {config && (
                <div className="flex items-center gap-2">
                  <span className="text-sm text-bambu-gray">{t('backup.enabled')}</span>
                  <Toggle
                    checked={enabled}
                    onChange={setEnabled}
                  />
                </div>
              )}
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
                <p className="text-sm text-bambu-gray">
                  {t('backup.gitDescription')}
                </p>

                {/* Provider selector */}
                <div>
                  <label className="block text-sm text-bambu-gray mb-1">
                    {t('backup.providerLabel')}
                  </label>
                  <div className="flex gap-3">
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input
                        type="radio"
                        name="provider"
                        value="github"
                        checked={provider === 'github'}
                        onChange={() => { setProvider('github'); setTestResult(null); }}
                        className="w-4 h-4 text-bambu-green focus:ring-bambu-green bg-bambu-dark border-bambu-dark-tertiary"
                      />
                      <GitHubIcon className="w-4 h-4 text-white" />
                      <span className="text-sm text-white">{t('backup.providerGitHub')}</span>
                    </label>
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input
                        type="radio"
                        name="provider"
                        value="gitlab"
                        checked={provider === 'gitlab'}
                        onChange={() => { setProvider('gitlab'); setTestResult(null); }}
                        className="w-4 h-4 text-bambu-green focus:ring-bambu-green bg-bambu-dark border-bambu-dark-tertiary"
                      />
                      <GitLabIcon className="w-4 h-4 text-white" />
                      <span className="text-sm text-white">{t('backup.providerGitLab')}</span>
                    </label>
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input
                        type="radio"
                        name="provider"
                        value="gitea"
                        checked={provider === 'gitea'}
                        onChange={() => { setProvider('gitea'); setTestResult(null); }}
                        className="w-4 h-4 text-bambu-green focus:ring-bambu-green bg-bambu-dark border-bambu-dark-tertiary"
                      />
                      <GitHubIcon className="w-4 h-4 text-white" />
                      <span className="text-sm text-white">{t('backup.providerGitea')}</span>
                    </label>
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input
                        type="radio"
                        name="provider"
                        value="forgejo"
                        checked={provider === 'forgejo'}
                        onChange={() => { setProvider('forgejo'); setTestResult(null); }}
                        className="w-4 h-4 text-bambu-green focus:ring-bambu-green bg-bambu-dark border-bambu-dark-tertiary"
                      />
                      <GitHubIcon className="w-4 h-4 text-white" />
                      <span className="text-sm text-white">{t('backup.providerForgejo')}</span>
                    </label>
                  </div>
                </div>

                {/* GitLab API Base URL */}
                {provider === 'gitlab' && (
                  <div>
                    <label className="block text-sm text-bambu-gray mb-1">
                      {t('backup.apiBaseUrlLabel')}
                    </label>
                    <input
                      type="text"
                      value={apiBaseUrl}
                      onChange={(e) => { setApiBaseUrl(e.target.value); setTestResult(null); }}
                      placeholder={t('backup.apiBaseUrlPlaceholder')}
                      className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                    />
                    <p className="text-xs text-bambu-gray mt-1">
                      {t('backup.apiBaseUrlHint')}
                    </p>
                  </div>
                )}

                {/* Repository URL */}
                <div>
                  <label className="block text-sm text-bambu-gray mb-1">
                    {t('backup.repositoryUrl')}
                  </label>
                  <input
                    type="text"
                    value={repoUrl}
                    onChange={(e) => { setRepoUrl(e.target.value); setTestResult(null); }}
                    placeholder={
                      provider === 'github'
                        ? 'https://github.com/username/repo'
                        : provider === 'gitlab'
                          ? 'https://gitlab.com/group/project'
                          : 'https://your-host/owner/repo'
                    }
                    className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                  />
                </div>

                {/* Access Token */}
                <div>
                  <label className="block text-sm text-bambu-gray mb-1">
                    {t('backup.personalAccessToken')} {config?.has_token && <span className="text-green-400">{t('backup.tokenSaved')}</span>}
                  </label>
                  <input
                    type="password"
                    value={accessToken}
                    onChange={(e) => { setAccessToken(e.target.value); setTestResult(null); }}
                    placeholder={
                      config?.has_token
                        ? t('backup.enterNewToken')
                        : provider === 'github'
                          ? 'ghp_xxxxxxxxxxxx'
                          : provider === 'gitlab'
                            ? 'glpat-xxxxxxxxxxxx'
                            : 'token (Gitea / Forgejo personal access token)'
                    }
                    className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                  />
                  <p className="text-xs text-bambu-gray mt-1">
                    {provider === 'github'
                      ? t('backup.tokenHintGitHub')
                      : provider === 'gitlab'
                        ? t('backup.tokenHintGitLab')
                        : t('backup.tokenHintGitea')}
                  </p>
                </div>

            {/* Branch - inline with schedule */}
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-sm text-bambu-gray mb-1">{t('backup.branch')}</label>
                <input
                  type="text"
                  value={branch}
                  onChange={(e) => setBranch(e.target.value)}
                  placeholder="main"
                  className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                />
              </div>
              <div>
                <label className="block text-sm text-bambu-gray mb-1">{t('backup.autoBackup')}</label>
                <select
                  value={scheduleEnabled ? scheduleType : 'disabled'}
                  onChange={(e) => {
                    if (e.target.value === 'disabled') {
                      setScheduleEnabled(false);
                    } else {
                      setScheduleEnabled(true);
                      setScheduleType(e.target.value as ScheduleType);
                    }
                  }}
                  className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                >
                  <option value="disabled">{t('backup.manualOnly')}</option>
                  <option value="hourly">{t('backup.hourly')}</option>
                  <option value="daily">{t('backup.daily')}</option>
                  <option value="weekly">{t('backup.weekly')}</option>
                </select>
              </div>
            </div>

            {/* What to backup */}
            <div>
              <label className="block text-sm text-bambu-gray mb-2">{t('backup.includeInBackup')}</label>
              <div className="space-y-2">
                <label className={`flex items-start gap-2 ${noPrintersConnected ? 'cursor-not-allowed opacity-60' : 'cursor-pointer'}`}>
                  <input
                    type="checkbox"
                    checked={backupKProfiles}
                    onChange={(e) => setBackupKProfiles(e.target.checked)}
                    className="w-4 h-4 mt-0.5 rounded border-bambu-dark-tertiary bg-bambu-dark text-bambu-green focus:ring-bambu-green"
                    disabled={noPrintersConnected}
                  />
                  <div className="flex-1">
                    <div className="flex items-center gap-2">
                      <span className={`text-sm ${noPrintersConnected ? 'text-bambu-gray' : 'text-white'}`}>{t('backup.kProfiles')}</span>
                      {noPrintersConnected && (
                        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs bg-yellow-500/20 text-yellow-400">
                          <AlertTriangle className="w-3 h-3" />
                          {t('backup.noPrintersConnected')}
                        </span>
                      )}
                      {somePrintersDisconnected && (
                        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs bg-yellow-500/20 text-yellow-400">
                          <AlertTriangle className="w-3 h-3" />
                          {t('backup.printersConnected', { connected: connectedPrinters, total: totalPrinters })}
                        </span>
                      )}
                    </div>
                    <p className="text-xs text-bambu-gray">{t('backup.kProfilesDescription')}</p>
                  </div>
                </label>
                <label className={`flex items-start gap-2 ${!cloudStatus?.is_authenticated ? 'cursor-not-allowed opacity-60' : 'cursor-pointer'}`}>
                  <input
                    type="checkbox"
                    checked={backupCloudProfiles}
                    onChange={(e) => setBackupCloudProfiles(e.target.checked)}
                    className="w-4 h-4 mt-0.5 rounded border-bambu-dark-tertiary bg-bambu-dark text-bambu-green focus:ring-bambu-green"
                    disabled={!cloudStatus?.is_authenticated}
                  />
                  <div>
                    <div className="flex items-center gap-2">
                      <span className={`text-sm ${cloudStatus?.is_authenticated ? 'text-white' : 'text-bambu-gray'}`}>{t('backup.cloudProfiles')}</span>
                      {!cloudStatus?.is_authenticated && (
                        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs bg-yellow-500/20 text-yellow-400">
                          <AlertTriangle className="w-3 h-3" />
                          {t('backup.cloudLoginRequiredShort')}
                        </span>
                      )}
                    </div>
                    <p className="text-xs text-bambu-gray">{t('backup.cloudProfilesDescription')}</p>
                  </div>
                </label>
                <label className="flex items-start gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={backupSettings}
                    onChange={(e) => setBackupSettings(e.target.checked)}
                    className="w-4 h-4 mt-0.5 rounded border-bambu-dark-tertiary bg-bambu-dark text-bambu-green focus:ring-bambu-green"
                  />
                  <div>
                    <span className="text-white text-sm">{t('backup.appSettings')}</span>
                    <p className="text-xs text-bambu-gray">{t('backup.appSettingsDescription')}</p>
                  </div>
                </label>
                <label className="flex items-start gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={backupSpools}
                    onChange={(e) => setBackupSpools(e.target.checked)}
                    className="w-4 h-4 mt-0.5 rounded border-bambu-dark-tertiary bg-bambu-dark text-bambu-green focus:ring-bambu-green"
                  />
                  <div>
                    <span className="text-white text-sm">{t('backup.backupSpools')}</span>
                    <p className="text-xs text-bambu-gray">{t('backup.backupSpoolsHint')}</p>
                  </div>
                </label>
                <label className="flex items-start gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={backupArchives}
                    onChange={(e) => setBackupArchives(e.target.checked)}
                    className="w-4 h-4 mt-0.5 rounded border-bambu-dark-tertiary bg-bambu-dark text-bambu-green focus:ring-bambu-green"
                  />
                  <div>
                    <span className="text-white text-sm">{t('backup.backupArchives')}</span>
                    <p className="text-xs text-bambu-gray">{t('backup.backupArchivesHint')}</p>
                  </div>
                </label>
              </div>
            </div>

            {/* Test + Status + Actions */}
            <div className="border-t border-bambu-dark-tertiary pt-4 space-y-3">
              {/* Status line */}
              {status?.configured && (
                <div className="flex items-center justify-between text-sm">
                  <div className="flex items-center gap-2 text-bambu-gray">
                    {status.last_backup_at ? (
                      <>
                        <span>{t('backup.lastBackupAt')} {formatRelativeTime(status.last_backup_at, 'system', t)}</span>
                        <StatusBadge status={status.last_backup_status} />
                      </>
                    ) : (
                      <span>{t('backup.noBackupsYet')}</span>
                    )}
                  </div>
                  {status.next_scheduled_run && (
                    <span className="text-bambu-gray">
                      <Clock className="w-3 h-3 inline mr-1" />
                      {t('backup.next')} {formatRelativeTime(status.next_scheduled_run, 'system', t)}
                    </span>
                  )}
                </div>
              )}

              {/* Test result */}
              {testResult && (
                <div className={`text-sm flex items-center gap-1 ${testResult.success ? 'text-green-400' : 'text-red-400'}`}>
                  {testResult.success ? <CheckCircle className="w-4 h-4" /> : <XCircle className="w-4 h-4" />}
                  {testResult.message}
                </div>
              )}

              {/* Action buttons */}
              <div className="flex flex-wrap items-center gap-2">
                {status?.configured ? (
                  <>
                    {(triggerBackupMutation.isPending || status.is_running) ? (
                      <div className="flex items-center gap-2 text-bambu-green">
                        <Loader2 className="w-4 h-4 animate-spin" />
                        <span className="text-sm">{status.progress || t('backup.startingBackup')}</span>
                      </div>
                    ) : (
                      <>
                        <Button
                          variant="primary"
                          size="sm"
                          onClick={() => triggerBackupMutation.mutate()}
                          disabled={!config?.enabled}
                        >
                          <Play className="w-4 h-4" />
                          {t('backup.backupNow')}
                        </Button>
                        <Button
                          variant="secondary"
                          size="sm"
                          onClick={handleTestConnection}
                          disabled={testLoading}
                        >
                          {testLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
                          {t('backup.test')}
                        </Button>
                      </>
                    )}
                  </>
                ) : (
                  <>
                    <Button
                      variant="primary"
                      size="sm"
                      onClick={handleInitialSetup}
                      disabled={saveConfigMutation.isPending || !repoUrl || !accessToken}
                    >
                      {saveConfigMutation.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCircle className="w-4 h-4" />}
                      {t('backup.enableBackup')}
                    </Button>
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={handleTestConnection}
                      disabled={testLoading || !repoUrl || !accessToken}
                    >
                      {testLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
                      {t('backup.testConnection')}
                    </Button>
                  </>
                )}
              </div>
            </div>
          </CardContent>
        </Card>

        {/* Backup History - only show if configured and has logs */}
        {logs && logs.length > 0 && (
          <Card>
            <CardHeader>
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <History className="w-5 h-5 text-gray-400" />
                  <h2 className="text-lg font-semibold text-white">{t('backup.history')}</h2>
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => clearLogsMutation.mutate()}
                  disabled={clearLogsMutation.isPending}
                >
                  <Trash2 className="w-4 h-4" />
                  {t('backup.clear')}
                </Button>
              </div>
            </CardHeader>
            <CardContent>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-bambu-gray border-b border-bambu-dark-tertiary">
                      <th className="text-left py-2 px-2">{t('backup.date')}</th>
                      <th className="text-left py-2 px-2">{t('backup.status')}</th>
                      <th className="text-left py-2 px-2">{t('backup.commit')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {logs.slice(0, 10).map((log) => (
                      <tr key={log.id} className="border-b border-bambu-dark-tertiary/50 hover:bg-bambu-dark-secondary">
                        <td className="py-2 px-2 text-white">{formatDateTime(log.started_at, timeFormat, dateFormat)}</td>
                        <td className="py-2 px-2"><StatusBadge status={log.status} /></td>
                        <td className="py-2 px-2">
                          {log.commit_sha ? (
                            <a
                              href={`${config?.repository_url}/commit/${log.commit_sha}`}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="text-bambu-green hover:underline inline-flex items-center gap-1"
                            >
                              {log.commit_sha.substring(0, 7)}
                              <ExternalLink className="w-3 h-3" />
                            </a>
                          ) : (
                            <span className="text-bambu-gray">-</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </CardContent>
          </Card>
        )}
      </div>

      {/* Right Column - Local Backup */}
      <div className="space-y-4">
        <Card>
          <CardHeader>
            <div className="flex items-center gap-2">
              <Database className="w-5 h-5 text-gray-400" />
              <h2 className="text-lg font-semibold text-white">{t('backup.localBackup')}</h2>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <p className="text-sm text-bambu-gray">
              {t('backup.localBackupDescription')}
            </p>

            {/* Export */}
            <div className="flex items-center justify-between py-3 border-b border-bambu-dark-tertiary">
              <div>
                <p className="text-white">{t('backup.downloadBackupLabel')}</p>
                <p className="text-sm text-bambu-gray">
                  {t('backup.completeBackupZip')}
                </p>
              </div>
              <Button
                variant="secondary"
                size="sm"
                disabled={isExporting || isRestoring}
                onClick={async () => {
                  setIsExporting(true);
                  setOperationStatus(t('backup.preparingBackup'));
                  try {
                    setOperationStatus(t('backup.creatingArchive'));
                    const { blob, filename } = await api.exportBackup();
                    setOperationStatus(t('backup.downloadingFile'));
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = filename;
                    a.click();
                    URL.revokeObjectURL(url);
                    showToast(t('backup.backupDownloaded'));
                  } catch (e) {
                    showToast(t('backup.failedToCreateBackup', { message: e instanceof Error ? e.message : 'Unknown error' }), 'error');
                  } finally {
                    setIsExporting(false);
                    setOperationStatus('');
                  }
                }}
              >
                <Download className="w-4 h-4" />
                {t('backup.download')}
              </Button>
            </div>

            {/* Import */}
            <div className="flex items-center justify-between py-3 border-b border-bambu-dark-tertiary">
              <div>
                <p className="text-white">{t('backup.restoreBackup')}</p>
                <p className="text-sm text-bambu-gray">
                  {t('backup.restoreDescription')}
                </p>
                <p className="text-xs text-bambu-gray-light mt-1">
                  {t('backup.restoreNote')}
                </p>
              </div>
              <input
                ref={fileInputRef}
                type="file"
                accept=".zip"
                className="hidden"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) {
                    setRestoreFile(file);
                    setShowRestoreConfirm(true);
                  }
                  e.target.value = '';
                }}
              />
              <Button
                variant="secondary"
                size="sm"
                disabled={isRestoring || isExporting}
                onClick={() => fileInputRef.current?.click()}
              >
                <Upload className="w-4 h-4" />
                {t('backup.restore')}
              </Button>
            </div>

            {/* Restore result message */}
            {restoreResult && (
              <div className={`p-3 rounded-lg ${restoreResult.success ? 'bg-green-500/10 border border-green-500/30' : 'bg-red-500/10 border border-red-500/30'}`}>
                <div className="flex items-start gap-2 text-sm">
                  {restoreResult.success ? (
                    <CheckCircle className="w-4 h-4 text-green-400 mt-0.5 flex-shrink-0" />
                  ) : (
                    <XCircle className="w-4 h-4 text-red-400 mt-0.5 flex-shrink-0" />
                  )}
                  <div className={restoreResult.success ? 'text-green-200' : 'text-red-200'}>
                    {restoreResult.message}
                    {restoreResult.success && (
                      <div className="mt-2">
                        <Button
                          size="sm"
                          onClick={() => window.location.reload()}
                        >
                          <RotateCcw className="w-3 h-3" />
                          {t('backup.reloadNow')}
                        </Button>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            )}

            {/* Warning */}
            <div className="p-3 rounded-lg bg-yellow-500/10 border border-yellow-500/30">
              <div className="flex items-start gap-2 text-sm">
                <AlertTriangle className="w-4 h-4 text-yellow-400 mt-0.5 flex-shrink-0" />
                <div className="text-yellow-200">
                  <span className="font-medium">{t('backup.restoreReplacesAll')}</span>{' '}
                  <span className="text-yellow-200/70">{t('backup.restoreReplacesAllDetail')}</span>
                </div>
              </div>
            </div>
          </CardContent>
        </Card>

        {/* Scheduled Local Backups (#884) */}
        <Card>
          <CardHeader>
            <div className="flex items-center gap-2">
              <Clock className="w-5 h-5 text-bambu-green" />
              <h2 className="text-lg font-semibold text-white">{t('backup.scheduledLocalBackup.title')}</h2>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <p className="text-sm text-bambu-gray">{t('backup.scheduledLocalBackup.description')}</p>

            {/* Enable toggle */}
            <div className="flex items-center justify-between">
              <span className="text-sm text-white">{t('backup.scheduledLocalBackup.enabled')}</span>
              <Toggle
                checked={localBackupStatus?.enabled ?? false}
                onChange={async (checked) => {
                  try {
                    await api.updateSettings({ local_backup_enabled: checked });
                    refetchLocalStatus();
                    showToast(t('common.saved'), 'success');
                  } catch (e) {
                    showToast(e instanceof Error ? e.message : 'Failed', 'error');
                  }
                }}
              />
            </div>

            {localBackupStatus?.enabled && (
              <>
                {/* Schedule + time + retention + path */}
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <label className="text-sm">
                    <span className="block text-bambu-gray mb-1">{t('backup.scheduledLocalBackup.schedule')}</span>
                    <select
                      value={localBackupStatus?.schedule ?? 'daily'}
                      onChange={async (e) => {
                        try {
                          await api.updateSettings({ local_backup_schedule: e.target.value });
                          refetchLocalStatus();
                        } catch (err) {
                          showToast(err instanceof Error ? err.message : 'Failed', 'error');
                        }
                      }}
                      className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:outline-none focus:border-bambu-green"
                    >
                      <option value="hourly">{t('backup.scheduledLocalBackup.hourly')}</option>
                      <option value="daily">{t('backup.scheduledLocalBackup.daily')}</option>
                      <option value="weekly">{t('backup.scheduledLocalBackup.weekly')}</option>
                    </select>
                  </label>

                  {(localBackupStatus?.schedule ?? 'daily') !== 'hourly' && (
                    <label className="text-sm">
                      <span className="block text-bambu-gray mb-1">{t('backup.scheduledLocalBackup.time')}</span>
                      <input
                        type="time"
                        value={localBackupStatus?.time ?? '03:00'}
                        onChange={async (e) => {
                          try {
                            await api.updateSettings({ local_backup_time: e.target.value });
                            refetchLocalStatus();
                          } catch (err) {
                            showToast(err instanceof Error ? err.message : 'Failed', 'error');
                          }
                        }}
                        className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:outline-none focus:border-bambu-green"
                      />
                    </label>
                  )}

                  <label className="text-sm">
                    <span className="block text-bambu-gray mb-1">{t('backup.scheduledLocalBackup.retention')}</span>
                    <input
                      type="number"
                      min={1}
                      max={100}
                      value={localBackupStatus?.retention ?? 5}
                      onChange={async (e) => {
                        const v = Math.max(1, Math.min(100, parseInt(e.target.value, 10) || 1));
                        try {
                          await api.updateSettings({ local_backup_retention: v });
                          refetchLocalStatus();
                        } catch (err) {
                          showToast(err instanceof Error ? err.message : 'Failed', 'error');
                        }
                      }}
                      className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:outline-none focus:border-bambu-green"
                    />
                  </label>

                  <label className="text-sm sm:col-span-2">
                    <span className="block text-bambu-gray mb-1">{t('backup.scheduledLocalBackup.path')}</span>
                    <input
                      type="text"
                      value={localBackupPath}
                      placeholder={localBackupStatus?.default_path ?? ''}
                      onChange={(e) => setLocalBackupPath(e.target.value)}
                      onBlur={async () => {
                        if (localBackupPath === (localBackupStatus?.path ?? '')) return;
                        try {
                          await api.updateSettings({ local_backup_path: localBackupPath });
                          refetchLocalStatus();
                          refetchLocalBackups();
                        } catch (err) {
                          showToast(err instanceof Error ? err.message : 'Failed', 'error');
                        }
                      }}
                      className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:outline-none focus:border-bambu-green"
                    />
                    <p className="text-xs text-bambu-gray/70 mt-1">{t('backup.scheduledLocalBackup.pathHint')}</p>
                  </label>
                </div>

                {/* Status row */}
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-sm bg-bambu-dark/50 rounded-lg p-3">
                  <div>
                    <div className="text-bambu-gray text-xs">{t('backup.scheduledLocalBackup.lastRun')}</div>
                    <div className="text-white">
                      {formatDateTime(localBackupStatus?.last_backup_at ?? null, timeFormat, dateFormat)}
                      {localBackupStatus?.last_status && (
                        <span className={`ml-2 text-xs ${localBackupStatus.last_status === 'success' ? 'text-status-ok' : 'text-status-error'}`}>
                          ({localBackupStatus.last_status})
                        </span>
                      )}
                    </div>
                  </div>
                  <div>
                    <div className="text-bambu-gray text-xs">{t('backup.scheduledLocalBackup.nextRun')}</div>
                    <div className="text-white">{formatDateTime(localBackupStatus?.next_run ?? null, timeFormat, dateFormat)}</div>
                  </div>
                </div>
              </>
            )}

            {/* Run-now button */}
            <Button
              variant="secondary"
              disabled={localBackupStatus?.is_running}
              onClick={async () => {
                try {
                  const result = await api.triggerLocalBackup();
                  if (result.success) {
                    showToast(t('backup.scheduledLocalBackup.runSuccess', { filename: result.filename ?? '' }), 'success');
                  } else {
                    showToast(result.message, 'error');
                  }
                  refetchLocalStatus();
                  refetchLocalBackups();
                } catch (e) {
                  showToast(e instanceof Error ? e.message : 'Failed', 'error');
                }
              }}
            >
              {localBackupStatus?.is_running ? (
                <Loader2 className="w-4 h-4 animate-spin mr-2" />
              ) : (
                <Play className="w-4 h-4 mr-2" />
              )}
              {t('backup.scheduledLocalBackup.runNow')}
            </Button>

            {/* Backups list */}
            <div>
              <h3 className="text-sm text-bambu-gray mb-2">{t('backup.scheduledLocalBackup.backups')}</h3>
              {!localBackups || localBackups.length === 0 ? (
                <p className="text-bambu-gray/60 text-sm italic">{t('backup.scheduledLocalBackup.noBackups')}</p>
              ) : (
                <div className="space-y-1">
                  {localBackups.map((b) => (
                    <div key={b.filename} className="flex items-center gap-3 p-2 bg-bambu-dark/50 rounded-lg">
                      <Database className="w-4 h-4 text-bambu-gray shrink-0" />
                      <div className="flex-1 min-w-0">
                        <p className="text-sm text-white truncate">{b.filename}</p>
                        <p className="text-xs text-bambu-gray">
                          {(b.size / (1024 * 1024)).toFixed(1)} MB · {formatDateTime(b.created_at, timeFormat, dateFormat)}
                        </p>
                      </div>
                      <a
                        href={api.getLocalBackupDownloadUrl(b.filename)}
                        title={t('backup.scheduledLocalBackup.download')}
                        className="p-1.5 rounded hover:bg-bambu-green/20 text-bambu-green transition-colors"
                      >
                        <Download className="w-4 h-4" />
                      </a>
                      <button
                        type="button"
                        onClick={() => setScheduledRestoreFile(b.filename)}
                        title={t('backup.scheduledLocalBackup.restore')}
                        className="p-1.5 rounded hover:bg-yellow-500/20 text-yellow-400 transition-colors"
                      >
                        <RotateCcw className="w-4 h-4" />
                      </button>
                      <button
                        type="button"
                        onClick={() => setScheduledDeleteFile(b.filename)}
                        title={t('backup.scheduledLocalBackup.delete')}
                        className="p-1.5 rounded hover:bg-red-500/20 text-red-400 transition-colors"
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Scheduled-backup restore confirmation */}
      {scheduledRestoreFile && (
        <ConfirmModal
          title={t('backup.restoreConfirmTitle')}
          message={t('backup.restoreConfirmMessage', { filename: scheduledRestoreFile })}
          confirmText={t('backup.restoreConfirmButton')}
          variant="danger"
          onConfirm={async () => {
            const filename = scheduledRestoreFile;
            setScheduledRestoreFile(null);
            setIsRestoring(true);
            setOperationStatus(t('backup.restoringBackup'));
            try {
              const result = await api.restoreLocalBackup(filename);
              if (result.success !== false) {
                showToast(t('backup.backupRestoredRestart'), 'success');
              } else {
                showToast(result.message ?? t('backup.failedToRestore'), 'error');
              }
            } catch (e) {
              showToast(e instanceof Error ? e.message : t('backup.failedToRestore'), 'error');
            } finally {
              setIsRestoring(false);
              setOperationStatus('');
            }
          }}
          onCancel={() => setScheduledRestoreFile(null)}
        />
      )}

      {/* Scheduled-backup delete confirmation */}
      {scheduledDeleteFile && (
        <ConfirmModal
          title={t('backup.scheduledLocalBackup.deleteConfirmTitle')}
          message={t('backup.scheduledLocalBackup.deleteConfirmMessage', { filename: scheduledDeleteFile })}
          confirmText={t('common.delete')}
          variant="danger"
          onConfirm={async () => {
            const filename = scheduledDeleteFile;
            setScheduledDeleteFile(null);
            try {
              const result = await api.deleteLocalBackup(filename);
              if (result.success) {
                showToast(result.message, 'success');
                refetchLocalBackups();
              } else {
                showToast(result.message, 'error');
              }
            } catch (e) {
              showToast(e instanceof Error ? e.message : 'Failed', 'error');
            }
          }}
          onCancel={() => setScheduledDeleteFile(null)}
        />
      )}

      {/* Restore Confirmation Modal */}
      {showRestoreConfirm && restoreFile && (
        <ConfirmModal
          title={t('backup.restoreConfirmTitle')}
          message={t('backup.restoreConfirmMessage', { filename: restoreFile.name })}
          confirmText={t('backup.restoreConfirmButton')}
          variant="danger"
          onConfirm={async () => {
            setShowRestoreConfirm(false);
            setIsRestoring(true);
            setRestoreResult(null);
            try {
              setOperationStatus(t('backup.uploadingFile'));
              const result = await api.importBackup(restoreFile);
              setRestoreResult(result);
              if (result.success) {
                showToast(t('backup.backupRestoredRestart'), 'success');
              } else {
                showToast(result.message, 'error');
              }
            } catch (e) {
              const message = e instanceof Error ? e.message : t('backup.failedToRestore');
              setRestoreResult({ success: false, message });
              showToast(message, 'error');
            } finally {
              setIsRestoring(false);
              setOperationStatus('');
              setRestoreFile(null);
            }
          }}
          onCancel={() => {
            setShowRestoreConfirm(false);
            setRestoreFile(null);
          }}
        />
      )}

      {/* Blocking overlay during backup/restore operations */}
      {(isExporting || isRestoring) && (
        <div className="fixed inset-0 bg-black/80 flex items-center justify-center z-[100]">
          <div className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl p-8 max-w-md w-full mx-4 text-center">
            <div className="flex justify-center mb-4">
              <div className="relative">
                <div className="w-16 h-16 border-4 border-bambu-dark-tertiary rounded-full"></div>
                <div className="w-16 h-16 border-4 border-bambu-green border-t-transparent rounded-full absolute inset-0 animate-spin"></div>
              </div>
            </div>
            <h3 className="text-xl font-semibold text-white mb-2">
              {isExporting ? t('backup.creatingBackup') : t('backup.restoringBackup')}
            </h3>
            <p className="text-bambu-gray mb-4">
              {operationStatus || (isExporting ? t('backup.preparing') : t('backup.processing'))}
            </p>
            <div className="p-3 rounded-lg bg-yellow-500/10 border border-yellow-500/30">
              <div className="flex items-start gap-2 text-sm">
                <AlertTriangle className="w-4 h-4 text-yellow-400 mt-0.5 flex-shrink-0" />
                <p className="text-yellow-200 text-left">
                  {t('backup.doNotClosePage')}
                </p>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
