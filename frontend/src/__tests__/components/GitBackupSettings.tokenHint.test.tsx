/**
 * The token hint under the field names the SELECTED provider's scopes (#2775).
 *
 * Forgejo shared Gitea's string, which is how a Forgejo user was told to mint a
 * token by a name their instance does not use — and never told the thing that
 * matters here: a token limited to this one repository is enough. That is the
 * token Forgejo v15 recommends, and it is exactly the token the connection test
 * used to reject.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { http, HttpResponse } from 'msw';

import { render } from '../utils';
import { server } from '../mocks/server';
import { GitBackupSettings } from '../../components/GitBackupSettings';

describe('GitBackupSettings - token hint', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    server.use(
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
        }),
      ),
      http.get('/api/v1/git-backup/logs', () => HttpResponse.json([])),
      http.get('/api/v1/local-backup/status', () => HttpResponse.json({ enabled: false })),
      http.get('/api/v1/local-backup/backups', () => HttpResponse.json([])),
      http.get('/api/v1/cloud/status', () => HttpResponse.json({ is_authenticated: false })),
      http.get('/api/v1/printers', () => HttpResponse.json([])),
    );
  });

  const pick = (value: string) =>
    fireEvent.click(document.querySelector(`input[name="provider"][value="${value}"]`)!);

  it('follows the provider the operator picked', async () => {
    render(<GitBackupSettings />);

    // GitHub is the default, and the shared hint was always GitHub's advice.
    await waitFor(() => expect(screen.getByText(/Contents read\/write/i)).toBeInTheDocument());

    pick('gitlab');
    await waitFor(() => expect(screen.getByText(/write_repository/i)).toBeInTheDocument());
    expect(screen.queryByText(/Contents read\/write/i)).not.toBeInTheDocument();
  });

  it('gives Forgejo its own advice, not Gitea\u2019s', async () => {
    render(<GitBackupSettings />);
    await waitFor(() => expect(screen.getByText(/Contents read\/write/i)).toBeInTheDocument());

    pick('gitea');
    await waitFor(() => expect(screen.getByText(/write:repository/i)).toBeInTheDocument());
    const gitea = screen.getByText(/write:repository/i).textContent;

    pick('forgejo');
    await waitFor(() =>
      // ⚠️ The sentence that only Forgejo gets, and the point of the whole fix.
      expect(screen.getByText(/limited to this one repository/i)).toBeInTheDocument(),
    );
    expect(screen.getByText(/write:repository/i).textContent).not.toBe(gitea);
  });
});
