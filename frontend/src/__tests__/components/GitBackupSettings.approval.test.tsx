import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { render } from '../utils';
import { api, type GitBackupConfig, type GitBackupStatus, type GitBackupLog } from '../../api/client';
import { GitBackupSettings } from '../../components/GitBackupSettings';
import i18n from '../../i18n';

const auth = vi.hoisted(() => ({ isAdmin: true, gitBackup: true }));
vi.mock('../../contexts/AuthContext', () => ({
  AuthProvider: ({ children }: { children: React.ReactNode }) => children,
  useAuth: () => ({ isAdmin: auth.isAdmin, hasPermission: (permission: string) => permission !== 'git:backup' || auth.gitBackup }),
}));

const initialConfig: GitBackupConfig = {
  id: 1, repository_url: 'https://github.com/farm/private', has_token: true, provider: 'github',
  api_base_url: null, branch: 'main', enabled: true, schedule_enabled: true, schedule_type: 'daily',
  backup_kprofiles: false, backup_cloud_profiles: false, backup_settings: true, backup_spools: false,
  backup_archives: false, last_backup_at: null, last_backup_status: null, last_backup_message: null,
  last_backup_commit_sha: null, next_scheduled_run: '2026-11-14T10:00:00Z', created_at: '2026-11-01T10:00:00Z',
  updated_at: '2026-11-13T10:00:00Z', approval_required: true, blocked_reason: 'approval_required',
  last_successful_backup_at: null, last_backup_reason_code: null,
};
let config: GitBackupConfig;
let status: GitBackupStatus;
let logs: GitBackupLog[];

beforeEach(async () => {
  auth.isAdmin = true;
  auth.gitBackup = true;
  await i18n.changeLanguage('en');
  config = { ...initialConfig };
  status = { configured: true, enabled: true, is_running: false, restore_running: false, progress: null,
    last_backup_at: null, last_backup_status: null, next_scheduled_run: config.next_scheduled_run,
    approval_required: true, blocked_reason: 'approval_required', last_successful_backup_at: null,
    last_backup_reason_code: null };
  logs = [];
  vi.spyOn(api, 'getGitBackupConfig').mockImplementation(async () => ({ ...config }));
  vi.spyOn(api, 'getGitBackupStatus').mockImplementation(async () => ({ ...status }));
  vi.spyOn(api, 'getGitBackupLogs').mockImplementation(async () => logs);
  vi.spyOn(api, 'getUiPreferences').mockResolvedValue({ time_format: '24h', date_format: 'iso' } as Awaited<ReturnType<typeof api.getUiPreferences>>);
  vi.spyOn(api, 'getLocalBackupStatus').mockResolvedValue({ enabled: false } as Awaited<ReturnType<typeof api.getLocalBackupStatus>>);
  vi.spyOn(api, 'listLocalBackups').mockResolvedValue([]);
  vi.spyOn(api, 'getCloudStatus').mockResolvedValue({ is_authenticated: false } as Awaited<ReturnType<typeof api.getCloudStatus>>);
  vi.spyOn(api, 'getPrinters').mockResolvedValue([]);
});
afterEach(async () => { cleanup(); vi.restoreAllMocks(); await i18n.changeLanguage('en'); });

