/**
 * The streaming-overlay builder and the token: creating one, importing an
 * address that carries one, keeping it off the screen, and remembering every
 * choice EXCEPT it.
 *
 * The rule under all of it: a token in the builder lives in the component's
 * memory and in the address it builds, and nowhere else — not in browser
 * storage, not in a request, not on the screen until asked.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { StreamOverlayBuilder } from '../../components/StreamOverlayBuilder';

const printers = [
  { id: 1, name: 'X1 Carbon', ip_address: '192.168.1.100', serial_number: '00M09A350100001', model: 'X1C' },
  { id: 2, name: 'P1S', ip_address: '192.168.1.101', serial_number: '01P00A000000002', model: 'P1S' },
];

const SECRET = 'bblt_0a1b2c3d_SECRETSECRETSECRETSECRETSECRET00';
const STORAGE_KEY = 'streamOverlayBuilder';

const shownUrls = () => Array.from(document.querySelectorAll('code')).map((code) => code.textContent ?? '');
const shownUrl = () => shownUrls()[0] ?? '';
const realUrl = () => screen.getAllByRole('link', { name: 'Open' })[0].getAttribute('href') ?? '';

/** Everything either storage holds, as one string. */
function everythingStored(): string {
  const dump = (storage: Storage) =>
    Array.from({ length: storage.length }, (_, index) => {
      const key = storage.key(index) ?? '';
      return `${key}=${storage.getItem(key)}`;
    }).join('\n');
  return `${dump(window.localStorage)}\n${dump(window.sessionStorage)}`;
}

function user(permissions: string[], isAdmin = false) {
  server.use(
    http.get('/api/v1/auth/me', () =>
      HttpResponse.json({
        id: 7,
        username: 'operator',
        role: isAdmin ? 'admin' : 'user',
        is_active: true,
        is_admin: isAdmin,
        groups: [],
        permissions,
        created_at: '2024-01-01T00:00:00Z',
      }),
    ),
  );
}

async function ready() {
  await waitFor(() => expect(shownUrl()).toContain('/overlay/'));
  await waitFor(() => expect(screen.getByLabelText('Printer')).not.toHaveValue(''));
}

