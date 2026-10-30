import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { api, ApiError, type PrintQueueItem } from '../../api/client';
import { QueueDuration } from '../../components/QueueDuration';
import { sharedSlicerTime } from '../../utils/slicerTime';
import { MoveToAutoButton } from '../../components/MoveToAutoButton';
import { QueueStateBadge } from '../../components/QueueStateBadge';

const rights = vi.hoisted(() => ({ allowed: true }));

vi.mock('../../contexts/AuthContext', async importOriginal => ({
  ...await importOriginal<typeof import('../../contexts/AuthContext')>(),
  useAuth: () => ({ authEnabled: true, user: { id: 1 }, hasPermission: () => rights.allowed }),
}));

const item = { id: 9, created_by_id: 1, status: 'pending' } as PrintQueueItem;
const proposal = { allowed: true, token: 'signed', manual_start: true, release_pins: true,
  display_name: 'A very long sliced job', comparison_model: 'P1S', target_model: 'P1S',
  option_changes: [{ field: 'timelapse', before: false, after: true }] };

describe('queue operator controls', () => {
  beforeEach(() => { vi.restoreAllMocks(); rights.allowed = true; });

  it('renders no bordered queue strip without queue read permission', () => {
    rights.allowed = false;
    const { container } = render(<QueueStateBadge printerId={1} bordered />);
    expect(container).toBeEmptyDOMElement();
  });

  it('explains a closed-order preview refusal in the dialog', async () => {
    vi.spyOn(api, 'previewQueueAutoTransfer').mockRejectedValue(new ApiError('The order is closed', 409, 'transfer_order_closed'));
    render(<MoveToAutoButton item={item} />);
    fireEvent.click(screen.getByRole('button', { name: 'Move to auto queue' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('The order is closed. Nothing was moved.');
    expect(screen.getByRole('button', { name: 'Move' })).toBeDisabled();
  });

  it.each([null, undefined, 0, -1, NaN, Infinity])('does not invent duration for %s', value => {
    render(<QueueDuration seconds={value} />);
    expect(screen.getByText('—')).toHaveAttribute('title', 'Slicer estimate unavailable');
  });

  it('formats positive subminute and hour estimates without a countdown', () => {
    render(<><QueueDuration seconds={20} /><QueueDuration seconds={5460} /></>);
    expect(screen.getByText('<1m')).toBeInTheDocument();
    expect(screen.getByText('1h 31m')).toBeInTheDocument();
    expect(sharedSlicerTime([120, 120])).toBe(120);
    expect(sharedSlicerTime([120, null])).toBeNull();
    expect(sharedSlicerTime([120, 180])).toBeNull();
    expect(sharedSlicerTime([])).toBeNull();
  });

  it('requires all applicable consents and sends one addressed move', async () => {
    vi.spyOn(api, 'previewQueueAutoTransfer').mockResolvedValue(proposal);
    const move = vi.spyOn(api, 'moveQueueToAuto').mockResolvedValue({ old_item_id: 9, resulting_auto_item_id: 21, mode: 'created' });
    render(<MoveToAutoButton item={item} />);
    expect(api.previewQueueAutoTransfer).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Move to auto queue' }));
    const confirm = await screen.findByRole('button', { name: 'Move' });
    expect(confirm).toBeDisabled();
    await screen.findByText(/A very long sliced job/);
    for (const checkbox of screen.getAllByRole('checkbox')) fireEvent.click(checkbox);
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);
    await waitFor(() => expect(move).toHaveBeenCalledTimes(1));
    expect(move).toHaveBeenCalledWith(9, { token: 'signed', allow_automatic_start: true,
      allow_automatic_filament: true, accept_profile_changes: true });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('keeps an unknown sent outcome non-retryable', async () => {
    vi.spyOn(api, 'previewQueueAutoTransfer').mockResolvedValue({ ...proposal, manual_start: false, release_pins: false, option_changes: [] });
    const move = vi.spyOn(api, 'moveQueueToAuto').mockRejectedValue(new TypeError('Failed to fetch'));
    render(<MoveToAutoButton item={item} />);
    fireEvent.click(screen.getByRole('button', { name: 'Move to auto queue' }));
    const confirm = await screen.findByRole('button', { name: 'Move' });
    await waitFor(() => expect(confirm).toBeEnabled());
    fireEvent.click(confirm);
    await screen.findByText(/The result is unknown/);
    expect(confirm).toBeDisabled();
    fireEvent.click(confirm);
    expect(move).toHaveBeenCalledTimes(1);
  });
});
