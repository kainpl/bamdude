/**
 * The provider event list comes from the API, and nothing can fall out of it.
 *
 * The bug this replaces: the dialog kept its own list of eighteen `useState`
 * toggles while the backend grew to thirty-four flags. Six of the sixteen it
 * hid default to ON, so a new provider sent events its creator was never shown.
 * These tests pin the property that makes that impossible — every row the API
 * sends is rendered, including one this codebase has never heard of.
 */
import { describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import { render } from '../utils';
import { server } from '../mocks/server';
import { PROVIDER_EVENTS } from '../fixtures/providerEvents';
import { ProviderEventToggles } from '../../components/ProviderEventToggles';
import { humaniseFlag } from '../../components/providerEvents';

describe('ProviderEventToggles', () => {
  it('renders every flag the API returns', async () => {
    render(<ProviderEventToggles value={{}} onChange={() => undefined} />);

    await waitFor(() => expect(screen.getAllByRole('switch').length).toBe(PROVIDER_EVENTS.length));
  });

  it('renders a flag it has no label for, instead of dropping it', async () => {
    server.use(
      http.get('*/api/v1/notifications/events', () =>
        HttpResponse.json([
          ...PROVIDER_EVENTS,
          {
            flag: 'on_something_nobody_wrote_a_label_for',
            event_types: [],
            group: 'printer',
            severity: 'warning',
            default: false,
          },
        ]),
      ),
    );

    render(<ProviderEventToggles value={{}} onChange={() => undefined} />);

    expect(await screen.findByText('Something nobody wrote a label for')).toBeInTheDocument();
  });

  it('reports the flag and the new state when a switch is flipped', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ProviderEventToggles value={{ on_print_start: false }} onChange={onChange} />);

    const toggle = await screen.findByRole('switch', { name: 'Print Started' });
    await user.click(toggle);

    expect(onChange).toHaveBeenCalledWith('on_print_start', true);
  });

  it('renders an extra control under the flag it belongs to', async () => {
    render(
      <ProviderEventToggles
        value={{}}
        onChange={() => undefined}
        extras={{ on_print_progress: <span>duration floor</span> }}
      />,
    );

    expect(await screen.findByText('duration floor')).toBeInTheDocument();
  });

  it('says so when the list cannot be loaded, rather than showing an empty form', async () => {
    server.use(http.get('*/api/v1/notifications/events', () => HttpResponse.error()));

    render(<ProviderEventToggles value={{}} onChange={() => undefined} />);

    expect(await screen.findByText(/could not be loaded/i)).toBeInTheDocument();
    expect(screen.queryByRole('switch')).not.toBeInTheDocument();
  });
});

describe('humaniseFlag', () => {
  it('turns a flag into a readable label', () => {
    expect(humaniseFlag('on_queue_job_waiting')).toBe('Queue job waiting');
    expect(humaniseFlag('on_')).toBe('on_');
  });
});
