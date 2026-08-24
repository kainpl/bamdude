/**
 * Settings → Network → Cloud Link.
 *
 * Three questions, one per thing that can only be got wrong here:
 *
 *  - the status the panel shows is the status the backend sent (four separate
 *    fields collapse into one badge, so the collapse is worth pinning);
 *  - Unpair asks before it acts — it deletes a credential, and the panel is a
 *    click away from the switch that merely turns the link off;
 *  - a 502 from ``POST /pair`` says *portal refused or unreachable*, not
 *    anything about the network. The backend's ``network`` failure code covers
 *    a portal answering 500 and a proxy answering 502 as well as a dead
 *    socket, so a message that sends the user to check their router is wrong
 *    most of the time it is shown.
 *
 * Note the panel is deliberately NOT asserted against the server's ``detail``
 * string: that text is English, and the whole point of keying off the status
 * code is that the panel never renders it.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { CloudLinkSettings } from '../../components/settings/CloudLinkSettings';

const pairedStatus = {
  enabled: true,
  paired: true,
  connected: true,
  portal_url: 'https://portal.example.com',
  instance_id: 'inst-abc123',
  last_connected_at: '2026-08-24T10:00:00Z',
  last_error: null,
  revoked: false,
  published_printer_ids: [1],
};

const unpairedStatus = {
  ...pairedStatus,
  enabled: false,
  paired: false,
  connected: false,
  instance_id: null,
  last_connected_at: null,
  published_printer_ids: [],
};

const emptyAudit = { items: [], total: 0, page: 1, page_size: 20 };

/** Only the fields the panel reads. `GET /printers/` excludes archived rows but
 *  NOT maintenance-mode ones, which is the whole point of two of the tests
 *  below — so `is_active` varies here while `archived` stays false. */
const printer = (id: number, name: string, is_active = true) => ({
  id,
  name,
  model: 'X1C',
  is_active,
  archived: false,
});

function mockStatus(status: object, printers: object[] = []) {
  server.use(
    http.get('/api/v1/cloud-link/status', () => HttpResponse.json(status)),
    http.get('/api/v1/cloud-link/audit', () => HttpResponse.json(emptyAudit)),
    http.get('/api/v1/printers/', () => HttpResponse.json(printers)),
  );
}

