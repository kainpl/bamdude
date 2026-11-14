/**
 * The token list reads the tokens again when a token was created somewhere
 * else on the page — the streaming-overlay builder below it creates tokens too.
 */

import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import CameraTokensPanel from '../../components/settings/CameraTokensPanel';

const token = (id: number, name: string) => ({
  id,
  user_id: 1,
  name,
  scope: 'overlay',
  lookup_prefix: `0000000${id}`,
  token: null,
  token_identity: `identity-${id}`,
  created_at: '2026-01-01T00:00:00',
  expires_at: '2099-01-01T00:00:00',
  last_used_at: null,
});

describe('CameraTokensPanel', () => {
  it('reads the tokens again when its refresh key moves', async () => {
    let tokens = [token(1, 'Wall display')];
    let reads = 0;
    server.use(
      http.get('/api/v1/auth/tokens', () => {
        reads += 1;
        return HttpResponse.json(tokens);
      }),
      http.get('/api/v1/auth/tokens/all', () => HttpResponse.json(tokens)),
      http.get('/api/v1/users/slim', () => HttpResponse.json([])),
    );
    const { rerender } = render(<CameraTokensPanel refreshKey={0} />);

    await waitFor(() => expect(screen.getByText('Wall display')).toBeInTheDocument());
    const before = reads;

    // The same key reads nothing again.
    rerender(<CameraTokensPanel refreshKey={0} />);
    expect(reads).toBe(before);

    tokens = [...tokens, token(2, 'OBS scene')];
    rerender(<CameraTokensPanel refreshKey={1} />);

    await waitFor(() => expect(screen.getByText('OBS scene')).toBeInTheDocument());
    expect(reads).toBe(before + 1);
  });
});
