/**
 * Model sources: one page, a tab per source the user may view. Manyfold shows once it is
 * connected — or to someone who can connect it.
 */

import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { render } from '../utils';
import { ModelSourcesPage } from '../../pages/ModelSourcesPage';

function manyfold(usable: boolean) {
  server.use(
    http.get('/api/v1/model-sources/manyfold/status', () =>
      HttpResponse.json({ source: 'manyfold', name: 'Manyfold', configured: usable, usable, base_url: null }),
    ),
    http.get('/api/v1/model-sources/manyfold/models', () =>
      HttpResponse.json({ total: 0, page: 1, pages: 1, has_next: false, has_previous: false, models: [] }),
    ),
    http.get('/api/v1/makerworld/status', () =>
      HttpResponse.json({ has_cloud_token: true, can_download: true, sign_in_expired: false }),
    ),
    http.get('/api/v1/library/folders', () => HttpResponse.json([])),
  );
}

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
    window.history.pushState({}, '', '/model-sources?tab=manyfold');
    render(<ModelSourcesPage />);

    await waitFor(() => expect(screen.getByRole('tab', { name: 'Manyfold' })).toHaveAttribute('aria-selected', 'true'));
    expect(await screen.findByText('No models found.')).toBeInTheDocument();
    window.history.pushState({}, '', '/');
  });

  it('an old MakerWorld link lands on the MakerWorld tab', async () => {
    manyfold(true);
    window.history.pushState({}, '', '/model-sources?tab=makerworld');
    render(<ModelSourcesPage />);

    expect(await screen.findByRole('tab', { name: 'MakerWorld' })).toHaveAttribute('aria-selected', 'true');
    window.history.pushState({}, '', '/');
  });
});
