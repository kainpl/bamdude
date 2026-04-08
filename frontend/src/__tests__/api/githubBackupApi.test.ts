/**
 * Tests for the Git Backup API client functions.
 */

import { describe, it, expect } from 'vitest';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import type {
  GitBackupConfig,
  GitBackupStatus,
  GitBackupLog,
} from '../../api/client';

// Mock API base URL
const API_BASE = 'http://localhost:5000/api/v1';

// Create MSW server
const server = setupServer();

beforeAll(() => server.listen({ onUnhandledRequest: 'bypass' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('Git Backup API Types', () => {
  it('GitBackupConfig has correct shape', () => {
    const config: GitBackupConfig = {
      id: 1,
      repository_url: 'https://github.com/test/repo',
      has_token: true,
      branch: 'main',
      schedule_enabled: true,
      schedule_type: 'daily',
      backup_kprofiles: true,
      backup_cloud_profiles: true,
      backup_settings: false,
      enabled: true,
      provider: 'github',
      api_base_url: null,
      last_backup_at: '2026-01-27T10:00:00Z',
      last_backup_status: 'success',
      last_backup_message: null,
      last_backup_commit_sha: 'abc123',
      next_scheduled_run: '2026-01-28T00:00:00Z',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-27T10:00:00Z',
    };

    expect(config.id).toBe(1);
    expect(config.has_token).toBe(true);
    expect(config.schedule_type).toBe('daily');
    expect(config.provider).toBe('github');
    expect(config.api_base_url).toBeNull();
  });

  it('GitBackupConfig supports gitlab provider', () => {
    const config: GitBackupConfig = {
      id: 2,
      repository_url: 'https://gitlab.com/group/project',
      has_token: true,
      branch: 'main',
      schedule_enabled: false,
      schedule_type: 'daily',
      backup_kprofiles: true,
      backup_cloud_profiles: false,
      backup_settings: true,
      enabled: true,
      provider: 'gitlab',
      api_base_url: 'https://gitlab.example.com',
      last_backup_at: null,
      last_backup_status: null,
      last_backup_message: null,
      last_backup_commit_sha: null,
      next_scheduled_run: null,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    };

    expect(config.provider).toBe('gitlab');
    expect(config.api_base_url).toBe('https://gitlab.example.com');
  });

  it('GitBackupStatus has correct shape', () => {
    const status: GitBackupStatus = {
      configured: true,
      enabled: true,
      is_running: false,
      progress: null,
      last_backup_at: '2026-01-27T10:00:00Z',
      last_backup_status: 'success',
      next_scheduled_run: '2026-01-28T00:00:00Z',
    };

    expect(status.configured).toBe(true);
    expect(status.is_running).toBe(false);
  });

  it('GitBackupStatus can have progress', () => {
    const status: GitBackupStatus = {
      configured: true,
      enabled: true,
      is_running: true,
      progress: 'Pushing to remote...',
      last_backup_at: null,
      last_backup_status: null,
      next_scheduled_run: null,
    };

    expect(status.is_running).toBe(true);
    expect(status.progress).toBe('Pushing to remote...');
  });

  it('GitBackupLog has correct shape', () => {
    const log: GitBackupLog = {
      id: 1,
      config_id: 1,
      started_at: '2026-01-27T10:00:00Z',
      completed_at: '2026-01-27T10:01:00Z',
      status: 'success',
      trigger: 'manual',
      commit_sha: 'abc123',
      files_changed: 5,
      error_message: null,
    };

    expect(log.status).toBe('success');
    expect(log.trigger).toBe('manual');
    expect(log.files_changed).toBe(5);
  });

  it('GitBackupLog can have error', () => {
    const log: GitBackupLog = {
      id: 2,
      config_id: 1,
      started_at: '2026-01-27T10:00:00Z',
      completed_at: '2026-01-27T10:00:30Z',
      status: 'failed',
      trigger: 'scheduled',
      commit_sha: null,
      files_changed: 0,
      error_message: 'Authentication failed',
    };

    expect(log.status).toBe('failed');
    expect(log.error_message).toBe('Authentication failed');
    expect(log.commit_sha).toBeNull();
  });
});

describe('Git Backup API Endpoints', () => {
  it('GET /git-backup/config returns null when not configured', async () => {
    server.use(
      http.get(`${API_BASE}/git-backup/config`, () => {
        return HttpResponse.json(null);
      })
    );

    const response = await fetch(`${API_BASE}/git-backup/config`);
    const data = await response.json();
    expect(data).toBeNull();
  });

  it('GET /git-backup/config returns config when exists', async () => {
    const mockConfig: GitBackupConfig = {
      id: 1,
      repository_url: 'https://github.com/test/repo',
      has_token: true,
      branch: 'main',
      schedule_enabled: false,
      schedule_type: 'daily',
      backup_kprofiles: true,
      backup_cloud_profiles: true,
      backup_settings: false,
      enabled: true,
      provider: 'github',
      api_base_url: null,
      last_backup_at: null,
      last_backup_status: null,
      last_backup_message: null,
      last_backup_commit_sha: null,
      next_scheduled_run: null,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    };

    server.use(
      http.get(`${API_BASE}/git-backup/config`, () => {
        return HttpResponse.json(mockConfig);
      })
    );

    const response = await fetch(`${API_BASE}/git-backup/config`);
    const data = await response.json();
    expect(data.repository_url).toBe('https://github.com/test/repo');
    expect(data.has_token).toBe(true);
  });

  it('GET /git-backup/status returns not configured status', async () => {
    const mockStatus: GitBackupStatus = {
      configured: false,
      enabled: false,
      is_running: false,
      progress: null,
      last_backup_at: null,
      last_backup_status: null,
      next_scheduled_run: null,
    };

    server.use(
      http.get(`${API_BASE}/git-backup/status`, () => {
        return HttpResponse.json(mockStatus);
      })
    );

    const response = await fetch(`${API_BASE}/git-backup/status`);
    const data = await response.json();
    expect(data.configured).toBe(false);
    expect(data.enabled).toBe(false);
  });

  it('GET /git-backup/logs returns empty list when no logs', async () => {
    server.use(
      http.get(`${API_BASE}/git-backup/logs`, () => {
        return HttpResponse.json([]);
      })
    );

    const response = await fetch(`${API_BASE}/git-backup/logs`);
    const data = await response.json();
    expect(data).toEqual([]);
  });

  it('GET /git-backup/logs returns log entries', async () => {
    const mockLogs: GitBackupLog[] = [
      {
        id: 1,
        config_id: 1,
        started_at: '2026-01-27T10:00:00Z',
        completed_at: '2026-01-27T10:01:00Z',
        status: 'success',
        trigger: 'manual',
        commit_sha: 'abc123',
        files_changed: 5,
        error_message: null,
      },
    ];

    server.use(
      http.get(`${API_BASE}/git-backup/logs`, () => {
        return HttpResponse.json(mockLogs);
      })
    );

    const response = await fetch(`${API_BASE}/git-backup/logs`);
    const data = await response.json();
    expect(data.length).toBe(1);
    expect(data[0].status).toBe('success');
  });

  it('POST /git-backup/run returns 404 when not configured', async () => {
    server.use(
      http.post(`${API_BASE}/git-backup/run`, () => {
        return HttpResponse.json(
          { detail: 'No configuration found' },
          { status: 404 }
        );
      })
    );

    const response = await fetch(`${API_BASE}/git-backup/run`, {
      method: 'POST',
    });
    expect(response.status).toBe(404);
  });

  it('POST /git-backup/test returns success on valid credentials', async () => {
    server.use(
      http.post(`${API_BASE}/git-backup/test`, () => {
        return HttpResponse.json({
          success: true,
          message: 'Connection successful',
          repo_name: 'test/repo',
          default_branch: 'main',
        });
      })
    );

    const response = await fetch(
      `${API_BASE}/git-backup/test?repo_url=https://github.com/test/repo&token=ghp_test&provider=github`,
      { method: 'POST' }
    );
    const data = await response.json();
    expect(data.success).toBe(true);
    expect(data.repo_name).toBe('test/repo');
  });

  it('POST /git-backup/test returns failure on invalid credentials', async () => {
    server.use(
      http.post(`${API_BASE}/git-backup/test`, () => {
        return HttpResponse.json({
          success: false,
          message: 'Authentication failed',
          repo_name: null,
          default_branch: null,
        });
      })
    );

    const response = await fetch(
      `${API_BASE}/git-backup/test?repo_url=https://github.com/test/repo&token=invalid&provider=github`,
      { method: 'POST' }
    );
    const data = await response.json();
    expect(data.success).toBe(false);
    expect(data.message).toBe('Authentication failed');
  });
});
