import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import i18n from '../../i18n';
import { api, type Printer } from '../../api/client';
import { BulkPrinterPlacementDialog } from '../../components/BulkPrinterPlacementDialog';
import { BulkPrinterToolbar } from '../../components/BulkPrinterToolbar';
import { ToastProvider } from '../../contexts/ToastContext';

const { allowed } = vi.hoisted(() => ({ allowed: { value: true } }));
vi.mock('../../contexts/AuthContext', () => ({ useAuth: () => ({ hasPermission: () => allowed.value }) }));

const room = { id: 7, name: 'Room', parent_id: null, path: 'Room' };
const printers = [
  { id: 1, name: 'A', location: room, location_id: 7, tags: [], tag_ids: [3] },
  { id: 2, name: 'B', location: null, location_id: null, tags: [], tag_ids: [] },
] as unknown as Printer[];

function mount(ui: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>{ui}</ToastProvider>
    </QueryClientProvider>,
  );
}

beforeEach(async () => {
  vi.restoreAllMocks();
  allowed.value = true;
  await i18n.changeLanguage('en');
  vi.spyOn(api, 'getPrinterTags').mockResolvedValue({
    tags: [
      { id: 3, name: 'Fast', color: null, printer_count: 1 },
      { id: 4, name: 'ABS', color: '#ff0000', printer_count: 0 },
    ],
  } as Awaited<ReturnType<typeof api.getPrinterTags>>);
  vi.spyOn(api, 'getPrinterLocations').mockResolvedValue({
    locations: [{ ...room, depth: 1 }],
  } as unknown as Awaited<ReturnType<typeof api.getPrinterLocations>>);
  vi.spyOn(api, 'placePrinters').mockResolvedValue({ updated: 2 });
});

describe('BulkPrinterPlacementDialog', () => {
  it('shows where the selection stands and sends nothing until something is chosen', async () => {
    mount(<BulkPrinterPlacementDialog printers={printers} onClose={vi.fn()} />);

    expect(await screen.findByText('Room · 1')).toBeInTheDocument();
    expect(screen.getByText('Ungrouped · 1')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Apply' })).toBeDisabled();
    expect(api.placePrinters).not.toHaveBeenCalled();
  });

  it('sends one request with the tags to add and to remove, leaving the place alone', async () => {
    const user = userEvent.setup();
    const close = vi.fn();
    mount(<BulkPrinterPlacementDialog printers={printers} onClose={close} />);

    await user.click(within(await screen.findByRole('group', { name: 'ABS' })).getByRole('button', { name: 'Add' }));
    await user.click(within(screen.getByRole('group', { name: 'Fast' })).getByRole('button', { name: 'Remove' }));
    await user.click(screen.getByRole('button', { name: 'Apply' }));

    await waitFor(() => expect(close).toHaveBeenCalled());
    expect(api.placePrinters).toHaveBeenCalledExactlyOnceWith({
      printer_ids: [1, 2],
      set_location: false,
      location_id: null,
      add_tag_ids: [4],
      remove_tag_ids: [3],
    });
  });

  it('a tag nobody carries cannot be removed, and one everybody carries cannot be added', async () => {
    mount(<BulkPrinterPlacementDialog printers={[printers[0]]} onClose={vi.fn()} />);

    expect(within(await screen.findByRole('group', { name: 'ABS' })).getByRole('button', { name: 'Remove' })).toBeDisabled();
    expect(within(screen.getByRole('group', { name: 'Fast' })).getByRole('button', { name: 'Add' })).toBeDisabled();
  });

  it('changing the location to none takes the printers out of their place', async () => {
    const user = userEvent.setup();
    mount(<BulkPrinterPlacementDialog printers={printers} onClose={vi.fn()} />);

    await user.click(await screen.findByRole('checkbox', { name: 'Change the location' }));
    await user.click(screen.getByRole('button', { name: 'Apply' }));

    await waitFor(() => expect(api.placePrinters).toHaveBeenCalled());
    expect(vi.mocked(api.placePrinters).mock.calls[0][0]).toMatchObject({ set_location: true, location_id: null });
  });

  it('a refusal is shown and the dialog stays open', async () => {
    const user = userEvent.setup();
    const close = vi.fn();
    vi.mocked(api.placePrinters).mockRejectedValue(new Error('Archived printers cannot be placed: B.'));
    mount(<BulkPrinterPlacementDialog printers={printers} onClose={close} />);

    await user.click(await screen.findByRole('checkbox', { name: 'Change the location' }));
    await user.click(screen.getByRole('button', { name: 'Apply' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Archived printers cannot be placed: B.');
    expect(close).not.toHaveBeenCalled();
  });
});

describe('BulkPrinterToolbar placement', () => {
  const toolbar = (props: Partial<React.ComponentProps<typeof BulkPrinterToolbar>> = {}) => (
    <BulkPrinterToolbar
      selectedIds={new Set([1, 2])}
      printers={printers}
      onClose={vi.fn()}
      onSelectAll={vi.fn()}
      onSelectByLocation={vi.fn()}
      onSelectByState={vi.fn()}
      onAction={vi.fn()}
      onPlacement={vi.fn()}
      actionPending={false}
      {...props}
    />
  );

  it('the menu opens the placement dialog, not a device command', async () => {
    const user = userEvent.setup();
    const onPlacement = vi.fn();
    const onAction = vi.fn();
    mount(toolbar({ onPlacement, onAction }));

    await user.click(screen.getByRole('button', { name: 'Actions' }));
    await user.click(screen.getByRole('menuitem', { name: 'Location and tags' }));

    expect(onPlacement).toHaveBeenCalledOnce();
    expect(onAction).not.toHaveBeenCalled();
  });

  it('the printers that stand nowhere can be selected at once', async () => {
    const user = userEvent.setup();
    const onSelectByLocation = vi.fn();
    mount(toolbar({ onSelectByLocation }));

    await user.click(screen.getByRole('button', { name: 'By Location' }));
    await user.click(screen.getByRole('button', { name: /Ungrouped/ }));

    expect(onSelectByLocation).toHaveBeenCalledExactlyOnceWith(null);
  });
});
