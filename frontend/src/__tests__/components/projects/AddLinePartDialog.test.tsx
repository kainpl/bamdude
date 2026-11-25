import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api, ApiError, type LineConfigurationImpact, type LinePartCandidates, type Order } from '../../../api/client';
import { AddLinePartDialog } from '../../../components/projects/AddLinePartDialog';
import { render } from '../../utils';

const ring = {
  id: 2, part_identity: 'original-ring', name: 'Copper ring', kind: 'purchased',
  compatible: true, saved_qty: 0, suggested_qty: 1, extra_percent: 0,
};
const starting: LinePartCandidates = {
  revision: 1, catalog_version: 'original-catalog', quantity: 3, variant_available: true,
  variant_names: ['Opal'], parts: [ring],
};
const line = {
  id: 21, mode: 'product' as const, product_name: 'Desk lamp', quantity: 3,
};

function open() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <AddLinePartDialog orderId={9} line={line} onClose={() => {}} onSaved={() => {}} />
    </QueryClientProvider>,
  );
  return client;
}

async function selectRing() {
  await screen.findByRole('option', { name: 'Copper ring' });
  const picker = screen.getByRole('combobox');
  fireEvent.change(picker, { target: { value: '2' } });
  return picker;
}

async function readyToAdd() {
  const add = screen.getByRole('button', { name: 'Add' });
  await waitFor(() => expect(add).toBeEnabled());
  return add;
}

const noReservation: LineConfigurationImpact = {
  reserved_before: 0, reserved_after: 0, finished_before: 0, finished_after: 0, dropping: [],
};

describe('AddLinePartDialog', () => {
  beforeEach(() => { vi.spyOn(api, 'previewLineConfiguration').mockResolvedValue(noReservation); });
  afterEach(() => { vi.restoreAllMocks(); });

  it('clears a replaced selection even when its numeric ID and name are unchanged', async () => {
    const candidates = vi.spyOn(api, 'getLinePartCandidates').mockResolvedValue(starting);
    const write = vi.spyOn(api, 'addLinePart').mockResolvedValue({} as Order);
    const client = open();
    const picker = await selectRing();
    const quantity = screen.getByRole('spinbutton');
    fireEvent.change(quantity, { target: { value: '4' } });

    candidates.mockResolvedValue({
      ...starting, catalog_version: 'new-catalog',
      parts: [{ ...ring, part_identity: 'replacement-ring' }],
    });
    await act(async () => { await client.invalidateQueries({ queryKey: ['line-part-candidates', 9, 21] }); });
    await waitFor(() => expect(picker).toHaveValue('0'));
    expect(quantity).toHaveValue(null);
    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled();
    expect(write).not.toHaveBeenCalled();

    fireEvent.change(picker, { target: { value: '2' } });
    expect(quantity).toHaveValue(1);
    fireEvent.click(await readyToAdd());
    await waitFor(() => expect(write).toHaveBeenCalledWith(9, 21, expect.objectContaining({
      part_id: 2, part_identity: 'replacement-ring', quantity: 1, catalog_version: 'new-catalog',
    })));
  });

  it('keeps the same part and typed count when only the number of units changes', async () => {
    const candidates = vi.spyOn(api, 'getLinePartCandidates').mockResolvedValue(starting);
    const write = vi.spyOn(api, 'addLinePart')
      .mockRejectedValueOnce(new ApiError('Review again', 409))
      .mockResolvedValue({} as Order);
    open();
    const picker = await selectRing();
    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '4' } });
    candidates.mockResolvedValue({ ...starting, quantity: 5 });
    const add = await readyToAdd();
    fireEvent.click(add);
    await screen.findByText('4 × 5 = 20');
    expect(picker).toHaveValue('2');
    expect(screen.getByRole('spinbutton')).toHaveValue(4);
    await waitFor(() => expect(add).toBeEnabled());
    fireEvent.click(add);
    await waitFor(() => expect(write).toHaveBeenLastCalledWith(9, 21, expect.objectContaining({
      part_identity: 'original-ring', expected_quantity: 5, quantity: 4,
    })));
  });

  it('requires a fresh reservation preview after a conflict even when candidates are unchanged', async () => {
    const candidates = vi.spyOn(api, 'getLinePartCandidates').mockResolvedValue(starting);
    const write = vi.spyOn(api, 'addLinePart')
      .mockRejectedValueOnce(new ApiError('Review again', 409))
      .mockResolvedValue({} as Order);
    open();
    await selectRing();
    const add = await readyToAdd();
    let resolvePreview!: (preview: LineConfigurationImpact) => void;
    vi.mocked(api.previewLineConfiguration).mockImplementation(() => new Promise((resolve) => { resolvePreview = resolve; }));
    fireEvent.click(add);
    await waitFor(() => expect(resolvePreview).toBeDefined());
    expect(candidates).toHaveBeenCalledTimes(2);
    expect(add).toBeDisabled();
    expect(write).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolvePreview({
        ...noReservation, finished_before: 1, part_reservation_version: 'fresh-holds',
        released_parts: [{ part_id: 7, name: 'Printed shade', quantity: 2 }],
      });
    });
    await screen.findByText('Ready from stock: 1 → 0');
    expect(screen.getByText('Will release from reservation: Printed shade × 2')).toBeInTheDocument();
    await readyToAdd();
    fireEvent.click(add);
    await waitFor(() => expect(write).toHaveBeenLastCalledWith(9, 21, expect.objectContaining({
      reservation_review: {
        reserved_before: 0, reserved_after: 0, finished_before: 1, finished_after: 0,
        part_reservation_version: 'fresh-holds',
      },
    })));
  });

  it('takes the variant label and available parts from the same refreshed candidate list', async () => {
    const candidates = vi.spyOn(api, 'getLinePartCandidates').mockResolvedValue(starting);
    vi.spyOn(api, 'addLinePart').mockRejectedValueOnce(new ApiError('Review again', 409));
    open();
    const picker = await selectRing();
    expect(screen.getByText('Desk lamp · Opal')).toBeInTheDocument();
    candidates.mockResolvedValue({
      ...starting, revision: 2, catalog_version: 'new-variant', variant_names: ['Amber'],
      parts: [
        { ...ring, compatible: false },
        { ...ring, id: 3, name: 'Amber lens', part_identity: 'amber-lens' },
      ],
    });
    fireEvent.click(await readyToAdd());
    await screen.findByRole('option', { name: 'Amber lens' });
    expect(screen.getByText('Desk lamp · Amber')).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: 'For the selected variant' })).toBeChecked();
    expect(screen.queryByRole('option', { name: 'Copper ring' })).toBeNull();
    expect(picker).toHaveValue('0');
    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled();
  });
});