describe('CloudLinkSettings', () => {
  beforeEach(() => {
    mockStatus(pairedStatus);
  });

  it('renders the status the backend reported', async () => {
    render(<CloudLinkSettings />);

    expect(await screen.findByText('Connected')).toBeInTheDocument();
    expect(screen.getByText('https://portal.example.com')).toBeInTheDocument();
    expect(screen.getByText('inst-abc123')).toBeInTheDocument();
  });

  it('shows the portal revocation as its own state, not as "offline"', async () => {
    mockStatus({ ...pairedStatus, connected: false, revoked: true });
    render(<CloudLinkSettings />);

    expect(await screen.findByText('Revoked by the portal')).toBeInTheDocument();
    expect(screen.queryByText('Offline')).not.toBeInTheDocument();
  });

  it('asks for confirmation before unpairing, and does not act on the ask', async () => {
    const user = userEvent.setup();
    let unpairCalls = 0;
    server.use(
      http.post('/api/v1/cloud-link/unpair', () => {
        unpairCalls += 1;
        return HttpResponse.json(unpairedStatus);
      }),
    );
    render(<CloudLinkSettings />);

    const unpair = await screen.findByRole('button', { name: 'Unpair' });
    // Nothing is being asked yet.
    expect(screen.queryByText('Unpair from the portal?')).not.toBeInTheDocument();

    await user.click(unpair);

    expect(await screen.findByText('Unpair from the portal?')).toBeInTheDocument();
    // Opening the dialog deletes nothing — the whole reason it exists.
    expect(unpairCalls).toBe(0);
  });

  it('does not offer a printer the backend would refuse', async () => {
    // `archived` and `is_active` are independent axes: `GET /printers/` drops
    // archived rows, but a Maintenance Mode printer comes back with
    // `is_active: false` — and the publish-set validator refuses it.
    mockStatus(pairedStatus, [printer(1, 'Alpha'), printer(2, 'Beta', false)]);
    render(<CloudLinkSettings />);

    expect(await screen.findByText('Alpha')).toBeInTheDocument();
    expect(screen.queryByText('Beta')).not.toBeInTheDocument();
    expect(screen.getByText('1 of 1 selected')).toBeInTheDocument();
  });

  it('never sends a saved id the picker cannot show', async () => {
    const user = userEvent.setup();
    let sent: unknown = null;
    // 2 was published and has since been parked in Maintenance Mode; 99 was
    // published and has since been archived away entirely. Both still sit in
    // `published_printer_ids`, and both would 422 the whole save.
    mockStatus({ ...pairedStatus, published_printer_ids: [1, 2, 99] }, [
      printer(1, 'Alpha'),
      printer(2, 'Beta', false),
    ]);
    server.use(
      http.put('/api/v1/cloud-link/publish-set', async ({ request }) => {
        sent = await request.json();
        return HttpResponse.json({ ...pairedStatus, published_printer_ids: [1] });
      }),
    );
    render(<CloudLinkSettings />);

    // The count agrees with what is on screen, not with what is stored.
    expect(await screen.findByText('1 of 1 selected')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Save published set' }));

    await waitFor(() => expect(sent).toEqual({ printer_ids: [1] }));
  });

  it('reports a 502 from the portal as "refused or unreachable"', async () => {
    const user = userEvent.setup();
    mockStatus(unpairedStatus);
    server.use(
      http.post('/api/v1/cloud-link/pair', () =>
        HttpResponse.json(
          // A pydantic-shaped `detail` — the OTHER thing FastAPI can put
          // there. Proof in one go that the panel neither renders the server's
          // text nor trips over its shape.
          { detail: [{ loc: ['body'], msg: 'portal said no', type: 'value_error' }] },
          { status: 502 },
        ),
      ),
    );
    render(<CloudLinkSettings />);

    const code = await screen.findByLabelText('Pairing code');
    await user.type(code, 'ABCD-EFGH');
    await user.click(screen.getByRole('button', { name: 'Connect' }));

    await waitFor(() => {
      expect(
        screen.getByText(/refused the pairing or is unreachable/i),
      ).toBeInTheDocument();
    });
    expect(screen.queryByText(/portal said no/)).not.toBeInTheDocument();
  });

  it('names the portal URL that is now saved after a failed pair', async () => {
    const user = userEvent.setup();
    // The backend validates and stores `portal_url` BEFORE redeeming the code,
    // and deliberately leaves the new one standing when pairing then fails —
    // so the second read is the one that tells the user where a retry goes.
    let reads = 0;
    server.use(
      http.get('/api/v1/cloud-link/status', () => {
        reads += 1;
        return HttpResponse.json({
          ...unpairedStatus,
          portal_url: reads === 1 ? 'https://old.example.com' : 'https://new.example.com',
        });
      }),
      http.get('/api/v1/cloud-link/audit', () => HttpResponse.json(emptyAudit)),
      http.get('/api/v1/printers/', () => HttpResponse.json([])),
      http.post('/api/v1/cloud-link/pair', () =>
        HttpResponse.json({ detail: 'nope' }, { status: 502 }),
      ),
    );
    render(<CloudLinkSettings />);

    await user.type(await screen.findByLabelText('Pairing code'), 'ABCD-EFGH');
    await user.click(screen.getByRole('button', { name: /Advanced/ }));
    await user.type(screen.getByLabelText('Portal URL'), 'https://new.example.com');
    await user.click(screen.getByRole('button', { name: 'Connect' }));

    await waitFor(() => {
      expect(screen.getByText(/now saved as https:\/\/new\.example\.com/)).toBeInTheDocument();
    });
  });

  it('reports an unknown pairing code as a code problem, not a portal problem', async () => {
    const user = userEvent.setup();
    mockStatus(unpairedStatus);
    server.use(
      http.post('/api/v1/cloud-link/pair', () =>
        HttpResponse.json({ detail: 'nope' }, { status: 404 }),
      ),
    );
    render(<CloudLinkSettings />);

    const code = await screen.findByLabelText('Pairing code');
    await user.type(code, 'ABCD-EFGH');
    await user.click(screen.getByRole('button', { name: 'Connect' }));

    await waitFor(() => {
      expect(screen.getByText(/does not know that pairing code/i)).toBeInTheDocument();
    });
    expect(screen.queryByText('nope')).not.toBeInTheDocument();
  });
});
