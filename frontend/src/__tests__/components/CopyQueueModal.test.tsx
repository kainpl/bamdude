/**
 * Copying one printer's queue onto other printers of the same model.
 *
 * The two rules worth pinning are the ones a farm would notice going wrong:
 * only same-model machines are offered (nothing here can re-slice), and the
 * plate travels with the item — a copy that forgot which plate was queued is
 * not a copy. Plus the dialog's own contract: items start ticked, printers do
 * not, and neither side can be empty when Copy is pressed.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { I18nextProvider } from 'react-i18next';
import i18n from '../../i18n';
import { CopyQueueModal } from '../../components/CopyQueueModal';
import { copyableItems, copyTargets, copyableCurrentPrint, withCurrentPrint } from '../../lib/copyQueue';
import { api, type PrinterQueue, type PrinterStatus, type PrintQueueItem } from '../../api/client';

const item = (over: Partial<PrintQueueItem> = {}): PrintQueueItem =>
  ({
    id: 1,
    queue_id: 1,
    archive_id: null,
    library_file_id: 10,
    library_file_name: 'bracket.gcode.3mf',
    library_file_thumbnail: null,
    archive_name: null,
    archive_thumbnail: null,
    plate_id: null,
    project_id: null,
    project_line_id: null,
    project_name: null,
    position: 1,
    status: 'pending',
    ...over,
  }) as PrintQueueItem;

const queue = (over: Partial<PrinterQueue> = {}): PrinterQueue =>
  ({
    id: 1,
    printer_id: 1,
    printer_name: 'P1S-A',
    printer_model: 'P1S',
    printer_location: null,
    status: 'idle',
    is_paused: false,
    pending_count: 0,
    completed_count: 0,
    failed_count: 0,
    cancelled_count: 0,
    skipped_count: 0,
    total_count: 0,
    ...over,
  }) as PrinterQueue;

const SOURCE = queue({ id: 1, printer_id: 1, printer_name: 'P1S-A', printer_model: 'P1S' });

const status = (over: Partial<PrinterStatus> = {}): PrinterStatus =>
  ({ connected: true, progress: 0, current_archive_id: null, current_plate_id: null, ...over }) as PrinterStatus;

function renderModal(items: PrintQueueItem[], droppedCount = 0) {
  const onConfirm = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={i18n}>
        <CopyQueueModal
          source={SOURCE}
          items={copyableItems(items)}
          droppedCount={droppedCount}
          onCancel={vi.fn()}
          onConfirm={onConfirm}
        />
      </I18nextProvider>
    </QueryClientProvider>,
  );
  return { onConfirm };
}

describe('what can be copied', () => {
  it('carries the plate with the item — the same file has the same plates', () => {
    expect(copyableItems([item({ plate_id: 3 })])[0].file.plateId).toBe(3);
  });

  it('leaves out an item backed by no file at all', () => {
    expect(copyableItems([item({ library_file_id: null, archive_id: null })])).toEqual([]);
  });

  it('prefers the library file when an item has both', () => {
    const [only] = copyableItems([item({ library_file_id: 10, archive_id: 99 })]);

    expect(only.file).toMatchObject({ id: 10, source: 'library' });
  });

  it('falls back to the archive, which is what an external print has', () => {
    const [only] = copyableItems([
      item({ library_file_id: null, archive_id: 99, library_file_name: null, archive_name: 'Ran from the screen' }),
    ]);

    expect(only.file).toMatchObject({ id: 99, source: 'archive', name: 'Ran from the screen' });
  });
});

describe('a print with no queue row behind it', () => {
  it('is taken from the printer instead — a screen-started job has no row', () => {
    const live = copyableCurrentPrint(
      status({ current_archive_id: 42, current_plate_id: 2, subtask_name: 'Ran from the screen' }),
    );

    expect(live?.file).toEqual({ id: 42, source: 'archive', name: 'Ran from the screen', plateId: 2 });
    expect(live?.printing).toBe(true);
  });

  it('is nothing at all when the printer names no archive', () => {
    expect(copyableCurrentPrint(status({ subtask_name: 'Something' }))).toBeNull();
    expect(copyableCurrentPrint(undefined)).toBeNull();
  });

  it('goes in front of the pending items', () => {
    const pending = copyableItems([item({ id: 1 })]);

    const withLive = withCurrentPrint(pending, status({ current_archive_id: 42, subtask_name: 'Live' }));

    expect(withLive.map((entry) => entry.file.name)).toEqual(['Live', 'bracket.gcode.3mf']);
  });

  it('is NOT added twice when the queue already has the same archive', () => {
    const rows = copyableItems([
      item({ id: 1, library_file_id: null, archive_id: 42, archive_name: 'Already here', status: 'pending' }),
    ]);

    const withLive = withCurrentPrint(rows, status({ current_archive_id: 42, subtask_name: 'Live' }));

    expect(withLive).toHaveLength(1);
  });

  it('is NOT added when the queue already knows something is printing', () => {
    const rows = copyableItems([item({ id: 1, status: 'printing' })]);

    const withLive = withCurrentPrint(rows, status({ current_archive_id: 42, subtask_name: 'Live' }));

    expect(withLive).toHaveLength(1);
  });
});

describe('where it can go', () => {
  it('offers only the same model', () => {
    const targets = copyTargets(
      [SOURCE, queue({ printer_id: 2, printer_model: 'P1S' }), queue({ printer_id: 3, printer_model: 'A1' })],
      SOURCE,
    );

    expect(targets.map((q) => q.printer_id)).toEqual([2]);
  });

  it('never offers the queue being copied', () => {
    expect(copyTargets([SOURCE], SOURCE)).toEqual([]);
  });

  it('survives queues it has not been given yet', () => {
    expect(copyTargets(undefined, SOURCE)).toEqual([]);
  });
});

describe('the dialog', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getQueues').mockResolvedValue([
      SOURCE,
      queue({ id: 2, printer_id: 2, printer_name: 'P1S-B', printer_model: 'P1S' }),
      queue({ id: 3, printer_id: 3, printer_name: 'A1-C', printer_model: 'A1' }),
    ]);
    vi.spyOn(api, 'getPrinterStatus').mockResolvedValue({ connected: true, progress: 0 } as never);
  });

  it('starts with every item ticked and no printer ticked', async () => {
    renderModal([item({ id: 1 }), item({ id: 2, library_file_id: 11 })]);

    expect(await screen.findByText(/What to copy \(2\)/i)).toBeInTheDocument();
    expect(screen.getByText(/Where to copy it \(0\)/i)).toBeInTheDocument();
  });

  it('will not copy with no printer chosen', async () => {
    const { onConfirm } = renderModal([item()]);

    expect(await screen.findByRole('button', { name: /^Copy$/i })).toBeDisabled();
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('will not copy with every item unticked', async () => {
    renderModal([item()]);
    const user = userEvent.setup();

    await user.click(await screen.findByText('P1S-B'));
    await user.click(screen.getAllByText(/^Clear$/i)[0]);

    expect(screen.getByRole('button', { name: /^Copy$/i })).toBeDisabled();
  });

  it('hands back the chosen items and printers', async () => {
    const { onConfirm } = renderModal([item({ id: 1, plate_id: 2 })]);
    const user = userEvent.setup();

    await user.click(await screen.findByText('P1S-B'));
    await user.click(screen.getByRole('button', { name: /^Copy$/i }));

    expect(onConfirm).toHaveBeenCalledWith(
      [
        {
          id: 10,
          source: 'library',
          name: 'bracket.gcode.3mf',
          plateId: 2,
          // The run groups copies now, and these are what keep two copies of one
          // file apart and let the source queue's blocks be re-formed.
          itemId: 1,
          batchId: undefined,
          // Answered "no order" when it was queued — carried, so the dialog
          // does not ask again.
          orderFiling: { projectId: null, projectLineId: null },
        },
      ],
      [2],
    );
  });

  it('does not offer a printer of another model', async () => {
    renderModal([item()]);

    expect(await screen.findByText('P1S-B')).toBeInTheDocument();
    expect(screen.queryByText('A1-C')).not.toBeInTheDocument();
  });

  it('says so when there is no other printer of this model', async () => {
    vi.spyOn(api, 'getQueues').mockResolvedValue([SOURCE]);

    renderModal([item()]);

    expect(await screen.findByText(/No other P1S printers/i)).toBeInTheDocument();
  });

  it('says how many items it had to leave out', async () => {
    renderModal([item({ id: 1 })], 1);

    expect(await screen.findByText(/1 item is not backed by a file/i)).toBeInTheDocument();
  });
});

describe('the order a copy inherits', () => {
  it('carries the order the row was filed under — and "none" as an answer, not an absence', () => {
    const [filed] = copyableItems([item({ project_id: 4, project_line_id: 9, project_name: 'Lamps' })]);
    expect(filed.file.orderFiling).toEqual({ projectId: 4, projectLineId: 9 });
    expect(filed.orderName).toBe('Lamps');

    const [unfiled] = copyableItems([item({ project_id: null, project_line_id: null, project_name: null })]);
    // Present with nulls: the question was answered when the row was queued,
    // and the dialog must not ask it again.
    expect(unfiled.file.orderFiling).toEqual({ projectId: null, projectLineId: null });
    expect(unfiled.orderName).toBeNull();
  });

  it('says in the row which order the copy will land under', () => {
    renderModal([item({ id: 1, project_id: 4, project_line_id: 9, project_name: 'Lamps' }), item({ id: 2 })]);
    // Only the filed row says so; an unfiled row has nothing to announce.
    expect(screen.getAllByText(/Order: Lamps/)).toHaveLength(1);
  });
});
