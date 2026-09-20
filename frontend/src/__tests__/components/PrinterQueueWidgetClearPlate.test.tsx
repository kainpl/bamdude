/**
 * Tests for the PrinterQueueWidget clear plate behavior.
 *
 * When the printer is in FINISH or FAILED state and has pending queue items,
 * the widget shows a "Clear Plate & Start Next" button instead of the
 * passive queue link. After clicking, it shows a confirmation state.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { PrinterQueueWidget } from '../../components/PrinterQueueWidget';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const mockQueueItems = [
  {
    id: 1,
    printer_id: 1,
    archive_id: 1,
    position: 1,
    status: 'pending',
    archive_name: 'First Print',
    printer_name: 'X1 Carbon',
    print_time_seconds: 3600,
    scheduled_time: null,
  },
  {
    id: 2,
    printer_id: 1,
    archive_id: 2,
    position: 2,
    status: 'pending',
    archive_name: 'Second Print',
    printer_name: 'X1 Carbon',
    print_time_seconds: 7200,
    scheduled_time: null,
  },
];

describe('PrinterQueueWidget - Clear Plate', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/queue/', ({ request }) => {
        const url = new URL(request.url);
        const printerId = url.searchParams.get('queue_id');
        if (printerId === '1') {
          return HttpResponse.json(mockQueueItems);
        }
        return HttpResponse.json([]);
      }),
      http.post('/api/v1/printers/:id/clear-plate', () => {
        return HttpResponse.json({ success: true, message: 'Plate cleared' });
      })
    );
  });

  describe('clear plate button visibility', () => {
    it('shows clear plate button when printer state is FINISH and gate is armed', async () => {
      render(<PrinterQueueWidget printerId={1} printerState="FINISH" awaitingPlateClear={true} />);

      await waitFor(() => {
        expect(screen.getByText('Clear plate')).toBeInTheDocument();
      });
    });

    it('shows clear plate button when printer state is FAILED and gate is armed', async () => {
      render(<PrinterQueueWidget printerId={1} printerState="FAILED" awaitingPlateClear={true} />);

      await waitFor(() => {
        expect(screen.getByText('Clear plate')).toBeInTheDocument();
      });
    });

    it('offers BOTH answers, not just clearing', async () => {
      // A full plate has two answers now: print it again, or clear and move on.
      // Repeat re-arms the very row that just finished — see the backend's
      // services/plate_hold — which is why it sits beside Clear rather than on
      // the queue row itself.
      render(<PrinterQueueWidget printerId={1} printerState="FINISH" awaitingPlateClear={true} />);

      await waitFor(() => {
        expect(screen.getByText('Repeat print')).toBeInTheDocument();
      });
      expect(screen.getByText('Clear plate')).toBeInTheDocument();
    });

    it('shows passive link when printer state is IDLE', async () => {
      render(<PrinterQueueWidget printerId={1} printerState="IDLE" />);

      await waitFor(() => {
        const link = screen.getByRole('link');
        expect(link).toHaveAttribute('href', '/queue');
      });

      expect(screen.queryByText('Clear plate')).not.toBeInTheDocument();
    });

    it('shows passive link when printer state is RUNNING', async () => {
      render(<PrinterQueueWidget printerId={1} printerState="RUNNING" />);

      await waitFor(() => {
        const link = screen.getByRole('link');
        expect(link).toHaveAttribute('href', '/queue');
      });
    });

    it('shows passive link when printerState is not provided', async () => {
      render(<PrinterQueueWidget printerId={1} />);

      await waitFor(() => {
        const link = screen.getByRole('link');
        expect(link).toHaveAttribute('href', '/queue');
      });
    });

    it('shows passive link when FINISH but awaitingPlateClear is false', async () => {
      render(<PrinterQueueWidget printerId={1} printerState="FINISH" awaitingPlateClear={false} />);

      await waitFor(() => {
        const link = screen.getByRole('link');
        expect(link).toHaveAttribute('href', '/queue');
      });

      expect(screen.queryByText('Clear plate')).not.toBeInTheDocument();
    });

    it('shows passive link when FAILED but awaitingPlateClear is false', async () => {
      render(<PrinterQueueWidget printerId={1} printerState="FAILED" awaitingPlateClear={false} />);

      await waitFor(() => {
        const link = screen.getByRole('link');
        expect(link).toHaveAttribute('href', '/queue');
      });

      expect(screen.queryByText('Clear plate')).not.toBeInTheDocument();
    });

    it('shows passive link when FINISH but requirePlateClear is false', async () => {
      render(<PrinterQueueWidget printerId={1} printerState="FINISH" requirePlateClear={false} />);

      await waitFor(() => {
        const link = screen.getByRole('link');
        expect(link).toHaveAttribute('href', '/queue');
      });

      expect(screen.queryByText('Clear plate')).not.toBeInTheDocument();
    });
  });

  describe('clear plate button shows queue info', () => {
    it('shows next item name in clear plate mode', async () => {
      render(<PrinterQueueWidget printerId={1} printerState="FINISH" awaitingPlateClear={true} />);

      await waitFor(() => {
        expect(screen.getByText('First Print')).toBeInTheDocument();
      });
    });

    it('shows additional items badge in clear plate mode', async () => {
      render(<PrinterQueueWidget printerId={1} printerState="FINISH" awaitingPlateClear={true} />);

      await waitFor(() => {
        expect(screen.getByText('+1')).toBeInTheDocument();
      });
    });
  });

  describe('clear plate action', () => {
    it('shows confirmation state after clicking clear plate', async () => {
      const user = userEvent.setup();
      render(<PrinterQueueWidget printerId={1} printerState="FINISH" awaitingPlateClear={true} />);

      await waitFor(() => {
        expect(screen.getByText('Clear plate')).toBeInTheDocument();
      });

      await user.click(screen.getByText('Clear plate'));

      await waitFor(() => {
        // Both the widget confirmation and the toast show this text
        const elements = screen.getAllByText('Plate cleared - ready for next print');
        expect(elements.length).toBeGreaterThanOrEqual(1);
      });
    });

    // Regression test for upstream #912: mutation state must reset between print cycles
    it('resets mutation state when printer leaves FINISH so the next cycle is clickable', async () => {
      const { rerender } = render(<PrinterQueueWidget printerId={1} printerState="FINISH" awaitingPlateClear={true} />);
      const user = userEvent.setup();

      // First cycle: click button → confirmation rendered
      await waitFor(() => {
        expect(screen.getByText('Clear plate')).toBeInTheDocument();
      });
      await user.click(screen.getByText('Clear plate'));
      await waitFor(() => {
        const confirmations = screen.getAllByText('Plate cleared - ready for next print');
        expect(confirmations.length).toBeGreaterThanOrEqual(1);
      });

      // Printer transitions to RUNNING (next print starts) - then back to FINISH.
      // The useEffect must have called mutation.reset(), so the button is rendered
      // again instead of the sticky "Plate Ready" confirmation.
      rerender(<PrinterQueueWidget printerId={1} printerState="RUNNING" />);
      rerender(<PrinterQueueWidget printerId={1} printerState="FINISH" awaitingPlateClear={true} />);

      await waitFor(() => {
        expect(screen.getByText('Clear plate')).toBeInTheDocument();
      });
    });

    it('shows error toast on API failure', async () => {
      server.use(
        http.post('/api/v1/printers/:id/clear-plate', () => {
          return HttpResponse.json(
            { detail: 'Printer not connected' },
            { status: 400 }
          );
        })
      );

      const user = userEvent.setup();
      render(<PrinterQueueWidget printerId={1} printerState="FAILED" awaitingPlateClear={true} />);

      await waitFor(() => {
        expect(screen.getByText('Clear plate')).toBeInTheDocument();
      });

      await user.click(screen.getByText('Clear plate'));

      // Button should remain visible (not transition to success state)
      await waitFor(() => {
        expect(screen.getByText('Clear plate')).toBeInTheDocument();
      });
    });
  });

  describe('empty queue', () => {
    it('renders nothing in FINISH state with no queue items', async () => {
      const { container } = render(<PrinterQueueWidget printerId={999} printerState="FINISH" awaitingPlateClear={true} />);

      await waitFor(() => {
        expect(container.querySelector('button')).not.toBeInTheDocument();
      });
    });
  });

  describe('filament compatibility filtering', () => {
    const petgQueueItems = [
      {
        id: 10,
        printer_id: 1,
        archive_id: 10,
        position: 1,
        status: 'pending',
        archive_name: 'PETG Print',
        printer_name: 'H2S',
        print_time_seconds: 3600,
        scheduled_time: null,
        required_filament_types: ['PETG'],
      },
    ];

    it('shows widget even when queue item requires filament not loaded on printer (per-printer queues)', async () => {
      server.use(
        http.get('/api/v1/queue/', () => HttpResponse.json(petgQueueItems))
      );

      render(
        <PrinterQueueWidget
          printerId={1}
          printerState="FINISH"
          awaitingPlateClear={true}
        />
      );

      // Per-printer queues no longer filter by filament compatibility
      await waitFor(() => {
        expect(screen.getByText('PETG Print')).toBeInTheDocument();
      });
    });

    it('shows widget when queue item required filaments match loaded', async () => {
      server.use(
        http.get('/api/v1/queue/', () => HttpResponse.json(petgQueueItems))
      );

      render(
        <PrinterQueueWidget
          printerId={1}
          printerState="FINISH"
          awaitingPlateClear={true}
        />
      );

      await waitFor(() => {
        expect(screen.getByText('PETG Print')).toBeInTheDocument();
        expect(screen.getByText('Clear plate')).toBeInTheDocument();
      });
    });

    it('shows widget when queue item has no required_filament_types', async () => {
      // Default mockQueueItems have no required_filament_types
      render(
        <PrinterQueueWidget
          printerId={1}
          printerState="FINISH"
          awaitingPlateClear={true}
        />
      );

      await waitFor(() => {
        expect(screen.getByText('First Print')).toBeInTheDocument();
        expect(screen.getByText('Clear plate')).toBeInTheDocument();
      });
    });

    it('shows widget when loadedFilamentTypes prop is not provided', async () => {
      server.use(
        http.get('/api/v1/queue/', () => HttpResponse.json(petgQueueItems))
      );

      render(
        <PrinterQueueWidget printerId={1} printerState="FINISH" awaitingPlateClear={true} />
      );

      await waitFor(() => {
        expect(screen.getByText('PETG Print')).toBeInTheDocument();
        expect(screen.getByText('Clear plate')).toBeInTheDocument();
      });
    });

    it('shows first item regardless of filament compatibility (per-printer queues)', async () => {
      const mixedQueue = [
        {
          id: 10,
          printer_id: 1,
          archive_id: 10,
          position: 1,
          status: 'pending',
          archive_name: 'PETG Print',
          printer_name: 'H2S',
          print_time_seconds: 3600,
          scheduled_time: null,
          required_filament_types: ['PETG'],
        },
        {
          id: 11,
          printer_id: 1,
          archive_id: 11,
          position: 2,
          status: 'pending',
          archive_name: 'PLA Print',
          printer_name: 'H2S',
          print_time_seconds: 1800,
          scheduled_time: null,
          required_filament_types: ['PLA'],
        },
      ];

      server.use(
        http.get('/api/v1/queue/', () => HttpResponse.json(mixedQueue))
      );

      render(
        <PrinterQueueWidget
          printerId={1}
          printerState="FINISH"
          awaitingPlateClear={true}
        />
      );

      // Per-printer queues show items in order without filament filtering
      await waitFor(() => {
        expect(screen.getByText('PETG Print')).toBeInTheDocument();
      });
    });

    it('matches filament types case-insensitively', async () => {
      const lowercaseQueue = [
        {
          id: 10,
          printer_id: 1,
          archive_id: 10,
          position: 1,
          status: 'pending',
          archive_name: 'Petg Print',
          printer_name: 'H2S',
          print_time_seconds: 3600,
          scheduled_time: null,
          required_filament_types: ['petg'],
        },
      ];

      server.use(
        http.get('/api/v1/queue/', () => HttpResponse.json(lowercaseQueue))
      );

      render(
        <PrinterQueueWidget
          printerId={1}
          printerState="FINISH"
          awaitingPlateClear={true}
        />
      );

      await waitFor(() => {
        expect(screen.getByText('Petg Print')).toBeInTheDocument();
        expect(screen.getByText('Clear plate')).toBeInTheDocument();
      });
    });
  });

  describe('filament override color filtering', () => {
    const whitePetgOverrideItem = [
      {
        id: 20,
        printer_id: null,
        archive_id: 20,
        position: 1,
        status: 'pending',
        archive_name: 'White PETG Print',
        printer_name: null,
        print_time_seconds: 3600,
        scheduled_time: null,
        required_filament_types: ['PETG'],
        filament_overrides: [{ slot_id: 1, type: 'PETG', color: '#FFFFFF' }],
      },
    ];

    it('shows widget even when override color does not match loaded filaments (per-printer queues)', async () => {
      server.use(
        http.get('/api/v1/queue/', () => HttpResponse.json(whitePetgOverrideItem))
      );

      render(
        <PrinterQueueWidget
          printerId={1}
          printerState="FINISH"
          awaitingPlateClear={true}
        />
      );

      // Per-printer queues no longer filter by filament color compatibility
      await waitFor(() => {
        expect(screen.getByText('White PETG Print')).toBeInTheDocument();
      });
    });

    it('shows widget when override color matches loaded filaments', async () => {
      server.use(
        http.get('/api/v1/queue/', () => HttpResponse.json(whitePetgOverrideItem))
      );

      render(
        <PrinterQueueWidget
          printerId={1}
          printerState="FINISH"
          awaitingPlateClear={true}
        />
      );

      await waitFor(() => {
        expect(screen.getByText('White PETG Print')).toBeInTheDocument();
        expect(screen.getByText('Clear plate')).toBeInTheDocument();
      });
    });

    it('normalizes override color format (strips # and lowercases)', async () => {
      const upperCaseColorItem = [
        {
          id: 21,
          printer_id: null,
          archive_id: 21,
          position: 1,
          status: 'pending',
          archive_name: 'Red PLA Print',
          printer_name: null,
          print_time_seconds: 3600,
          scheduled_time: null,
          required_filament_types: ['PLA'],
          filament_overrides: [{ slot_id: 1, type: 'PLA', color: '#FF0000' }],
        },
      ];

      server.use(
        http.get('/api/v1/queue/', () => HttpResponse.json(upperCaseColorItem))
      );

      render(
        <PrinterQueueWidget
          printerId={1}
          printerState="FINISH"
          awaitingPlateClear={true}
        />
      );

      await waitFor(() => {
        expect(screen.getByText('Red PLA Print')).toBeInTheDocument();
      });
    });

    it('shows widget when no loadedFilaments prop is provided (no color filtering)', async () => {
      server.use(
        http.get('/api/v1/queue/', () => HttpResponse.json(whitePetgOverrideItem))
      );

      render(
        <PrinterQueueWidget
          printerId={1}
          printerState="FINISH"
          awaitingPlateClear={true}
        />
      );

      await waitFor(() => {
        expect(screen.getByText('White PETG Print')).toBeInTheDocument();
      });
    });

    it('shows widget when queue item has no filament overrides', async () => {
      // Default mockQueueItems have no filament_overrides
      render(
        <PrinterQueueWidget
          printerId={1}
          printerState="FINISH"
          awaitingPlateClear={true}
        />
      );

      await waitFor(() => {
        expect(screen.getByText('First Print')).toBeInTheDocument();
      });
    });

    it('matches any override when multiple overrides exist', async () => {
      const multiOverrideItem = [
        {
          id: 22,
          printer_id: null,
          archive_id: 22,
          position: 1,
          status: 'pending',
          archive_name: 'Multi Color Print',
          printer_name: null,
          print_time_seconds: 3600,
          scheduled_time: null,
          required_filament_types: ['PLA'],
          filament_overrides: [
            { slot_id: 1, type: 'PLA', color: '#FF0000' },
            { slot_id: 2, type: 'PLA', color: '#00FF00' },
          ],
        },
      ];

      server.use(
        http.get('/api/v1/queue/', () => HttpResponse.json(multiOverrideItem))
      );

      // Printer has green PLA but not red - should still match (at least one override)
      render(
        <PrinterQueueWidget
          printerId={1}
          printerState="FINISH"
          awaitingPlateClear={true}
        />
      );

      await waitFor(() => {
        expect(screen.getByText('Multi Color Print')).toBeInTheDocument();
      });
    });
  });

  describe('staged (manual_start) items', () => {
    const stagedItems = [
      { id: 10, printer_id: 1, archive_id: 1, position: 1, status: 'pending', archive_name: 'Staged Print 1', manual_start: true, scheduled_time: null },
      { id: 11, printer_id: 1, archive_id: 2, position: 2, status: 'pending', archive_name: 'Staged Print 2', manual_start: true, scheduled_time: null },
    ];

    it('does not show clear plate button when all items are staged', async () => {
      server.use(
        http.get('/api/v1/queue/', () => HttpResponse.json(stagedItems)),
      );

      render(<PrinterQueueWidget printerId={1} printerState="FINISH" awaitingPlateClear={true} />);

      // Should show the passive link (not the clear plate button)
      await waitFor(() => {
        expect(screen.getByText('Staged Print 1')).toBeInTheDocument();
      });
      expect(screen.queryByText('Clear plate')).not.toBeInTheDocument();
    });

    it('shows clear plate button when mix of staged and auto-dispatch items', async () => {
      const mixedItems = [
        { id: 10, printer_id: 1, archive_id: 1, position: 1, status: 'pending', archive_name: 'Staged Print', manual_start: true, scheduled_time: null },
        { id: 11, printer_id: 1, archive_id: 2, position: 2, status: 'pending', archive_name: 'Auto Print', manual_start: false, scheduled_time: null },
      ];
      server.use(
        http.get('/api/v1/queue/', () => HttpResponse.json(mixedItems)),
      );

      render(<PrinterQueueWidget printerId={1} printerState="FINISH" awaitingPlateClear={true} />);

      await waitFor(() => {
        expect(screen.getByText('Clear plate')).toBeInTheDocument();
      });
    });
  });

  describe('defects beside the answer', () => {
    it('sends the touched defect counters with Clear plate, and nothing when untouched', async () => {
      let clearBody: unknown = 'unset';
      server.use(
        http.get('/api/v1/printers/:id/waiting-print', () =>
          HttpResponse.json({
            archive_id: 9, print_name: 'Done', status: 'completed', quantity: 6, defective_count: 0,
            parts: [
              { id: 21, name: 'lid', name_key: 'lid', quantity: 2, defective: 0 },
              { id: 22, name: 'base', name_key: 'base', quantity: 4, defective: 0 },
            ],
          }),
        ),
        http.post('/api/v1/printers/:id/clear-plate', async ({ request }) => {
          const text = await request.text();
          clearBody = text === '' ? null : JSON.parse(text);
          return HttpResponse.json({ success: true, message: 'Plate cleared' });
        }),
      );
      const user = userEvent.setup();
      render(<PrinterQueueWidget printerId={1} printerState="FINISH" awaitingPlateClear={true} />);

      await user.click(await screen.findByTestId('plate-defects-toggle'));
      const lid = (await screen.findByTestId('part-defective-21')) as HTMLInputElement;
      await user.clear(lid);
      await user.type(lid, '1');
      await user.click(screen.getByText('Clear plate'));

      await waitFor(() =>
        expect(clearBody).toEqual({ defects: { parts: [{ id: 21, defective: 1 }, { id: 22, defective: 0 }] } }),
      );
    });

    it('reports a refused shelf correction after the success toast', async () => {
      // Reported where a refusal can happen: the print on the plate is usually
      // filed under no order, so its defects correct a free-stock credit and
      // parts already spent cannot come back off the shelf.
      server.use(
        http.get('/api/v1/printers/:id/waiting-print', () =>
          HttpResponse.json({
            archive_id: 9, print_name: 'Done', status: 'completed', quantity: 2, defective_count: 0,
            parts: [{ id: 21, name: 'lid', name_key: 'lid', quantity: 2, defective: 0 }],
          }),
        ),
        http.post('/api/v1/printers/:id/clear-plate', () =>
          HttpResponse.json({ success: true, message: 'Plate cleared', ledger_refused_parts: 2 }),
        ),
      );
      const user = userEvent.setup();
      render(<PrinterQueueWidget printerId={1} printerState="FINISH" awaitingPlateClear={true} />);

      await user.click(await screen.findByTestId('plate-defects-toggle'));
      const lid = (await screen.findByTestId('part-defective-21')) as HTMLInputElement;
      await user.clear(lid);
      await user.type(lid, '1');
      await user.click(screen.getByText('Clear plate'));

      await waitFor(() =>
        expect(
          screen.getByText(
            'The shelf could not be corrected for 2 parts — they were already spent; fix them by hand on the product page',
          ),
        ).toBeInTheDocument(),
      );
    });

    it('says nothing about the shelf when nothing was refused', async () => {
      server.use(
        http.get('/api/v1/printers/:id/waiting-print', () =>
          HttpResponse.json({
            archive_id: 9, print_name: 'Done', status: 'completed', quantity: 2, defective_count: 0,
            parts: [{ id: 21, name: 'lid', name_key: 'lid', quantity: 2, defective: 0 }],
          }),
        ),
        http.post('/api/v1/printers/:id/clear-plate', () =>
          HttpResponse.json({ success: true, message: 'Plate cleared', ledger_refused_parts: 0 }),
        ),
      );
      const user = userEvent.setup();
      render(<PrinterQueueWidget printerId={1} printerState="FINISH" awaitingPlateClear={true} />);

      await user.click(await screen.findByText('Clear plate'));
      await waitFor(() =>
        expect(screen.getAllByText('Plate cleared - ready for next print').length).toBeGreaterThanOrEqual(1),
      );
      expect(screen.queryByText(/could not be corrected/)).not.toBeInTheDocument();
    });

    it('the toggle label follows the typed counters, never a stale flat total', async () => {
      // `sum || defectFlat` fell through to the server-seeded flat count the
      // moment every counter read 0, so the toggle kept advertising the old total.
      server.use(
        http.get('/api/v1/printers/:id/waiting-print', () =>
          HttpResponse.json({
            archive_id: 9, print_name: 'Done', status: 'completed', quantity: 2, defective_count: 1,
            parts: [{ id: 21, name: 'lid', name_key: 'lid', quantity: 2, defective: 1 }],
          }),
        ),
      );
      const user = userEvent.setup();
      render(<PrinterQueueWidget printerId={1} printerState="FINISH" awaitingPlateClear={true} />);

      const toggle = await screen.findByTestId('plate-defects-toggle');
      await waitFor(() => expect(toggle).toHaveTextContent('Defects in this print: 1'));

      await user.click(toggle);
      const lid = (await screen.findByTestId('part-defective-21')) as HTMLInputElement;
      await user.clear(lid);

      await waitFor(() => expect(toggle).toHaveTextContent('Defects in this print'));
      expect(toggle).not.toHaveTextContent('Defects in this print: 1');
    });

    it('does not ask for the waiting print where its counters cannot be shown', async () => {
      // The counters live inside the `needsClearPlate` block, which also needs a
      // non-empty auto queue; a gate armed over a staged-only queue used to issue
      // one GET per card that nobody ever read.
      let asked = 0;
      server.use(
        http.get('/api/v1/queue/', () =>
          HttpResponse.json([
            { id: 30, printer_id: 1, archive_id: 1, position: 1, status: 'pending', archive_name: 'Staged', manual_start: true, scheduled_time: null },
          ]),
        ),
        http.get('/api/v1/printers/:id/waiting-print', () => {
          asked += 1;
          return HttpResponse.json({ archive_id: 9, print_name: 'Done', status: 'completed', quantity: 1, defective_count: 0, parts: [] });
        }),
      );
      render(<PrinterQueueWidget printerId={1} printerState="FINISH" awaitingPlateClear={true} />);

      await waitFor(() => expect(screen.getByText('Staged')).toBeInTheDocument());
      expect(asked).toBe(0);
    });

    it('re-reads the waiting print when the answer was refused', async () => {
      // A refused answer rolls its defects back on the server (the write and the
      // answer share one transaction), so the card must stop showing what it typed.
      let asked = 0;
      server.use(
        http.get('/api/v1/printers/:id/waiting-print', () => {
          asked += 1;
          return HttpResponse.json({
            archive_id: 9, print_name: 'Done', status: 'completed', quantity: 2, defective_count: 0,
            parts: [{ id: 21, name: 'lid', name_key: 'lid', quantity: 2, defective: 0 }],
          });
        }),
        http.post('/api/v1/printers/:id/repeat-print', () =>
          HttpResponse.json({ detail: 'This print has no file to send again' }, { status: 409 }),
        ),
      );
      const user = userEvent.setup();
      render(<PrinterQueueWidget printerId={1} printerState="FINISH" awaitingPlateClear={true} />);

      await waitFor(() => expect(asked).toBe(1));
      await user.click(await screen.findByText('Repeat print'));

      await waitFor(() => expect(asked).toBeGreaterThan(1));
    });

    it('clears without a body when no counter was touched', async () => {
      let clearBody: unknown = 'unset';
      server.use(
        http.get('/api/v1/printers/:id/waiting-print', () =>
          HttpResponse.json({ archive_id: 9, print_name: 'Done', status: 'completed', quantity: 6, defective_count: 0, parts: [] }),
        ),
        http.post('/api/v1/printers/:id/clear-plate', async ({ request }) => {
          const text = await request.text();
          clearBody = text === '' ? null : JSON.parse(text);
          return HttpResponse.json({ success: true, message: 'Plate cleared' });
        }),
      );
      const user = userEvent.setup();
      render(<PrinterQueueWidget printerId={1} printerState="FINISH" awaitingPlateClear={true} />);
      await user.click(await screen.findByText('Clear plate'));
      await waitFor(() => expect(clearBody).toBeNull());
    });
  });
});
