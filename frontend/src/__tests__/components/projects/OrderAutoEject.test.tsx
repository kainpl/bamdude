import { describe, it, expect, vi, afterEach } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import { makeOrder } from '../../fixtures/orderDetail';
import { OrderAutoEject } from '../../../components/projects/OrderAutoEject';
import { AutoEjectBadge } from '../../../components/AutoEjectBadge';

afterEach(() => vi.restoreAllMocks());

function openConfirmation() {
  fireEvent.click(screen.getByRole('checkbox', { name: 'Auto-eject after printing' }));
  return within(screen.getByRole('dialog', { name: 'Enable auto-eject for this order?' }));
}

function acknowledgeAndEnable() {
  const dialog = openConfirmation();
  fireEvent.click(dialog.getByRole('checkbox'));
  fireEvent.click(dialog.getByRole('button', { name: 'Enable auto-eject' }));
}

describe('Order auto-eject MVP', () => {
  it('is off by default and writes only the order flag', async () => {
    const order = makeOrder({ name: 'Product A order' });
    const update = vi.spyOn(api, 'updateOrder').mockResolvedValue({ ...order, auto_eject_enabled: true });
    render(<OrderAutoEject order={order} canEdit />);
    const toggle = screen.getByRole('checkbox', { name: 'Auto-eject after printing' });
    expect(toggle).not.toBeChecked();
    const dialog = openConfirmation();
    expect(update).not.toHaveBeenCalled();
    expect(toggle).not.toBeChecked();
    const confirm = dialog.getByRole('button', { name: 'Enable auto-eject' });
    expect(confirm).toBeDisabled();
    fireEvent.click(confirm);
    expect(update).not.toHaveBeenCalled();
    fireEvent.click(dialog.getByRole('checkbox'));
    fireEvent.click(confirm);
    await waitFor(() => expect(update).toHaveBeenCalledWith(order.id, { auto_eject_enabled: true }));
    expect(screen.queryByText(/Apply mode to pending/)).not.toBeInTheDocument();
  });

  it('readers and closed orders cannot change the setting', () => {
    render(<OrderAutoEject order={makeOrder({ name: 'Product B order', auto_eject_enabled: true })} canEdit={false} />);
    expect(screen.getByRole('checkbox', { name: 'Auto-eject after printing' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'Auto-eject after printing' })).toBeDisabled();
  });

  it('shows a save failure and keeps the server value', async () => {
    vi.spyOn(api, 'updateOrder').mockRejectedValue(new Error('Synthetic refusal'));
    render(<OrderAutoEject order={makeOrder()} canEdit />);
    acknowledgeAndEnable();
    expect(await screen.findByRole('alert')).toHaveTextContent('Synthetic refusal');
    expect(screen.getByRole('checkbox', { name: 'Auto-eject after printing' })).not.toBeChecked();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('cancels without writing and requires a new acknowledgement on reopen', () => {
    const update = vi.spyOn(api, 'updateOrder');
    render(<OrderAutoEject order={makeOrder()} canEdit />);
    let dialog = openConfirmation();
    fireEvent.click(dialog.getByRole('checkbox'));
    fireEvent.click(dialog.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(update).not.toHaveBeenCalled();
    dialog = openConfirmation();
    expect(dialog.getByRole('checkbox')).not.toBeChecked();
    expect(dialog.getByRole('button', { name: 'Enable auto-eject' })).toBeDisabled();
  });

  it('disables an enabled order immediately without another confirmation', async () => {
    const order = makeOrder({ auto_eject_enabled: true });
    const update = vi.spyOn(api, 'updateOrder').mockResolvedValue({ ...order, auto_eject_enabled: false });
    render(<OrderAutoEject order={order} canEdit />);
    fireEvent.click(screen.getByRole('checkbox', { name: 'Auto-eject after printing' }));
    await waitFor(() => expect(update).toHaveBeenCalledWith(order.id, { auto_eject_enabled: false }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('names the reference profile, links its source and explains the limits', () => {
    render(<OrderAutoEject order={makeOrder()} canEdit />);
    const dialog = openConfirmation();
    expect(dialog.getByText(/Infinity Flow 3D Tilt Kit/)).toBeInTheDocument();
    expect(dialog.getByText(/2025-10-08/)).toBeInTheDocument();
    expect(dialog.getByText(/greater than 5.5 mm/)).toBeInTheDocument();
    expect(dialog.getByText(/outside the image or selected region/)).toBeInTheDocument();
    expect(dialog.getByText(/including P1S/)).toBeInTheDocument();
    expect(dialog.getByRole('link')).toHaveAttribute('href',
      'https://infinityflow3d.com/pages/free-3d-printer-auto-clearing-cad-and-g-code');
    expect(dialog.getByText(/does not validate the preset/)).toBeInTheDocument();
  });

  it('does not submit twice while saving and cannot be cancelled mid-save', async () => {
    const order = makeOrder();
    let resolve!: (value: typeof order) => void;
    const update = vi.spyOn(api, 'updateOrder').mockImplementation(() => new Promise((done) => { resolve = done; }));
    render(<OrderAutoEject order={order} canEdit />);
    const dialog = openConfirmation();
    fireEvent.click(dialog.getByRole('checkbox'));
    const confirm = dialog.getByRole('button', { name: 'Enable auto-eject' });
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
    expect(dialog.getByRole('button', { name: 'Cancel' })).toBeDisabled();
    expect(dialog.getByRole('checkbox')).toBeDisabled();
    resolve({ ...order, auto_eject_enabled: true });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('cannot confirm for another order after navigation', () => {
    const update = vi.spyOn(api, 'updateOrder');
    const { rerender } = render(<OrderAutoEject order={makeOrder({ id: 1 })} canEdit />);
    const dialog = openConfirmation();
    fireEvent.click(dialog.getByRole('checkbox'));
    rerender(<OrderAutoEject order={makeOrder({ id: 2 })} canEdit />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(update).not.toHaveBeenCalled();
  });

  it('closes the confirmation if the order becomes read-only', () => {
    const update = vi.spyOn(api, 'updateOrder');
    const order = makeOrder();
    const { rerender } = render(<OrderAutoEject order={order} canEdit />);
    openConfirmation();
    rerender(<OrderAutoEject order={order} canEdit={false} />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('checkbox')).toBeDisabled();
    expect(update).not.toHaveBeenCalled();
  });

  it('badge represents the stored job flag', () => {
    const { rerender } = render(<AutoEjectBadge mode={false} />);
    expect(screen.queryByText('Auto-eject')).not.toBeInTheDocument();
    rerender(<AutoEjectBadge mode />);
    expect(screen.getByText('Auto-eject')).toBeInTheDocument();
  });

  it('saves a bounded threshold explicitly and describes the detection limits', async () => {
    const order = makeOrder({ auto_eject_enabled: true });
    const update = vi.spyOn(api, 'updateOrder').mockResolvedValue(order);
    render(<OrderAutoEject order={order} canEdit />);
    const input = screen.getByRole('spinbutton');
    const save = screen.getByRole('button', { name: 'Save threshold' });
    expect(input).toHaveValue(1);
    expect(save).toBeDisabled();
    fireEvent.change(input, { target: { value: '11' } });
    expect(save).toBeDisabled();
    fireEvent.click(save);
    expect(update).not.toHaveBeenCalled();
    fireEvent.change(input, { target: { value: '' } });
    expect(save).toBeDisabled();
    fireEvent.change(input, { target: { value: '10' } });
    expect(screen.getByText(/may classify a plate with parts as empty/)).toBeInTheDocument();
    expect(update).not.toHaveBeenCalled();
    fireEvent.click(save);
    await waitFor(() => expect(update).toHaveBeenCalledWith(order.id, {
      auto_eject_settings: { difference_threshold: 10, skip_check: false },
    }));
  });

  it('requires two separate dialogs and an explicit acknowledgement before skipping checks', async () => {
    const order = makeOrder({ auto_eject_enabled: true });
    const update = vi.spyOn(api, 'updateOrder').mockResolvedValue(order);
    render(<OrderAutoEject order={order} canEdit />);
    const toggle = screen.getByRole('checkbox', { name: 'Skip OpenCV plate check' });
    fireEvent.click(toggle);
    let dialog = within(screen.getByRole('dialog', { name: 'Skip the camera check? (1 of 2)' }));
    expect(dialog.getByText('AT YOUR OWN RISK — THE PLATE WILL NOT BE CHECKED')).toBeInTheDocument();
    expect(update).not.toHaveBeenCalled();
    expect(toggle).not.toBeChecked();
    fireEvent.click(dialog.getByRole('button', { name: 'Continue to final confirmation' }));
    dialog = within(screen.getByRole('dialog', { name: 'Confirm operation without OpenCV (2 of 2)' }));
    const confirm = dialog.getByRole('button', { name: 'Accept risk and skip check' });
    expect(confirm).toBeDisabled();
    fireEvent.click(confirm);
    expect(update).not.toHaveBeenCalled();
    fireEvent.click(dialog.getByRole('checkbox'));
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
    expect(update).toHaveBeenCalledWith(order.id, { auto_eject_settings: { difference_threshold: 1, skip_check: true },
      auto_eject_skip_acknowledged: true });
  });

  it('cancels or navigates away without saving an unsafe preference', () => {
    const update = vi.spyOn(api, 'updateOrder');
    const order = makeOrder({ id: 1, auto_eject_enabled: true });
    const { rerender } = render(<OrderAutoEject order={order} canEdit />);
    fireEvent.click(screen.getByRole('checkbox', { name: 'Skip OpenCV plate check' }));
    fireEvent.click(screen.getByRole('button', { name: 'Continue to final confirmation' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(update).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Skip OpenCV plate check' }));
    expect(screen.getByRole('dialog', { name: 'Skip the camera check? (1 of 2)' })).toBeInTheDocument();
    rerender(<OrderAutoEject order={makeOrder({ id: 2, auto_eject_enabled: true })} canEdit />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(update).not.toHaveBeenCalled();
  });

  it('shows skip errors inside the second dialog without checking the server toggle', async () => {
    vi.spyOn(api, 'updateOrder').mockRejectedValue(new Error('Synthetic refusal'));
    render(<OrderAutoEject order={makeOrder({ auto_eject_enabled: true })} canEdit />);
    fireEvent.click(screen.getByRole('checkbox', { name: 'Skip OpenCV plate check' }));
    fireEvent.click(screen.getByRole('button', { name: 'Continue to final confirmation' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: 'Accept risk and skip check' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Synthetic refusal');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'Skip OpenCV plate check' })).not.toBeChecked();
  });

  it('restores checks immediately and displays saved opt-out prominently', async () => {
    const order = makeOrder({ auto_eject_enabled: true, auto_eject_settings: { difference_threshold: 2, skip_check: true } });
    const update = vi.spyOn(api, 'updateOrder').mockResolvedValue(order);
    render(<OrderAutoEject order={order} canEdit />);
    expect(screen.getByRole('alert')).toHaveTextContent('AT YOUR OWN RISK');
    expect(screen.getByRole('spinbutton')).toBeDisabled();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Skip OpenCV plate check' }));
    await waitFor(() => expect(update).toHaveBeenCalledWith(order.id, {
      auto_eject_settings: { difference_threshold: 2, skip_check: false },
    }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('re-enabling auto-eject resets a previously saved camera opt-out', async () => {
    const order = makeOrder({ auto_eject_settings: { difference_threshold: 2, skip_check: true } });
    const update = vi.spyOn(api, 'updateOrder').mockResolvedValue(order);
    render(<OrderAutoEject order={order} canEdit />);
    acknowledgeAndEnable();
    await waitFor(() => expect(update).toHaveBeenCalledWith(order.id, {
      auto_eject_enabled: true, auto_eject_settings: { difference_threshold: 2, skip_check: false },
    }));
  });

  it('marks an unchecked queue job separately from checked auto-eject', () => {
    render(<AutoEjectBadge mode settings={{ difference_threshold: 1, skip_check: true }} />);
    expect(screen.getByText('Auto-eject · no plate check')).toHaveAttribute('title',
      'AT YOUR OWN RISK — THE PLATE WILL NOT BE CHECKED');
  });
});