describe('Git backup approval visibility', () => {
  it('shows legacy approval before the first run without changing the schedule', async () => {
    const save = vi.spyOn(api, 'updateGitBackupConfig');
    render(<GitBackupSettings />);
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(/has not been approved/)).toBeInTheDocument();
    expect(within(alert).getByRole('link', { name: 'Review backup settings' })).toHaveAttribute('href', '/settings?tab=backup');
    expect(screen.getByText('No attempts yet')).toBeInTheDocument();
    expect(screen.getByText(/Last successful backup: No successful backups yet/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save and approve Git backup' })).toBeEnabled();
    expect(save).not.toHaveBeenCalled();
  });

  it('shows blocked reason, separate attempt/success dates and per-slot history', async () => {
    status = { ...status, blocked_reason: 'approver_not_authorized', last_backup_status: 'blocked',
      last_backup_at: '2026-11-13T10:00:00Z', last_successful_backup_at: '2026-11-12T09:00:00Z', last_backup_reason_code: 'approver_not_authorized' };
    logs = [{ id: 1, config_id: 1, started_at: '2026-11-13T10:00:00Z', completed_at: '2026-11-13T10:00:01Z',
      status: 'blocked', reason_code: 'approver_not_authorized', scheduled_for: '2026-11-13T10:00:00Z',
      trigger: 'scheduled', commit_sha: null, files_changed: 0, error_message: null }];
    render(<GitBackupSettings />);
    expect(within(await screen.findByRole('alert')).getByText(/no longer has access/)).toBeInTheDocument();
    expect(screen.getByText(/Last attempt:.*2026-11-13/)).toBeInTheDocument();
    expect(screen.getByText(/Last successful backup:.*2026-11-12/)).toBeInTheDocument();
    expect(screen.getAllByText('Blocked')).toHaveLength(2);
    expect(screen.getByText(/Scheduled for:.*2026-11-13/)).toBeInTheDocument();
  });

  it('explicitly re-saves the reviewed connection with keep and clears only the current warning', async () => {
    status = { ...status, last_backup_status: 'blocked', last_backup_at: '2026-11-13T10:00:00Z' };
    const save = vi.spyOn(api, 'updateGitBackupConfig').mockImplementation(async () => {
      config = { ...config, approval_required: false, blocked_reason: null };
      status = { ...status, approval_required: false, blocked_reason: null };
      return { ...config };
    });
    render(<GitBackupSettings />);
    await screen.findByDisplayValue(config.repository_url);
    fireEvent.click(await screen.findByRole('button', { name: 'Save and approve Git backup' }));
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    expect(save).toHaveBeenCalledWith({ provider: 'github', repository_url: config.repository_url,
      api_base_url: null, secret_actions: { access_token: 'keep' } });
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
    expect(screen.getByText('Blocked')).toBeInTheDocument();
  });

  it('uses current admission state after same-account regrant despite historical blocked config', async () => {
    status = { ...status, approval_required: false, blocked_reason: null, last_backup_status: 'blocked', last_backup_at: '2026-11-13T10:00:00Z' };
    render(<GitBackupSettings />);
    expect(await screen.findByText('Blocked')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save and approve Git backup' })).not.toBeInTheDocument();
  });

  it('does not turn a benign skipped attempt into an approval warning', async () => {
    status = { ...status, approval_required: false, blocked_reason: null, last_backup_status: 'skipped', last_backup_at: '2026-11-13T10:00:00Z' };
    config = { ...config, approval_required: false, blocked_reason: null };
    render(<GitBackupSettings />);
    expect(await screen.findByText('Skipped')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save and approve Git backup' })).not.toBeInTheDocument();
  });

  it('keeps protected configuration and approval action unavailable to non-admin roles', () => {
    auth.isAdmin = false;
    const { container } = render(<GitBackupSettings />);
    expect(container).toBeEmptyDOMElement();
    expect(api.getGitBackupConfig).not.toHaveBeenCalled();
    expect(api.getGitBackupStatus).not.toHaveBeenCalled();
  });

  it('requires the Git backup permission for the approval action', async () => {
    auth.gitBackup = false;
    render(<GitBackupSettings />);
    await screen.findByRole('alert');
    expect(screen.queryByRole('button', { name: 'Save and approve Git backup' })).not.toBeInTheDocument();
  });

  it('renders the approval state and action in Ukrainian', async () => {
    await i18n.changeLanguage('uk');
    render(<GitBackupSettings />);
    expect(within(await screen.findByRole('alert')).getByText(/Адміністратор ще не погодив/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Зберегти й погодити копіювання Git' })).toBeInTheDocument();
    expect(screen.getByText(/Остання успішна копія: Успішних копій ще не було/)).toBeInTheDocument();
  });
});
