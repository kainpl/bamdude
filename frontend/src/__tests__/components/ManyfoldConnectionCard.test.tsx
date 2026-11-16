/**
 * The Manyfold connection card: the secret is write-only (presence, keep / replace / clear),
 * a draft can be tested without saving, and the test says what it found.
 */

import { describe, it, expect } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { render } from '../utils';
import { ManyfoldConnectionCard } from '../../components/ManyfoldConnectionCard';

const URL = '/api/v1/model-sources/manyfold/connection';

function stored(connection: Record<string, unknown>) {
  const sent: { put: Array<Record<string, unknown>>; test: Array<Record<string, unknown>> } = { put: [], test: [] };
  server.use(
    http.get(URL, () => HttpResponse.json(connection)),
    http.put(URL, async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      sent.put.push(body);
      return HttpResponse.json({ ...connection, ...body, client_secret_configured: true, client_secret: undefined });
    }),
    http.post(`${URL}/test`, async ({ request }) => {
      sent.test.push((await request.json()) as Record<string, unknown>);
      return HttpResponse.json({ ok: true, models: 37, error: null, message: null });
    }),
  );
  return sent;
}

const CONNECTED = { enabled: true, url: 'http://manyfold.lan:3214', client_id: 'client-abc', client_secret_configured: true };
const EMPTY = { enabled: false, url: '', client_id: '', client_secret_configured: false };

describe('ManyfoldConnectionCard', () => {
  it('shows that a secret is stored, never the secret, and keeps it on save', async () => {
    const sent = stored(CONNECTED);
    render(<ManyfoldConnectionCard />);

    expect(await screen.findByDisplayValue('http://manyfold.lan:3214')).toBeInTheDocument();
    expect(screen.getByDisplayValue('client-abc')).toBeInTheDocument();
    expect(document.querySelector('input[type="password"]')).toBeNull(); // nothing to type into while it is kept

    fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));
    await waitFor(() => expect(sent.put).toHaveLength(1));
    expect(sent.put[0]).toEqual({
      enabled: true,
      url: 'http://manyfold.lan:3214',
      client_id: 'client-abc',
      secret_actions: { manyfold_client_secret: 'keep' },
    });
  });

  it('sends a new secret as a replacement', async () => {
    const sent = stored(EMPTY);
    render(<ManyfoldConnectionCard />);
    // The stored connection has arrived once the (empty) secret can be typed.
    await waitFor(() => expect(document.querySelector('input[type="password"]')).not.toBeNull());
    const url = screen.getByLabelText('Manyfold URL');

    fireEvent.change(url, { target: { value: ' http://192.168.1.10:3214 ' } });
    fireEvent.change(screen.getByLabelText('Client ID'), { target: { value: 'abc' } });
    fireEvent.change(document.querySelector('input[type="password"]') as HTMLInputElement, { target: { value: 's3cret' } });
    fireEvent.click(screen.getByRole('switch'));
    fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));

    await waitFor(() => expect(sent.put).toHaveLength(1));
    expect(sent.put[0]).toEqual({
      enabled: true,
      url: 'http://192.168.1.10:3214',
      client_id: 'abc',
      client_secret: 's3cret',
      secret_actions: { manyfold_client_secret: 'replace' },
    });
  });

  it('saving an empty form sends no empty replacement', async () => {
    const sent = stored(EMPTY);
    render(<ManyfoldConnectionCard />);
    await waitFor(() => expect(document.querySelector('input[type="password"]')).not.toBeNull());

    fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));
    await waitFor(() => expect(sent.put).toHaveLength(1));
    expect(sent.put[0]).toEqual({ enabled: false, url: '', client_id: '' });
  });

  it('tests the draft without saving and says how many models it sees', async () => {
    const sent = stored(CONNECTED);
    render(<ManyfoldConnectionCard />);
    await screen.findByDisplayValue('client-abc');

    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));

    expect(await screen.findByText('Connected. Models BamDude can see: 37')).toBeInTheDocument();
    expect(sent.test).toHaveLength(1);
    expect(sent.put).toHaveLength(0);
  });

  it('says in words that an instance is too old', async () => {
    stored(CONNECTED);
    server.use(
      http.post(`${URL}/test`, () =>
        HttpResponse.json({ ok: false, models: null, error: 'manyfold_too_old', message: 'This Manyfold is too old (needs 0.150 or newer).' }),
      ),
    );
    render(<ManyfoldConnectionCard />);
    await screen.findByDisplayValue('client-abc');

    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));

    expect(await screen.findByText('This Manyfold is too old. BamDude needs Manyfold 0.150 or newer.')).toBeInTheDocument();
  });

  it('tells the reader that a self-signed certificate is not accepted', async () => {
    stored(EMPTY);
    render(<ManyfoldConnectionCard />);

    expect(await screen.findByText(/self-signed certificate is not accepted/)).toBeInTheDocument();
  });
});
