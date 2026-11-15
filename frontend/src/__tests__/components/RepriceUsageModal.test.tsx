/**
 * Re-pricing recorded usage is scoped and previewed before it is applied
 * (owner's decision Р4).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { RepriceUsageModal } from '../../components/RepriceUsageModal';
import { api } from '../../api/client';

const PREVIEW = {
  dry_run: true, rows: 10, rows_changed: 10, archives_changed: 10, orders_changed: 1, cost_before: 200, cost_after: 20,
};

function open(props: Partial<Parameters<typeof RepriceUsageModal>[0]> = {}) {
  const onClose = vi.fn();
  const onDone = vi.fn();
  render(<RepriceUsageModal currency="$" onClose={onClose} onDone={onDone} {...props} />);
  return { onClose, onDone };
}

describe('RepriceUsageModal', () => {
  let reprice: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    reprice = vi.spyOn(api, 'repriceUsage').mockImplementation(async (body) => ({ ...PREVIEW, dry_run: body.dry_run }));
  });

  it('offers nothing until a spool or a period is chosen', () => {
    open();

    expect(screen.getByRole('button', { name: 'Preview' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Re-price' })).toBeDisabled();
    expect(screen.getByText('Choose a spool, a period, or both.')).toBeInTheDocument();
  });

  it('previews the selection and says what would move', async () => {
    open();
    fireEvent.change(screen.getByLabelText('Spool number'), { target: { value: '57' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));

    expect(await screen.findByText('10 of 10 usage records would change')).toBeInTheDocument();
    expect(screen.getByText('10 prints would get a new cost')).toBeInTheDocument();
    expect(screen.getByText('1 order would move')).toBeInTheDocument();
    expect(screen.getByText('Filament cost of the selection: $200.00 → $20.00')).toBeInTheDocument();
    expect(reprice).toHaveBeenCalledWith({ spool_id: 57, date_from: null, date_to: null, dry_run: true });
  });

  it('applies only after a preview, with the same selection', async () => {
    const { onClose, onDone } = open();
    fireEvent.change(screen.getByLabelText('Spool number'), { target: { value: '57' } });
    expect(screen.getByRole('button', { name: 'Re-price' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
    await screen.findByText('10 of 10 usage records would change');
    fireEvent.click(screen.getByRole('button', { name: 'Re-price' }));

    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(reprice).toHaveBeenLastCalledWith({ spool_id: 57, date_from: null, date_to: null, dry_run: false });
    expect(onClose).toHaveBeenCalled();
  });

  it('throws the preview away when the selection changes', async () => {
    open();
    fireEvent.change(screen.getByLabelText('Spool number'), { target: { value: '57' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
    await screen.findByText('10 of 10 usage records would change');

    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-10-01' } });

    expect(screen.queryByText('10 of 10 usage records would change')).toBeNull();
    expect(screen.getByRole('button', { name: 'Re-price' })).toBeDisabled();
    // Half a period is not a scope either.
    expect(screen.getByRole('button', { name: 'Preview' })).toBeDisabled();
  });

  it('takes a period alone', async () => {
    open();
    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-10-01' } });
    fireEvent.change(screen.getByLabelText('To'), { target: { value: '2026-10-31' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));

    await waitFor(() =>
      expect(reprice).toHaveBeenCalledWith({ spool_id: null, date_from: '2026-10-01', date_to: '2026-10-31', dry_run: true }));
  });

  it('does not offer to apply when nothing would change', async () => {
    reprice.mockResolvedValue({ ...PREVIEW, rows_changed: 0, archives_changed: 0, orders_changed: 0 });
    open({ spool: { id: 57, name: 'PLA Basic Black' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));

    expect(await screen.findByText(/Nothing to change/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Re-price' })).toBeDisabled();
  });

  it('shows the refusal it was given', async () => {
    reprice.mockRejectedValue(new Error('Spool not found'));
    open();
    fireEvent.change(screen.getByLabelText('Spool number'), { target: { value: '999' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Spool not found');
  });
});
