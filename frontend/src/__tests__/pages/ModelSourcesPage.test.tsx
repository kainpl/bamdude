/**
 * Model sources: one page, a tab per source the user may view. Manyfold shows once it is
 * connected — or to someone who can connect it.
 */

import type { ReactNode } from 'react';
import { describe, it, expect, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { delay, http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { render as renderBare } from '../utils';
import { ManyfoldImportProvider } from '../../contexts/ManyfoldImportContext';
import { ModelSourcesPage } from '../../pages/ModelSourcesPage';

const render = (ui: ReactNode) => renderBare(<ManyfoldImportProvider>{ui}</ManyfoldImportProvider>);

let requests = { manyfoldStatus: 0, makerworldStatus: 0 };

function manyfold(usable: boolean, options: { statusDelay?: number; statusFails?: boolean } = {}) {
  server.use(
    http.get('/api/v1/model-sources/manyfold/status', async () => {
      requests.manyfoldStatus += 1;
      if (options.statusDelay) await delay(options.statusDelay);
      if (options.statusFails) return HttpResponse.json({ detail: 'Internal Server Error' }, { status: 500 });
      return HttpResponse.json({
        source: 'manyfold',
        name: 'Manyfold',
        configured: usable,
        usable,
        base_url: null,
        imports: { active: 0, waiting: 0, limit: 2 },
      });
    }),
    http.get('/api/v1/model-sources/manyfold/models', () =>
      HttpResponse.json({ total: 0, page: 1, pages: 1, has_next: false, has_previous: false, models: [] }),
    ),
    http.get('/api/v1/makerworld/status', () => {
      requests.makerworldStatus += 1;
      return HttpResponse.json({ has_cloud_token: true, can_download: true, sign_in_expired: false });
    }),
    http.get('/api/v1/library/folders', () => HttpResponse.json([])),
  );
}

/** Not the harness's administrator: a user with exactly these permissions. */
function asUser(permissions: string[]) {
  server.use(
    http.get('/api/v1/auth/me', () =>
      HttpResponse.json({
        id: 2,
        username: 'operator',
        role: 'user',
        is_active: true,
        is_admin: false,
        groups: [],
        permissions,
        created_at: '2024-01-01T00:00:00Z',
      }),
    ),
  );
}

beforeEach(() => {
  requests = { manyfoldStatus: 0, makerworldStatus: 0 };
  window.history.replaceState({}, '', '/model-sources');
});

describe('ModelSourcesPage', () => {
  it('offers a tab for each source and opens MakerWorld first', async () => {
    manyfold(true);
    render(<ModelSourcesPage />);

    expect(await screen.findByRole('tab', { name: 'Manyfold' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'MakerWorld' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('heading', { level: 1, name: 'Model sources' })).toBeInTheDocument();
    // The embedded MakerWorld tab brings no second page title.
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(screen.getByText('Import from MakerWorld')).toBeInTheDocument();
  });

  it('opens the tab an address names', async () => {
    manyfold(true);
    window.history.replaceState({}, '', '/model-sources?tab=manyfold');
    render(<ModelSourcesPage />);

    await waitFor(() => expect(screen.getByRole('tab', { name: 'Manyfold' })).toHaveAttribute('aria-selected', 'true'));
    expect(await screen.findByText('No models found.')).toBeInTheDocument();
  });

  it('is a complete tab set: named list, panel tied to its tab, arrow keys, one tab stop', async () => {
    manyfold(true);
    render(<ModelSourcesPage />);
    const manyfoldTab = await screen.findByRole('tab', { name: 'Manyfold' });
    const makerworldTab = screen.getByRole('tab', { name: 'MakerWorld' });

    expect(screen.getByRole('tablist', { name: 'Model sources' })).toBeInTheDocument();
    const panel = screen.getByRole('tabpanel');
    expect(panel).toHaveAttribute('aria-labelledby', makerworldTab.id);
    expect(makerworldTab).toHaveAttribute('aria-controls', panel.id);
    // Roving tabindex: Tab reaches the selected tab only; the arrows move between tabs.
    expect(makerworldTab).toHaveAttribute('tabindex', '0');
    expect(manyfoldTab).toHaveAttribute('tabindex', '-1');

    makerworldTab.focus();
    fireEvent.keyDown(makerworldTab, { key: 'ArrowRight' });
    await waitFor(() => expect(manyfoldTab).toHaveAttribute('aria-selected', 'true'));
    expect(manyfoldTab).toHaveFocus();
    expect(manyfoldTab).toHaveAttribute('tabindex', '0');
    expect(screen.getByRole('tabpanel')).toHaveAttribute('aria-labelledby', manyfoldTab.id);
    expect(new URLSearchParams(window.location.search).get('tab')).toBe('manyfold');

    fireEvent.keyDown(manyfoldTab, { key: 'ArrowRight' }); // wraps round
    await waitFor(() => expect(makerworldTab).toHaveAttribute('aria-selected', 'true'));
    fireEvent.keyDown(makerworldTab, { key: 'End' });
    await waitFor(() => expect(manyfoldTab).toHaveAttribute('aria-selected', 'true'));
    fireEvent.keyDown(manyfoldTab, { key: 'Home' });
    await waitFor(() => expect(makerworldTab).toHaveAttribute('aria-selected', 'true'));
  });

  it('switching tabs does not add history steps', async () => {
    manyfold(true);
    render(<ModelSourcesPage />);
    const before = window.history.length;
    fireEvent.click(await screen.findByRole('tab', { name: 'Manyfold' }));
    await screen.findByText('No models found.');
    fireEvent.click(screen.getByRole('tab', { name: 'MakerWorld' }));
    await screen.findByText('Import from MakerWorld');

    expect(window.history.length).toBe(before);
  });

  it('a MakerWorld-only user gets MakerWorld alone, and Manyfold is not asked about', async () => {
    manyfold(true);
    asUser(['makerworld:view']);
    render(<ModelSourcesPage />);

    expect(await screen.findByText('Import from MakerWorld')).toBeInTheDocument();
    expect(screen.queryByRole('tab')).not.toBeInTheDocument(); // one source: no tab strip
    expect(requests.manyfoldStatus).toBe(0);
  });

  it('a Manyfold-only user gets the catalogue alone', async () => {
    manyfold(true);
    asUser(['manyfold:view']);
    render(<ModelSourcesPage />);

    expect(await screen.findByText('No models found.')).toBeInTheDocument();
    expect(screen.queryByRole('tab')).not.toBeInTheDocument();
    expect(screen.queryByText('Import from MakerWorld')).not.toBeInTheDocument();
    expect(requests.makerworldStatus).toBe(0);
  });

  it('a Manyfold-only user is told when it is not connected, without a way into Settings', async () => {
    manyfold(false);
    asUser(['manyfold:view']);
    render(<ModelSourcesPage />);

    expect(await screen.findByText('Manyfold is not connected')).toBeInTheDocument();
    expect(screen.getByText('Ask an administrator to connect your Manyfold library.')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /Connection settings/ })).not.toBeInTheDocument();
  });

  it('hides an unconnected Manyfold from a user who cannot connect it, also when the address asks for it', async () => {
    manyfold(false);
    asUser(['makerworld:view', 'manyfold:view']);
    window.history.replaceState({}, '', '/model-sources?tab=manyfold');
    render(<ModelSourcesPage />);

    expect(await screen.findByText('Import from MakerWorld')).toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Manyfold' })).not.toBeInTheDocument();
    expect(screen.queryByText('Manyfold is not connected')).not.toBeInTheDocument();
  });

  it('shows no source to a user with neither right', async () => {
    manyfold(true);
    asUser(['printers:read']);
    render(<ModelSourcesPage />);

    await screen.findByRole('heading', { level: 1, name: 'Model sources' });
    await waitFor(() => expect(screen.queryByText('Import from MakerWorld')).not.toBeInTheDocument());
    expect(screen.queryByRole('tab')).not.toBeInTheDocument();
    expect(screen.queryByText('No models found.')).not.toBeInTheDocument();
    expect(requests.manyfoldStatus).toBe(0);
  });

  it('keeps the Manyfold tab when its status could not be read', async () => {
    manyfold(true, { statusFails: true });
    asUser(['makerworld:view', 'manyfold:view']);
    render(<ModelSourcesPage />);

    // "Could not be checked" is not "not connected": the tab does not silently disappear.
    fireEvent.click(await screen.findByRole('tab', { name: 'Manyfold' }));
    // (The tab asks once more on mount, and a 500 is slow to give up: allow for both.)
    expect(
      await screen.findByText('BamDude could not check the Manyfold connection.', {}, { timeout: 5000 }),
    ).toBeInTheDocument();
  });

  it('waits for the Manyfold status before deciding what an address that names Manyfold opens', async () => {
    manyfold(true, { statusDelay: 150 });
    asUser(['makerworld:view', 'manyfold:view']);
    window.history.replaceState({}, '', '/model-sources?tab=manyfold');
    render(<ModelSourcesPage />);

    expect(await screen.findByText('No models found.')).toBeInTheDocument();
    // MakerWorld was never mounted in the meantime: none of its requests were sent.
    expect(requests.makerworldStatus).toBe(0);
  });

  /** Set up, and the status says it cannot be used; the catalogue refuses with this code. */
  function unusable(code: string, status: number) {
    manyfold(true);
    server.use(
      http.get('/api/v1/model-sources/manyfold/status', () =>
        HttpResponse.json({
          source: 'manyfold',
          name: 'Manyfold',
          configured: true,
          usable: false,
          base_url: null,
          imports: { active: 0, waiting: 0, limit: 2 },
        }),
      ),
      http.get('/api/v1/model-sources/manyfold/models', () =>
        HttpResponse.json({ detail: { error: code, message: 'refused' } }, { status }),
      ),
    );
  }

  it('keeps the Manyfold tab, and says why, when the safety check of its connection failed', async () => {
    unusable('manyfold_transport_unavailable', 503);
    asUser(['makerworld:view', 'manyfold:view']);
    render(<ModelSourcesPage />);

    fireEvent.click(await screen.findByRole('tab', { name: 'Manyfold' }));
    expect(
      await screen.findByText('The safety check of the Manyfold connection failed. Manyfold is switched off until BamDude restarts.'),
    ).toBeInTheDocument();
  });

  it('hides a Manyfold that is switched off from a user who cannot connect it', async () => {
    unusable('manyfold_disabled', 400);
    asUser(['makerworld:view', 'manyfold:view']);
    render(<ModelSourcesPage />);

    expect(await screen.findByText('Import from MakerWorld')).toBeInTheDocument();
    await waitFor(() => expect(requests.makerworldStatus).toBeGreaterThan(0));
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(screen.queryByRole('tab', { name: 'Manyfold' })).not.toBeInTheDocument();
  });
});
