/**
 * The MakerWorld page tells an expired Bambu Cloud sign-in from a missing one
 * (upstream #2562). A stored token Bambu has rejected downloads nothing, but it
 * is not "no token" either — saying "sign in" to someone who believes they
 * already are is what made the old banner confusing.
 */

import { describe, it, expect } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { render } from '../utils';
import { MakerworldPage } from '../../pages/MakerworldPage';

type Status = { has_cloud_token: boolean; can_download: boolean; sign_in_expired?: boolean };

function mockStatus(...answers: Status[]) {
  let call = 0;
  server.use(
    // Answers in order; the last one repeats for every later refetch.
    http.get('/api/v1/makerworld/status', () => HttpResponse.json(answers[Math.min(call++, answers.length - 1)])),
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

    // While the status loads, downloads read as off and the "required" banner
    // shows — wait for it to go, not merely for the page to render.
    await waitFor(() =>
      expect(screen.queryByText('Bambu Cloud sign-in required to download')).not.toBeInTheDocument(),
    );
    expect(screen.queryByText('Bambu Cloud sign-in expired')).not.toBeInTheDocument();
  });

  it('re-reads the sign-in state when MakerWorld refuses the token', async () => {
    // Loaded with a live token; Bambu rejects it on the next call, and the
    // backend marks it expired — the page must find out without a refocus.
    mockStatus(
      { has_cloud_token: true, can_download: true, sign_in_expired: false },
      { has_cloud_token: true, can_download: false, sign_in_expired: true },
    );
    server.use(
      http.post('/api/v1/makerworld/resolve', () =>
        HttpResponse.json(
          { detail: 'Your Bambu Cloud sign-in has expired. Open the Profiles page and sign in to Bambu Cloud again.' },
          { status: 401 },
        ),
      ),
    );

    render(<MakerworldPage />);
    await waitFor(() =>
      expect(screen.queryByText('Bambu Cloud sign-in required to download')).not.toBeInTheDocument(),
    );

    fireEvent.change(screen.getByPlaceholderText(/paste any MakerWorld link/), {
      target: { value: 'https://makerworld.com/en/models/1400373' },
    });
    fireEvent.click(screen.getByRole('button', { name: /Resolve/ }));

    expect(await screen.findByText('Bambu Cloud sign-in expired')).toBeInTheDocument();
  });
});
