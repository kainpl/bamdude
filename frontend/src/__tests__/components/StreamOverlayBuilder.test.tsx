/**
 * Tests for the streaming-overlay URL builder.
 *
 * The builder's whole output is a URL, so that is what these assert: the field
 * order, what is omitted at its default, and that the preview does not open a
 * camera stream until it is asked to.
 */

import { describe, it, expect, beforeEach } from 'vitest';
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

// The URL is rendered inside a <code>, so read it back the way a user would.
function shownUrl(): string {
  const code = document.querySelector('code');
  return code?.textContent ?? '';
}

describe('StreamOverlayBuilder', () => {
  beforeEach(() => {
    server.use(http.get('/api/v1/printers', () => HttpResponse.json(printers)));
  });

  it('starts on the first printer with the overlay defaults', async () => {
    render(<StreamOverlayBuilder />);

    await waitFor(() => {
      expect(shownUrl()).toContain('/overlay/1');
    });
    // The same set parseConfig() defaults to, so the builder's starting point
    // and a bare /overlay/1 render the same overlay. Emitted in the overlay's
    // own top-to-bottom field order rather than parseConfig's listing order —
    // ?show= is read with includes(), so order is free to be the stable one.
    expect(shownUrl()).toContain('show=filename%2Cstatus%2Cprogress%2Clayers%2Ceta');
    // Defaults are omitted rather than spelled out — a shorter URL to paste.
    expect(shownUrl()).not.toContain('size=');
    expect(shownUrl()).not.toContain('fps=');
    expect(shownUrl()).not.toContain('camera=');
    expect(shownUrl()).not.toContain('token=');
  });

  it('switches printer', async () => {
    const user = userEvent.setup();
    render(<StreamOverlayBuilder />);

    await waitFor(() => expect(screen.getByLabelText('Printer')).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText('Printer'), '2');

    await waitFor(() => expect(shownUrl()).toContain('/overlay/2'));
  });

  it('adds a temperature field the URL did not have', async () => {
    const user = userEvent.setup();
    render(<StreamOverlayBuilder />);

    await waitFor(() => expect(screen.getByLabelText('Nozzle')).toBeInTheDocument());
    await user.click(screen.getByLabelText('Nozzle'));

    await waitFor(() =>
      expect(shownUrl()).toContain('show=filename%2Cstatus%2Cprogress%2Clayers%2Ceta%2Cnozzle'),
    );
  });

  it('emits fields in the overlay order, not the order they were clicked', async () => {
    const user = userEvent.setup();
    render(<StreamOverlayBuilder />);

    // "Printer name" is first in the overlay's own top-to-bottom order, so
    // ticking it last must still put it at the front. Otherwise the same
    // selection would produce a different URL depending on click order, and a
    // scene file would stop being comparable to the one next to it.
    await waitFor(() => expect(screen.getByLabelText('Printer name')).toBeInTheDocument());
    await user.click(screen.getByLabelText('Printer name'));

    await waitFor(() => expect(shownUrl()).toContain('show=printer%2Cfilename'));
  });

  it('adds the printer model right after the name', async () => {
    const user = userEvent.setup();
    render(<StreamOverlayBuilder />);

    await waitFor(() => expect(screen.getByLabelText('Printer model')).toBeInTheDocument());
    await user.click(screen.getByLabelText('Printer model'));
    await waitFor(() => expect(shownUrl()).toContain('show=model%2Cfilename'));

    await user.click(screen.getByLabelText('Printer name'));
    await waitFor(() => expect(shownUrl()).toContain('show=printer%2Cmodel%2Cfilename'));
  });

  it('drops a field when its box is cleared', async () => {
    const user = userEvent.setup();
    render(<StreamOverlayBuilder />);

    await waitFor(() => expect(screen.getByLabelText('Layer count')).toBeInTheDocument());
    await user.click(screen.getByLabelText('Layer count'));

    await waitFor(() => expect(shownUrl()).not.toContain('layers'));
    expect(shownUrl()).toContain('progress');
  });

  it('emits camera=false when the camera feed is switched off', async () => {
    const user = userEvent.setup();
    render(<StreamOverlayBuilder />);

    await waitFor(() => expect(screen.getByLabelText('Camera feed')).toBeInTheDocument());
    await user.click(screen.getByLabelText('Camera feed'));

    await waitFor(() => expect(shownUrl()).toContain('camera=false'));
  });

  it('emits size and fps only when they differ from the defaults', async () => {
    const user = userEvent.setup();
    render(<StreamOverlayBuilder />);

    await waitFor(() => expect(screen.getByLabelText('Text size')).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText('Text size'), 'large');
    await waitFor(() => expect(shownUrl()).toContain('size=large'));

    await user.selectOptions(screen.getByLabelText('Text size'), 'medium');
    await waitFor(() => expect(shownUrl()).not.toContain('size='));
  });

  it('appends a token and warns that the URL is now a key', async () => {
    const user = userEvent.setup();
    render(<StreamOverlayBuilder />);

    await waitFor(() => expect(screen.getByLabelText(/token/i)).toBeInTheDocument());
    expect(screen.queryByText(/This URL contains a token/)).not.toBeInTheDocument();

    await user.type(screen.getByLabelText(/token/i), 'bblt_abc');

    await waitFor(() => expect(shownUrl()).toContain('token=bblt_abc'));
    expect(screen.getByText(/This URL contains a token/)).toBeInTheDocument();
  });

  it('opens no camera stream until the preview is asked for', async () => {
    const user = userEvent.setup();
    render(<StreamOverlayBuilder />);

    await waitFor(() => expect(screen.getByText('Show preview')).toBeInTheDocument());
    // An always-on preview would hold a subscriber on the printer's single
    // camera connection for as long as the settings tab stays open.
    expect(document.querySelector('iframe')).toBeNull();

    await user.click(screen.getByText('Show preview'));

    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull());
    expect(document.querySelector('iframe')?.getAttribute('src')).toContain('/overlay/1');
  });

  it('still builds a URL when the printer list cannot be loaded', async () => {
    server.use(http.get('/api/v1/printers', () => HttpResponse.json({ detail: 'nope' }, { status: 500 })));
    render(<StreamOverlayBuilder />);

    // Falls back to printer 1 rather than rendering /overlay/null — the number
    // is the one thing the user can fix by hand in the URL.
    await waitFor(() => expect(shownUrl()).toContain('/overlay/1'));
  });

  describe('progress colours', () => {
    it('emits no colour until custom colours are switched on', async () => {
      const user = userEvent.setup();
      render(<StreamOverlayBuilder />);

      await waitFor(() => expect(screen.getByLabelText('Custom progress colours')).toBeInTheDocument());
      expect(shownUrl()).not.toContain('progressFrom');
      expect(screen.queryByLabelText('Start colour')).not.toBeInTheDocument();

      await user.click(screen.getByLabelText('Custom progress colours'));
      await waitFor(() => expect(shownUrl()).toContain('progressFrom=00ae42&progressTo=7ee081'));

      fireEvent.change(screen.getByLabelText('Start colour'), { target: { value: '#ff8800' } });
      fireEvent.change(screen.getByLabelText('End colour'), { target: { value: '#0088ff' } });
      await waitFor(() => expect(shownUrl()).toContain('progressFrom=ff8800&progressTo=0088ff'));

      await user.click(screen.getByLabelText('Custom progress colours'));
      await waitFor(() => expect(shownUrl()).not.toContain('progressFrom'));
      expect(shownUrl()).not.toContain('progressTo');
    });

    // Dragging a colour picker changes the URL many times a second; a preview
    // keyed on the URL would open a camera stream per step.
    it('reloads an open preview once after a burst of colour changes', async () => {
      const user = userEvent.setup();
      render(<StreamOverlayBuilder />);

      await waitFor(() => expect(screen.getByLabelText('Custom progress colours')).toBeInTheDocument());
      await user.click(screen.getByLabelText('Custom progress colours'));
      await user.click(screen.getByText('Show preview'));
      await waitFor(() => expect(document.querySelector('iframe')?.getAttribute('src')).toContain('progressFrom=00ae42'));
      const first = document.querySelector('iframe');

      fireEvent.change(screen.getByLabelText('Start colour'), { target: { value: '#110000' } });
      fireEvent.change(screen.getByLabelText('Start colour'), { target: { value: '#220000' } });
      fireEvent.change(screen.getByLabelText('Start colour'), { target: { value: '#330000' } });
      // The URL to copy follows at once; the preview has not moved yet.
      expect(shownUrl()).toContain('progressFrom=330000');
      expect(document.querySelector('iframe')).toBe(first);
      expect(first?.getAttribute('src')).toContain('progressFrom=00ae42');

      await waitFor(() => expect(document.querySelector('iframe')?.getAttribute('src')).toContain('progressFrom=330000'));
      expect(document.querySelector('iframe')?.getAttribute('src')).not.toContain('110000');
    });
  });

  describe('design', () => {
    it('starts on Classic and says nothing about the design', async () => {
      render(<StreamOverlayBuilder />);

      await waitFor(() => expect(screen.getByLabelText('Design')).toBeInTheDocument());
      expect(screen.getByLabelText('Design')).toHaveValue('classic');
      expect(shownUrl()).not.toContain('design=');
      expect(shownUrl()).not.toContain('backgroundTransparency');
      // Transparency belongs to Version 2 only.
      expect(screen.queryByLabelText(/Background transparency/)).not.toBeInTheDocument();
    });

    it('emits design=2 for Version 2, and the transparency only above zero', async () => {
      const user = userEvent.setup();
      render(<StreamOverlayBuilder />);

      await waitFor(() => expect(screen.getByLabelText('Design')).toBeInTheDocument());
      await user.selectOptions(screen.getByLabelText('Design'), '2');
      await waitFor(() => expect(shownUrl()).toContain('design=2'));
      expect(shownUrl()).not.toContain('backgroundTransparency');

      fireEvent.change(screen.getByLabelText(/Background transparency/), { target: { value: '35' } });
      await waitFor(() => expect(shownUrl()).toContain('backgroundTransparency=35'));

      // Back on Classic the parameter would mean nothing: it is left out.
      await user.selectOptions(screen.getByLabelText('Design'), 'classic');
      await waitFor(() => expect(shownUrl()).not.toContain('design='));
      expect(shownUrl()).not.toContain('backgroundTransparency');
    });
  });
});
