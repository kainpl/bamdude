/**
 * The MakerWorld page tells an expired Bambu Cloud sign-in from a missing one
 * (upstream #2562). A stored token Bambu has rejected downloads nothing, but it
 * is not "no token" either — saying "sign in" to someone who believes they
 * already are is what made the old banner confusing.
 */

import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { render } from '../utils';
import { MakerworldPage } from '../../pages/MakerworldPage';

function mockStatus(status: { has_cloud_token: boolean; can_download: boolean; sign_in_expired?: boolean }) {
  server.use(
    http.get('/api/v1/makerworld/status', () => HttpResponse.json(status)),
    http.get('/api/v1/library/folders', () => HttpResponse.json([])),
  );
}

describe('MakerworldPage sign-in banner', () => {
  it('says the sign-in expired when Bambu rejected the stored token', async () => {
    mockStatus({ has_cloud_token: true, can_download: false, sign_in_expired: true });

    render(<MakerworldPage />);

    expect(await screen.findByText('Bambu Cloud sign-in expired')).toBeInTheDocument();
    expect(screen.queryByText('Bambu Cloud sign-in required to download')).not.toBeInTheDocument();
  });

  it('asks to sign in when there is no token', async () => {
    mockStatus({ has_cloud_token: false, can_download: false, sign_in_expired: false });

    render(<MakerworldPage />);

    expect(await screen.findByText('Bambu Cloud sign-in required to download')).toBeInTheDocument();
    expect(screen.queryByText('Bambu Cloud sign-in expired')).not.toBeInTheDocument();
  });

  it('shows no banner when downloads work', async () => {
    mockStatus({ has_cloud_token: true, can_download: true, sign_in_expired: false });

    render(<MakerworldPage />);

    await waitFor(() => expect(screen.getByText('Import from MakerWorld')).toBeInTheDocument());
    expect(screen.queryByText('Bambu Cloud sign-in required to download')).not.toBeInTheDocument();
    expect(screen.queryByText('Bambu Cloud sign-in expired')).not.toBeInTheDocument();
  });
});
