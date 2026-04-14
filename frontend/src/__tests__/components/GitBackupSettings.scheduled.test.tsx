/**
 * Tests for the Scheduled Local Backup UI in GitBackupSettings (upstream #884).
 *
 * Smoke-level checks: the new Scheduled Local Backups card renders with its
 * controls when enabled, hides them when disabled, and the time picker drops
 * out for the hourly schedule. Backup files render with size + filename.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { GitBackupSettings } from '../../components/GitBackupSettings';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const mockLocalBackupStatus = {
  enabled: true,
  schedule: 'daily',
  time: '03:00',
  retention: 5,
  path: '',
  default_path: '/data/backups',
  is_running: false,
  last_backup_at: null,
  last_status: null,
  last_message: null,
  next_run: '2026-04-13T03:00:00+00:00',
};

const mockLocalBackups = [
  {
    filename: 'bamdude-backup-20260412-120000.zip',
    size: 52428800,
    created_at: '2026-04-12T12:00:00+00:00',
  },
];

describe('GitBackupSettings - Scheduled Local Backups', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    server.use(
      http.get('/api/v1/local-backup/status', () => HttpResponse.json(mockLocalBackupStatus)),
      http.get('/api/v1/local-backup/backups', () => HttpResponse.json(mockLocalBackups)),
      http.get('/api/v1/git-backup/config', () => HttpResponse.json(null)),
      http.get('/api/v1/git-backup/status', () =>
        HttpResponse.json({
          configured: false,
          enabled: false,
          is_running: false,
          progress: null,
          last_backup_at: null,
          last_backup_status: null,
          next_scheduled_run: null,
        })
      ),
      http.get('/api/v1/git-backup/logs', () => HttpResponse.json([])),
      http.get('/api/v1/cloud/status', () => HttpResponse.json({ is_authenticated: false })),
      http.get('/api/v1/printers', () => HttpResponse.json([])),
      http.put('/api/v1/settings/', () => HttpResponse.json({})),
      http.put('/api/v1/settings', () => HttpResponse.json({})),
    );
  });

  it('renders Scheduled Backups card title', async () => {
    render(<GitBackupSettings />);
    await waitFor(() => {
      expect(screen.getByText('Scheduled Local Backups')).toBeInTheDocument();
    });
  });

  it('shows frequency dropdown when enabled', async () => {
    render(<GitBackupSettings />);
    await waitFor(() => {
      expect(screen.getByText('Frequency')).toBeInTheDocument();
    });
  });

  it('shows retention input when enabled', async () => {
    render(<GitBackupSettings />);
    await waitFor(() => {
      expect(screen.getByText('Keep last N backups')).toBeInTheDocument();
    });
  });

  it('shows backup file list', async () => {
    render(<GitBackupSettings />);
    await waitFor(() => {
      expect(screen.getByText('bamdude-backup-20260412-120000.zip')).toBeInTheDocument();
    });
  });

  it('shows file size in MB', async () => {
    render(<GitBackupSettings />);
    await waitFor(() => {
      expect(screen.getByText(/50\.0 MB/)).toBeInTheDocument();
    });
  });

  it('shows Run Now button', async () => {
    render(<GitBackupSettings />);
    await waitFor(() => {
      expect(screen.getByText('Run backup now')).toBeInTheDocument();
    });
  });

  it('hides schedule controls when disabled', async () => {
    server.use(
      http.get('/api/v1/local-backup/status', () =>
        HttpResponse.json({ ...mockLocalBackupStatus, enabled: false })
      ),
    );
    render(<GitBackupSettings />);
    await waitFor(() => {
      expect(screen.getByText('Scheduled Local Backups')).toBeInTheDocument();
    });
    expect(screen.queryByText('Frequency')).not.toBeInTheDocument();
    expect(screen.queryByText('Keep last N backups')).not.toBeInTheDocument();
    // Run-now button stays visible regardless of toggle.
    expect(screen.getByText('Run backup now')).toBeInTheDocument();
  });

  it('hides time picker when hourly is selected', async () => {
    server.use(
      http.get('/api/v1/local-backup/status', () =>
        HttpResponse.json({ ...mockLocalBackupStatus, schedule: 'hourly' })
      ),
    );
    render(<GitBackupSettings />);
    await waitFor(() => {
      expect(screen.getByText('Frequency')).toBeInTheDocument();
    });
    expect(screen.queryByText('Time of day')).not.toBeInTheDocument();
  });

  it('shows legacy bambuddy-backup-* file in the list', async () => {
    server.use(
      http.get('/api/v1/local-backup/backups', () =>
        HttpResponse.json([
          {
            filename: 'bambuddy-backup-20260101-000000.zip',
            size: 1024 * 1024,
            created_at: '2026-01-01T00:00:00+00:00',
          },
        ])
      ),
    );
    render(<GitBackupSettings />);
    await waitFor(() => {
      expect(screen.getByText('bambuddy-backup-20260101-000000.zip')).toBeInTheDocument();
    });
  });
});
