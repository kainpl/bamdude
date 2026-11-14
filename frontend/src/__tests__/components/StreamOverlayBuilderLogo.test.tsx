/**
 * The overlay builder's logo controls.
 *
 * One logo per installation. Whoever may change settings uploads, replaces and
 * removes it here; everybody else sees what is saved and why they cannot change
 * it. "Show the logo" is one of the builder's remembered choices and becomes
 * `logo=1` in the address.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { StreamOverlayBuilder } from '../../components/StreamOverlayBuilder';

const printers = [
  { id: 1, name: 'X1 Carbon', ip_address: '192.168.1.100', serial_number: '00M09A350100001', model: 'X1C' },
];
const STORAGE_KEY = 'streamOverlayBuilder';

const shownUrl = () => document.querySelector('code')?.textContent ?? '';
const preview = () => document.querySelector<HTMLImageElement>('img[data-overlay-logo-preview]');

function user(permissions: string[]) {
  server.use(
    http.get('/api/v1/auth/me', () =>
      HttpResponse.json({
        id: 7,
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

describe('StreamOverlayBuilder — logo', () => {
  let saved: { exists: boolean; revision: string | null };
  let calls: string[];
  let uploadedAs: string | null;

  beforeEach(() => {
    window.localStorage.removeItem(STORAGE_KEY);
    saved = { exists: false, revision: null };
    calls = [];
    uploadedAs = null;
    vi.spyOn(URL, 'createObjectURL').mockImplementation(() => `blob:logo-${saved.revision}`);
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
    server.use(
      http.get('/api/v1/printers', () => HttpResponse.json(printers)),
      http.get('/api/v1/settings/overlay-logo/info', () => HttpResponse.json(saved)),
      http.get('/api/v1/settings/overlay-logo', () =>
        saved.exists
          ? new HttpResponse(new Blob(['png']), { headers: { 'Content-Type': 'image/png' } })
          : HttpResponse.json({ detail: 'No overlay logo is saved' }, { status: 404 }),
      ),
      http.post('/api/v1/settings/overlay-logo', ({ request }) => {
        calls.push('upload');
        uploadedAs = request.headers.get('content-type');
        saved = { exists: true, revision: 'rev-new' };
        return HttpResponse.json(saved);
      }),
      http.delete('/api/v1/settings/overlay-logo', () => {
        calls.push('remove');
        saved = { exists: false, revision: null };
        return HttpResponse.json(saved);
      }),
    );
  });

  afterEach(() => {
    window.localStorage.removeItem(STORAGE_KEY);
    vi.restoreAllMocks();
  });

  describe('the choice', () => {
    it('leaves the address alone until it is ticked, then adds logo=1 and remembers it', async () => {
      const actor = userEvent.setup();
      const first = render(<StreamOverlayBuilder />);
      await waitFor(() => expect(shownUrl()).toContain('/overlay/1'));
      expect(shownUrl()).not.toContain('logo=');

      await actor.click(screen.getByLabelText('Show the logo on the overlay'));

      await waitFor(() => expect(shownUrl()).toContain('logo=1'));
      await waitFor(() => expect(window.localStorage.getItem(STORAGE_KEY)).toContain('"showLogo":true'));
      // Before the token, like every other choice: the token is the last parameter.
      first.unmount();
      render(<StreamOverlayBuilder />);
      await waitFor(() => expect(shownUrl()).toContain('logo=1'));
      expect(screen.getByLabelText('Show the logo on the overlay')).toBeChecked();
    });

    it('reads it off an imported address', async () => {
      render(<StreamOverlayBuilder />);
      await waitFor(() => expect(shownUrl()).toContain('/overlay/1'));

      fireEvent.change(screen.getByLabelText('Import an overlay address'), {
        target: { value: 'https://farm.example/overlay/1?show=progress&logo=1' },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Import' }));

      await waitFor(() => expect(screen.getByLabelText('Show the logo on the overlay')).toBeChecked());
      expect(shownUrl()).toContain('logo=1');
    });

    it('goes back off with the other choices', async () => {
      const actor = userEvent.setup();
      render(<StreamOverlayBuilder />);
      await waitFor(() => expect(shownUrl()).toContain('/overlay/1'));
      await actor.click(screen.getByLabelText('Show the logo on the overlay'));
      await waitFor(() => expect(shownUrl()).toContain('logo=1'));

      await actor.click(screen.getByRole('button', { name: 'Reset choices' }));

      await waitFor(() => expect(shownUrl()).not.toContain('logo='));
    });

    it('says so when no logo is saved yet', async () => {
      render(<StreamOverlayBuilder />);

      expect(await screen.findByText(/No logo is saved yet/)).toBeInTheDocument();
      expect(preview()).toBeNull();
    });
  });

  describe('somebody who may change settings', () => {
    beforeEach(() => user(['settings:read', 'settings:update', 'camera:view']));

    it('uploads a logo and sees it', async () => {
      const actor = userEvent.setup();
      render(<StreamOverlayBuilder />);
      const input = await screen.findByLabelText('Upload a logo');
      const file = new File(['png-bytes'], 'brand.png', { type: 'image/png' });

      await actor.upload(input, file);

      await waitFor(() => expect(calls).toEqual(['upload']));
      // A form with the file in it, its boundary written by the browser.
      expect(uploadedAs).toMatch(/^multipart\/form-data; boundary=/);
      await waitFor(() => expect(preview()?.getAttribute('src')).toBe('blob:logo-rev-new'));
      expect(screen.queryByText(/No logo is saved yet/)).toBeNull();
    });

    it('offers only the formats the server keeps', async () => {
      render(<StreamOverlayBuilder />);

      const input = await screen.findByLabelText('Upload a logo');
      expect(input.getAttribute('accept')).toBe('image/png,image/jpeg,image/webp');
    });

    it('shows the server\'s own sentence when a file is refused, and keeps the saved logo', async () => {
      saved = { exists: true, revision: 'rev-old' };
      server.use(
        http.post('/api/v1/settings/overlay-logo', () =>
          HttpResponse.json({ detail: 'The logo must be a PNG, JPEG or WebP image' }, { status: 422 }),
        ),
      );
      const actor = userEvent.setup({ applyAccept: false });
      render(<StreamOverlayBuilder />);
      await waitFor(() => expect(preview()?.getAttribute('src')).toBe('blob:logo-rev-old'));

      await actor.upload(screen.getByLabelText('Replace the logo'), new File(['<svg/>'], 'x.svg', { type: 'image/svg+xml' }));

      expect(await screen.findByText('The logo must be a PNG, JPEG or WebP image')).toBeInTheDocument();
      expect(preview()?.getAttribute('src')).toBe('blob:logo-rev-old');
    });

    it('removes the logo only after a yes', async () => {
      saved = { exists: true, revision: 'rev-old' };
      const actor = userEvent.setup();
      render(<StreamOverlayBuilder />);
      await waitFor(() => expect(preview()).not.toBeNull());

      await actor.click(screen.getByRole('button', { name: 'Remove logo' }));
      const dialog = await screen.findByRole('dialog');
      expect(calls).toEqual([]);

      await actor.click(within(dialog).getByRole('button', { name: 'Remove logo' }));

      await waitFor(() => expect(calls).toEqual(['remove']));
      await waitFor(() => expect(preview()).toBeNull());
      expect(await screen.findByText(/No logo is saved yet/)).toBeInTheDocument();
    });
  });

  describe('somebody who may not', () => {
    beforeEach(() => user(['settings:read', 'camera:view']));

    it('sees the saved logo and the choice, with no way to change the logo and the reason why', async () => {
      saved = { exists: true, revision: 'rev-old' };
      render(<StreamOverlayBuilder />);

      await waitFor(() => expect(preview()).not.toBeNull());
      expect(screen.getByLabelText('Show the logo on the overlay')).toBeEnabled();
      expect(screen.queryByLabelText('Upload a logo')).toBeNull();
      expect(screen.queryByLabelText('Replace the logo')).toBeNull();
      expect(screen.queryByRole('button', { name: 'Remove logo' })).toBeNull();
      expect(screen.getByText(/Only somebody who may change settings/)).toBeInTheDocument();
    });
  });
});
