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

/** How many times the picker has asked for the printer list — the only way to
 *  know a background refetch has actually been attempted. */
let printersFetches = 0;

function mockStatus(status: object, printers: object[] = []) {
  server.use(
    http.get('/api/v1/cloud-link/status', () => HttpResponse.json(status)),
    http.get('/api/v1/cloud-link/audit', () => HttpResponse.json(emptyAudit)),
    http.get('/api/v1/printers/', () => {
      printersFetches += 1;
      return HttpResponse.json(printers);
    }),
  );
}

describe('CloudLinkSettings', () => {
  beforeEach(() => {
    printersFetches = 0;
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

  it('says out loud that it dropped published printers it cannot show', async () => {
    // Silently pruning is right; silently pruning *without saying so* is not.
    // "1 of 1 selected" beside a stored set of three is otherwise the only clue
    // that two machines are about to stop being published on the next save.
    mockStatus({ ...pairedStatus, published_printer_ids: [1, 2, 99] }, [
      printer(1, 'Alpha'),
      printer(2, 'Beta', false),
    ]);
    render(<CloudLinkSettings />);

    expect(
      await screen.findByText(
        '2 published printers are no longer available and will be removed when you save.',
      ),
    ).toBeInTheDocument();
  });

  it('keeps the prune while a background refetch of the printers is failing', async () => {
    // TanStack drops `isSuccess` to false for the length of a failed background
    // refetch even though the cached list is still there and still rendered. A
    // prune gated on `isSuccess` therefore un-prunes inside that window: the
    // dead ids walk back into the selection under a picker that has not
    // changed, and a save landing then is a 422 on the whole set.
    mockStatus({ ...pairedStatus, published_printer_ids: [1, 99] }, [printer(1, 'Alpha')]);
    render(<CloudLinkSettings />);

    expect(await screen.findByText('1 of 1 selected')).toBeInTheDocument();

    server.use(
      http.get('/api/v1/printers/', () => {
        printersFetches += 1;
        return new HttpResponse(null, { status: 500 });
      }),
    );
    // What TanStack's focus manager listens on — a tab coming back to the
    // foreground is exactly when this refetch happens for real.
    window.dispatchEvent(new Event('visibilitychange'));

    await waitFor(() => expect(printersFetches).toBeGreaterThan(1));
    // Still pruned, still counted against what is on screen.
    expect(screen.getByText('1 of 1 selected')).toBeInTheDocument();
    expect(screen.getByText(/no longer available/)).toBeInTheDocument();
  });

  it('keeps quiet when every published printer is still on screen', async () => {
    mockStatus(pairedStatus, [printer(1, 'Alpha')]);
    render(<CloudLinkSettings />);

    expect(await screen.findByText('Alpha')).toBeInTheDocument();
    expect(screen.queryByText(/no longer available/)).not.toBeInTheDocument();
  });

  it('stops the notice once the selection is the user\'s own', async () => {
    const user = userEvent.setup();
    mockStatus({ ...pairedStatus, published_printer_ids: [1, 99] }, [printer(1, 'Alpha')]);
    render(<CloudLinkSettings />);

    expect(await screen.findByText(/no longer available/)).toBeInTheDocument();

    await user.click(screen.getByText('Alpha'));

    // From the first tick the set on screen is a draft the user built. Still
    // blaming the difference on a vanished printer would be describing a
    // change they made themselves.
    await waitFor(() =>
      expect(screen.queryByText(/no longer available/)).not.toBeInTheDocument(),
    );
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
