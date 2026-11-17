/**
 * The Manyfold connection card: the secret is write-only (presence, keep / replace / clear),
 * a draft can be tested without saving, and the test says what it found.
 */

import { describe, it, expect } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
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

  it('clears the typed secret once it is saved, also when nothing else changed', async () => {
    const sent = stored(CONNECTED);
    // The server's real answer: the stored connection, field for field as before.
    server.use(
      http.put(URL, async ({ request }) => {
        sent.put.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json(CONNECTED);
      }),
    );
    render(<ManyfoldConnectionCard />);
    await screen.findByDisplayValue('client-abc');

    fireEvent.change(screen.getByRole('combobox', { name: 'Credential action' }), { target: { value: 'replace' } });
    fireEvent.change(document.querySelector('input[type="password"]') as HTMLInputElement, { target: { value: 'n3w-secret' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));
    await waitFor(() => expect(sent.put).toHaveLength(1));
    expect(sent.put[0].client_secret).toBe('n3w-secret');

    // The answer equals what was stored before (only the secret changed): the form still lets go of it.
    await waitFor(() => expect(document.querySelector('input[type="password"]')).toBeNull());
    expect(screen.getByRole('combobox', { name: 'Credential action' })).toHaveValue('keep');
    // A later save sends no secret again.
    fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));
    await waitFor(() => expect(sent.put).toHaveLength(2));
    expect(sent.put[1]).not.toHaveProperty('client_secret');
    expect(sent.put[1].secret_actions).toEqual({ manyfold_client_secret: 'keep' });
  });

  it('asks for the secret again as soon as the address of a saved connection is edited', async () => {
    const sent = stored(CONNECTED);
    render(<ManyfoldConnectionCard />);
    const url = await screen.findByDisplayValue('http://manyfold.lan:3214');
    const action = screen.getByRole('combobox', { name: 'Credential action' });

    // The path "Test connection" itself sends the admin down: http:// → https://.
    fireEvent.change(url, { target: { value: 'https://manyfold.lan:3214' } });
    expect(action).toHaveValue('replace');
    expect(document.querySelector('input[type="password"]')).not.toBeNull();
    expect(screen.getByText(/The stored secret belongs to the address and client ID it was saved for/)).toBeInTheDocument();

    // Nothing is sent until the secret is entered — and the card says what is missing.
    fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Enter the client secret');
    expect(sent.put).toHaveLength(0);
    expect(sent.test).toHaveLength(0);

    fireEvent.change(document.querySelector('input[type="password"]') as HTMLInputElement, { target: { value: 's3cret' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));
    await waitFor(() => expect(sent.put).toHaveLength(1));
    expect(sent.put[0]).toMatchObject({
      url: 'https://manyfold.lan:3214',
      client_secret: 's3cret',
      secret_actions: { manyfold_client_secret: 'replace' },
    });
  });

  it('goes back to keeping the secret when the address and client ID are put back', async () => {
    stored(CONNECTED);
    render(<ManyfoldConnectionCard />);
    const clientId = await screen.findByDisplayValue('client-abc');

    fireEvent.change(clientId, { target: { value: 'client-xyz' } });
    expect(screen.getByRole('combobox', { name: 'Credential action' })).toHaveValue('replace');
    fireEvent.change(clientId, { target: { value: 'client-abc' } });
    expect(screen.getByRole('combobox', { name: 'Credential action' })).toHaveValue('keep');
    expect(document.querySelector('input[type="password"]')).toBeNull();
  });

  it('opens the secret field when the server says the kept secret cannot follow the new address', async () => {
    const sent = stored(CONNECTED);
    server.use(
      http.put(URL, async ({ request }) => {
        sent.put.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json(
          { detail: { error: 'manyfold_secret_required', message: 'Enter the client secret again: the address or client ID changed.' } },
          { status: 400 },
        );
      }),
    );
    render(<ManyfoldConnectionCard />);
    const url = await screen.findByDisplayValue('http://manyfold.lan:3214');
    fireEvent.change(url, { target: { value: 'https://manyfold.lan:3214' } });
    // The admin insists on "Keep": the server's refusal code brings the field back.
    fireEvent.change(screen.getByRole('combobox', { name: 'Credential action' }), { target: { value: 'keep' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));

    await waitFor(() => expect(sent.put).toHaveLength(1));
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Credential action' })).toHaveValue('replace'));
    expect(document.querySelector('input[type="password"]')).not.toBeNull();
    expect(screen.getByRole('alert')).toHaveTextContent('Enter the client secret');
    // No settings-key name, no raw sentence in a toast.
    expect(screen.queryByText(/manyfold_client_secret/)).not.toBeInTheDocument();
  });

  it('a test that answers "secret required" opens the secret field too', async () => {
    stored(CONNECTED);
    server.use(
      http.post(`${URL}/test`, () =>
        HttpResponse.json({ ok: false, models: null, error: 'manyfold_secret_required', message: 'Enter the client secret again.' }),
      ),
    );
    render(<ManyfoldConnectionCard />);
    const url = await screen.findByDisplayValue('http://manyfold.lan:3214');
    fireEvent.change(url, { target: { value: 'https://manyfold.lan:3214' } });
    fireEvent.change(screen.getByRole('combobox', { name: 'Credential action' }), { target: { value: 'keep' } });
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));

    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Credential action' })).toHaveValue('replace'));
    expect(screen.getByRole('alert')).toHaveTextContent('Enter the client secret');
  });

  it('offers neither Save nor Test until the stored connection has loaded', async () => {
    const sent = stored(CONNECTED);
    let arrive: () => void = () => {};
    const gate = new Promise<void>((resolve) => { arrive = resolve; });
    server.use(
      http.get(URL, async () => {
        await gate;
        return HttpResponse.json(CONNECTED);
      }),
    );
    render(<ManyfoldConnectionCard />);

    // An empty form is not the stored connection: saving it would wipe the address and switch Manyfold off.
    const save = await screen.findByRole('button', { name: 'Save connection' });
    expect(save).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Test connection' })).toBeDisabled();
    expect(screen.getByLabelText('Manyfold URL')).toBeDisabled();
    fireEvent.click(save);
    expect(sent.put).toHaveLength(0);

    arrive();
    await screen.findByDisplayValue('client-abc');
    expect(screen.getByRole('button', { name: 'Save connection' })).toBeEnabled();
  });

  it('forgets the test result when the draft it was about changes', async () => {
    stored(CONNECTED);
    render(<ManyfoldConnectionCard />);
    const clientId = await screen.findByDisplayValue('client-abc');
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    await screen.findByText('Connected. Models BamDude can see: 37');

    fireEvent.change(clientId, { target: { value: 'client-other' } });
    expect(screen.queryByText('Connected. Models BamDude can see: 37')).not.toBeInTheDocument();
  });

  it('clears the stored secret only when "Clear" is chosen', async () => {
    const sent = stored(CONNECTED);
    render(<ManyfoldConnectionCard />);
    await screen.findByDisplayValue('client-abc');

    fireEvent.change(screen.getByRole('combobox', { name: 'Credential action' }), { target: { value: 'clear' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));
    await waitFor(() => expect(sent.put).toHaveLength(1));
    expect(sent.put[0].secret_actions).toEqual({ manyfold_client_secret: 'clear' });
    expect(sent.put[0]).not.toHaveProperty('client_secret');
  });

  it('says why a save was refused by its code, and never repeats the secret', async () => {
    const sent = stored(CONNECTED);
    server.use(
      http.put(URL, async ({ request }) => {
        sent.put.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json({ detail: { error: 'manyfold_credentials', message: 'credentials refused: n3w-secret' } }, { status: 400 });
      }),
    );
    render(<ManyfoldConnectionCard />);
    await screen.findByDisplayValue('client-abc');
    fireEvent.change(screen.getByRole('combobox', { name: 'Credential action' }), { target: { value: 'replace' } });
    fireEvent.change(document.querySelector('input[type="password"]') as HTMLInputElement, { target: { value: 'n3w-secret' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));

    const toasts = within(screen.getByTestId('toast-viewport'));
    expect(await toasts.findByText('Manyfold did not accept the client ID or secret.')).toBeInTheDocument();
    expect(document.body.textContent).not.toContain('n3w-secret'); // only ever inside the masked field
  });

  it('says why a test failed when the server refuses it outright', async () => {
    stored(CONNECTED);
    server.use(
      http.post(`${URL}/test`, () =>
        HttpResponse.json({ detail: { error: 'manyfold_wrong_scheme', message: 'redirects to https' } }, { status: 400 }),
      ),
    );
    render(<ManyfoldConnectionCard />);
    await screen.findByDisplayValue('client-abc');
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));

    expect(await screen.findByText('Manyfold redirects to https. Enter its address starting with https://.')).toBeInTheDocument();
  });

  it('is read-only for a user who may read the settings and not change them', async () => {
    stored(CONNECTED);
    asUser(['settings:read']);
    render(<ManyfoldConnectionCard />);

    const url = await screen.findByDisplayValue('http://manyfold.lan:3214');
    await waitFor(() => expect(url).toBeDisabled());
    expect(screen.getByDisplayValue('client-abc')).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Save connection' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Test connection' })).not.toBeInTheDocument();
  });

  it('is not shown to a user the server does not let read the connection', async () => {
    stored(CONNECTED);
    asUser(['settings:read', 'settings:update']);
    server.use(http.get(URL, () => HttpResponse.json({ detail: 'Administrators only' }, { status: 403 })));
    const { container } = render(<ManyfoldConnectionCard />);

    await waitFor(() => expect(screen.queryByText('Manyfold connection')).not.toBeInTheDocument());
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it('stops offering Save to a user whose save the server forbids', async () => {
    const sent = stored(CONNECTED);
    asUser(['settings:read', 'settings:update']);
    server.use(
      http.put(URL, async ({ request }) => {
        sent.put.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json({ detail: 'Only an administrator can change the Manyfold connection' }, { status: 403 });
      }),
    );
    render(<ManyfoldConnectionCard />);
    await screen.findByDisplayValue('client-abc');
    fireEvent.click(await screen.findByRole('button', { name: 'Save connection' }));

    const toasts = within(screen.getByTestId('toast-viewport'));
    expect(await toasts.findByText('Only an administrator can change the Manyfold connection')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Save connection' })).not.toBeInTheDocument());
    expect(screen.getByDisplayValue('client-abc')).toBeDisabled();
  });

  it('says a connection that could not be read could not be read, with a way to ask again', async () => {
    stored(CONNECTED);
    let failing = true;
    server.use(
      http.get(URL, () =>
        failing ? HttpResponse.json({ detail: 'Bad gateway' }, { status: 400 }) : HttpResponse.json(CONNECTED),
      ),
    );
    render(<ManyfoldConnectionCard />);

    expect(await screen.findByText('BamDude could not read the Manyfold connection.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save connection' })).not.toBeInTheDocument();
    failing = false;
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByDisplayValue('client-abc')).toBeInTheDocument();
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