describe('StreamOverlayBuilder — token', () => {
  beforeEach(() => {
    window.localStorage.removeItem(STORAGE_KEY);
    server.use(http.get('/api/v1/printers', () => HttpResponse.json(printers)));
  });

  afterEach(() => {
    window.localStorage.removeItem(STORAGE_KEY);
    vi.restoreAllMocks();
  });

  describe('masking', () => {
    it('keeps a typed token off the screen until it is asked for', async () => {
      const actor = userEvent.setup();
      render(<StreamOverlayBuilder />);
      await ready();

      const field = screen.getByLabelText(/Streaming Overlay token/);
      expect(field).toHaveAttribute('type', 'password');
      await actor.type(field, SECRET);

      // The address to read hides it; the one that is copied and opened does not.
      await waitFor(() => expect(shownUrl()).toContain('token=****'));
      expect(document.body.textContent).not.toContain(SECRET);
      expect(realUrl()).toContain(`token=${SECRET}`);

      await actor.click(screen.getByRole('button', { name: 'Show token' }));
      expect(field).toHaveAttribute('type', 'text');
      expect(shownUrl()).toContain(`token=${SECRET}`);

      await actor.click(screen.getByRole('button', { name: 'Hide token' }));
      expect(field).toHaveAttribute('type', 'password');
      expect(shownUrl()).not.toContain(SECRET);
    });

    it('copies the real address while the shown one is masked', async () => {
      const actor = userEvent.setup();
      const writeText = vi.fn().mockResolvedValue(undefined);
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
      Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
      render(<StreamOverlayBuilder />);
      await ready();

      fireEvent.change(screen.getByLabelText(/Streaming Overlay token/), { target: { value: SECRET } });
      await waitFor(() => expect(shownUrl()).toContain('token=****'));
      await actor.click(screen.getByRole('button', { name: 'Copy' }));

      await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
      expect(writeText.mock.calls[0][0]).toContain(`token=${SECRET}`);
    });

    it('closes an open preview when the token changes', async () => {
      const actor = userEvent.setup();
      render(<StreamOverlayBuilder />);
      await ready();

      await actor.click(screen.getByText('Show preview'));
      await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull());

      fireEvent.change(screen.getByLabelText(/Streaming Overlay token/), { target: { value: SECRET } });
      await waitFor(() => expect(document.querySelector('iframe')).toBeNull());
    });
  });

  describe('create token', () => {
    it('offers no button to somebody who may not view cameras', async () => {
      user(['printers:read']);
      render(<StreamOverlayBuilder />);
      await ready();

      expect(screen.queryByRole('button', { name: 'Create token' })).not.toBeInTheDocument();
    });

    it('creates an overlay-scoped token through the ordinary endpoint and puts it in the address', async () => {
      user(['camera:view']);
      const actor = userEvent.setup();
      const bodies: unknown[] = [];
      server.use(
        http.post('/api/v1/auth/tokens', async ({ request }) => {
          bodies.push(await request.json());
          return HttpResponse.json(
            { id: 5, user_id: 7, name: 'OBS scene', scope: 'overlay', lookup_prefix: '0a1b2c3d', token: SECRET },
            { status: 201 },
          );
        }),
      );
      const onTokenCreated = vi.fn();
      render(<StreamOverlayBuilder onTokenCreated={onTokenCreated} />);
      await ready();

      await actor.click(await screen.findByRole('button', { name: 'Create token' }));
      await actor.type(screen.getByLabelText('Token name'), 'OBS scene');
      fireEvent.change(screen.getByLabelText('Valid for (days)'), { target: { value: '30' } });
      await actor.click(screen.getByRole('button', { name: 'Create' }));

      await waitFor(() => expect(realUrl()).toContain(`token=${SECRET}`));
      // The scope is not the user's to choose here: an overlay token, nothing wider.
      expect(bodies).toEqual([{ name: 'OBS scene', expires_in_days: 30, scope: 'overlay' }]);
      expect(onTokenCreated).toHaveBeenCalledTimes(1);
      // Created, and still not on the screen.
      expect(shownUrl()).toContain('token=****');
      expect(document.body.textContent).not.toContain(SECRET);
      expect(screen.getByText(/cannot be shown again/)).toBeInTheDocument();
      // …and nowhere in the browser's storage.
      expect(everythingStored()).not.toContain(SECRET);
    });

    it('keeps the form and the address as they were when the server refuses', async () => {
      user(['camera:view']);
      const actor = userEvent.setup();
      server.use(http.post('/api/v1/auth/tokens', () => HttpResponse.json({ detail: 'nope' }, { status: 403 })));
      const onTokenCreated = vi.fn();
      render(<StreamOverlayBuilder onTokenCreated={onTokenCreated} />);
      await ready();

      await actor.click(await screen.findByRole('button', { name: 'Create token' }));
      await actor.type(screen.getByLabelText('Token name'), 'OBS scene');
      await actor.click(screen.getByRole('button', { name: 'Create' }));

      await waitFor(() => expect(screen.getByRole('button', { name: 'Create' })).not.toBeDisabled());
      expect(realUrl()).not.toContain('token=');
      expect(onTokenCreated).not.toHaveBeenCalled();
      expect(screen.getByLabelText('Token name')).toHaveValue('OBS scene');
    });

    it('does not send a token without a name', async () => {
      user(['camera:view']);
      const actor = userEvent.setup();
      const called = vi.fn();
      server.use(
        http.post('/api/v1/auth/tokens', () => {
          called();
          return HttpResponse.json({}, { status: 201 });
        }),
      );
      render(<StreamOverlayBuilder />);
      await ready();

      await actor.click(await screen.findByRole('button', { name: 'Create token' }));
      expect(screen.getByRole('button', { name: 'Create' })).toBeDisabled();
      expect(called).not.toHaveBeenCalled();
    });
  });

  describe('import an address', () => {
    async function importAddress(text: string) {
      fireEvent.change(screen.getByLabelText('Import an overlay address'), { target: { value: text } });
      fireEvent.click(screen.getByRole('button', { name: 'Import' }));
    }

    it('loads every choice of a pasted address and throws its origin away', async () => {
      render(<StreamOverlayBuilder />);
      await ready();

      await importAddress(
        `https://other-farm.example:8443/overlay/2?show=printer,model,progress,nozzle&size=large&fps=7&camera=0`
        + `&progressFrom=%23FF8800&progressTo=0088ff&design=2&backgroundTransparency=35&layout=portrait&token=${SECRET}`,
      );

      await waitFor(() => expect(realUrl()).toContain('/overlay/2?'));
      const built = new URL(realUrl());
      // Ours, whatever was pasted.
      expect(built.origin).toBe(window.location.origin);
      expect(document.body.innerHTML).not.toContain('other-farm.example');
      expect(built.searchParams.get('show')).toBe('printer,model,progress,nozzle');
      expect(built.searchParams.get('size')).toBe('large');
      expect(built.searchParams.get('fps')).toBe('7');
      expect(built.searchParams.get('camera')).toBe('false');
      expect(built.searchParams.get('progressFrom')).toBe('ff8800');
      expect(built.searchParams.get('progressTo')).toBe('0088ff');
      expect(built.searchParams.get('design')).toBe('2');
      expect(built.searchParams.get('backgroundTransparency')).toBe('35');
      expect(built.searchParams.get('layout')).toBe('portrait');
      expect(built.searchParams.get('token')).toBe(SECRET);

      // The controls show it too.
      expect(screen.getByLabelText('Printer')).toHaveValue('2');
      expect(screen.getByLabelText('Text size')).toHaveValue('large');
      expect(screen.getByLabelText('Design')).toHaveValue('2');
      expect(screen.getByLabelText('Layout')).toHaveValue('portrait');
      expect(screen.getByLabelText('Printer model')).toBeChecked();
      expect(screen.getByLabelText('File name')).not.toBeChecked();
      expect(screen.getByLabelText('Camera feed')).not.toBeChecked();
      // The pasted text is gone from the field, and its token is not on screen.
      expect(screen.getByLabelText('Import an overlay address')).toHaveValue('');
      expect(document.body.textContent).not.toContain(SECRET);
    });

    it('keeps an imported token in memory only: no request, no storage, no open preview', async () => {
      const actor = userEvent.setup();
      const seen: string[] = [];
      const record = async ({ request }: { request: Request }) => {
        const body = request.method === 'GET' ? '' : await request.clone().text();
        seen.push(`${request.url} ${JSON.stringify(Array.from(request.headers.entries()))} ${body}`);
      };
      server.events.on('request:start', record);
      try {
        render(<StreamOverlayBuilder />);
        await ready();
        await actor.click(screen.getByText('Show preview'));
        await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull());

        await importAddress(`http://192.168.1.50:8000/overlay/1?token=${SECRET}`);

        await waitFor(() => expect(realUrl()).toContain(`token=${SECRET}`));
        // A preview would send the token to the server: it opens only when asked.
        expect(document.querySelector('iframe')).toBeNull();
        expect(everythingStored()).not.toContain(SECRET);
        expect(seen.join('\n')).not.toContain(SECRET);
        expect(seen.join('\n')).not.toContain('192.168.1.50');
      } finally {
        server.events.removeListener('request:start', record);
      }
    });

    it('shows a printer this server does not have by its number', async () => {
      render(<StreamOverlayBuilder />);
      await ready();

      await importAddress('/overlay/41?show=progress');

      await waitFor(() => expect(realUrl()).toContain('/overlay/41?'));
      expect(screen.getByLabelText('Printer')).toHaveValue('41');
    });

    it('keeps the token already there when the pasted address carries none', async () => {
      render(<StreamOverlayBuilder />);
      await ready();
      fireEvent.change(screen.getByLabelText(/Streaming Overlay token/), { target: { value: SECRET } });

      await importAddress('http://farm.local/overlay/2?size=small');

      await waitFor(() => expect(realUrl()).toContain('/overlay/2?'));
      expect(realUrl()).toContain(`token=${SECRET}`);
    });

    it.each([
      ['not an address at all', 'hello there'],
      ['another page', 'https://farm.local/printers/2?show=progress'],
      ['a script address', 'javascript:alert(1)//overlay/2'],
      ['a data address', 'data:text/html,/overlay/2'],
      ['a name and password in it', 'https://admin:hunter2@farm.local/overlay/2'],
      ['two tokens', `https://farm.local/overlay/2?token=${SECRET}&token=other`],
      ['a printer that is not a number', 'https://farm.local/overlay/abc'],
      ['printer zero', 'https://farm.local/overlay/0'],
      ['a negative printer', 'https://farm.local/overlay/-3'],
      ['something after the printer', 'https://farm.local/overlay/2/edit'],
      ['nothing', '   '],
    ])('refuses %s and changes nothing', async (_name, text) => {
      render(<StreamOverlayBuilder />);
      await ready();
      const before = realUrl();

      await importAddress(text);

      await waitFor(() => expect(screen.getByText('That is not a stream overlay address.')).toBeInTheDocument());
      expect(realUrl()).toBe(before);
      expect(realUrl()).not.toContain('token=');
    });

    it('ignores what the address gets wrong, as the overlay itself would', async () => {
      render(<StreamOverlayBuilder />);
      await ready();

      await importAddress(
        'https://farm.local/overlay/2?show=progress,banana,layers&size=huge&fps=900&progressFrom=red&progressTo=blue'
        + '&design=9&backgroundTransparency=40&layout=diagonal',
      );

      await waitFor(() => expect(realUrl()).toContain('/overlay/2?'));
      const built = new URL(realUrl());
      expect(built.searchParams.get('show')).toBe('progress,layers');
      expect(built.searchParams.has('size')).toBe(false);
      expect(built.searchParams.get('fps')).toBe('30');
      expect(built.searchParams.has('progressFrom')).toBe(false);
      expect(built.searchParams.has('design')).toBe(false);
      // Transparency belongs to Version 2 only.
      expect(built.searchParams.has('backgroundTransparency')).toBe(false);
      expect(built.searchParams.has('layout')).toBe(false);
    });
  });

  describe('remembered choices', () => {
    it('comes back as it was left, without the token', async () => {
      const actor = userEvent.setup();
      const first = render(<StreamOverlayBuilder />);
      await ready();

      await actor.selectOptions(screen.getByLabelText('Printer'), '2');
      await actor.selectOptions(screen.getByLabelText('Text size'), 'large');
      await actor.selectOptions(screen.getByLabelText('Design'), '2');
      await actor.selectOptions(screen.getByLabelText('Layout'), 'both');
      await actor.click(screen.getByLabelText('Printer model'));
      await actor.click(screen.getByLabelText('Custom progress colours'));
      fireEvent.change(screen.getByLabelText('Start colour'), { target: { value: '#ff8800' } });
      fireEvent.change(screen.getByLabelText(/Background transparency/), { target: { value: '25' } });
      fireEvent.change(screen.getByLabelText(/Streaming Overlay token/), { target: { value: SECRET } });
      await waitFor(() => expect(realUrl()).toContain(`token=${SECRET}`));
      const left = shownUrls().map((url) => url.replace('&token=****', ''));
      first.unmount();

      // Not under its own key, not under anybody else's.
      expect(everythingStored()).not.toContain(SECRET);
      expect(window.localStorage.getItem(STORAGE_KEY)).not.toBeNull();
      expect(window.localStorage.getItem(STORAGE_KEY)).not.toContain('token');

      render(<StreamOverlayBuilder />);
      await ready();
      await waitFor(() => expect(shownUrls()).toEqual(left));
      expect(realUrl()).not.toContain('token=');
      expect(screen.getByLabelText(/Streaming Overlay token/)).toHaveValue('');
    });

    it('never writes the token, whichever way it arrives', async () => {
      user(['camera:view']);
      const actor = userEvent.setup();
      const written: string[] = [];
      vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
        written.push(`${key}=${value}`);
      });
      server.use(
        http.post('/api/v1/auth/tokens', () =>
          HttpResponse.json(
            { id: 5, user_id: 7, name: 'OBS', scope: 'overlay', lookup_prefix: '0a1b2c3d', token: `${SECRET}created` },
            { status: 201 },
          ),
        ),
      );
      render(<StreamOverlayBuilder />);
      await ready();

      // Typed.
      fireEvent.change(screen.getByLabelText(/Streaming Overlay token/), { target: { value: `${SECRET}typed` } });
      await actor.selectOptions(screen.getByLabelText('Text size'), 'small');
      // Imported.
      fireEvent.change(screen.getByLabelText('Import an overlay address'), {
        target: { value: `https://farm.local/overlay/2?size=large&token=${SECRET}imported` },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Import' }));
      await waitFor(() => expect(realUrl()).toContain(`${SECRET}imported`));
      // Created.
      await actor.click(await screen.findByRole('button', { name: 'Create token' }));
      await actor.type(screen.getByLabelText('Token name'), 'OBS');
      await actor.click(screen.getByRole('button', { name: 'Create' }));
      await waitFor(() => expect(realUrl()).toContain(`${SECRET}created`));
      await actor.selectOptions(screen.getByLabelText('Layout'), 'landscape');

      await waitFor(() => expect(written.some((entry) => entry.includes('"layout":"landscape"'))).toBe(true));
      expect(written.join('\n')).not.toContain(SECRET);
      // What the builder itself writes has no place for one.
      const own = written.filter((entry) => entry.startsWith(`${STORAGE_KEY}=`));
      expect(own.length).toBeGreaterThan(0);
      expect(own.join('\n')).not.toContain('token');
    });

    it.each([
      ['not JSON', '{{{'],
      ['not an object', '"hello"'],
      ['an array', '[1,2,3]'],
      ['null', 'null'],
    ])('starts from the defaults when storage holds %s', async (_name, stored) => {
      window.localStorage.setItem(STORAGE_KEY, stored);
      render(<StreamOverlayBuilder />);
      await ready();

      expect(shownUrl()).toContain('/overlay/1?show=filename%2Cstatus%2Cprogress%2Clayers%2Ceta');
      expect(shownUrl()).not.toContain('size=');
    });

    it('checks every remembered value the way the overlay would', async () => {
      window.localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({
          printerId: 2,
          fields: ['progress', 'banana', 7, 'layers'],
          size: 'huge',
          fps: 900,
          showCamera: 'no',
          customColours: true,
          progressFrom: 'red;background:url(x)',
          progressTo: '#0000ff',
          design: 'v3',
          transparency: 400,
          layout: 'diagonal',
          token: SECRET,
        }),
      );
      render(<StreamOverlayBuilder />);
      await ready();

      await waitFor(() => expect(shownUrl()).toContain('/overlay/2?'));
      const built = new URL(realUrl());
      expect(built.searchParams.get('show')).toBe('progress,layers');
      expect(Array.from(built.searchParams.keys())).toEqual(['show']);
      // A token somebody planted in storage is not read either.
      expect(realUrl()).not.toContain(SECRET);
    });

    it('falls back to the first printer when the remembered one is gone', async () => {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ printerId: 99, size: 'large' }));
      render(<StreamOverlayBuilder />);
      await ready();

      await waitFor(() => expect(shownUrl()).toContain('/overlay/1?'));
      expect(shownUrl()).toContain('size=large');
    });

    it('still works when the browser refuses storage', async () => {
      const actor = userEvent.setup();
      // Only the builder's own key: the test's sign-in lives in storage too.
      const read = Storage.prototype.getItem;
      const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(function (this: Storage, key: string) {
        if (key === STORAGE_KEY) throw new DOMException('denied', 'SecurityError');
        return read.call(this, key);
      });
      const write = Storage.prototype.setItem;
      const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
        if (key === STORAGE_KEY) throw new DOMException('full', 'QuotaExceededError');
        write.call(this, key, value);
      });
      render(<StreamOverlayBuilder />);
      await ready();

      await actor.selectOptions(screen.getByLabelText('Text size'), 'large');
      await waitFor(() => expect(shownUrl()).toContain('size=large'));
      // Both were really refused, not skipped.
      expect(getItem.mock.calls.some(([key]) => key === STORAGE_KEY)).toBe(true);
      expect(setItem.mock.calls.some(([key]) => key === STORAGE_KEY)).toBe(true);
    });

    it('resets the choices and keeps the token', async () => {
      const actor = userEvent.setup();
      const first = render(<StreamOverlayBuilder />);
      await ready();
      await actor.selectOptions(screen.getByLabelText('Printer'), '2');
      await actor.selectOptions(screen.getByLabelText('Text size'), 'large');
      await actor.selectOptions(screen.getByLabelText('Design'), '2');
      fireEvent.change(screen.getByLabelText(/Streaming Overlay token/), { target: { value: SECRET } });
      await waitFor(() => expect(realUrl()).toContain('design=2'));

      await actor.click(screen.getByRole('button', { name: 'Reset choices' }));

      await waitFor(() => expect(realUrl()).not.toContain('design=2'));
      expect(realUrl()).toContain('/overlay/1?show=filename%2Cstatus%2Cprogress%2Clayers%2Ceta');
      expect(realUrl()).not.toContain('size=');
      expect(realUrl()).toContain(`token=${SECRET}`);
      first.unmount();

      // And the next visit starts from the defaults too.
      render(<StreamOverlayBuilder />);
      await ready();
      expect(shownUrl()).toContain('/overlay/1?show=filename%2Cstatus%2Cprogress%2Clayers%2Ceta');
      expect(shownUrl()).not.toContain('design=');
    });
  });
});
