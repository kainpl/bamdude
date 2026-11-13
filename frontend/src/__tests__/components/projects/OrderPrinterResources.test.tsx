import { beforeEach, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { render } from '../../utils';
import { api, ApiError, type OrderPrinterResourceState, type OrderResourceProposal, type WorkingPlan } from '../../../api/client';
import { OrderPrinterResources } from '../../../components/projects/OrderPrinterResources';
import { OrderPrinterResourcesDialog } from '../../../components/projects/OrderPrinterResourcesDialog';

function state(): OrderPrinterResourceState {
  return { token: 'a'.repeat(64), resources: { models: null, printer_ids: null }, printers: [
    { id: 1, name: 'Mini 1', model: 'A1 Mini', active: true, allowed: true },
    { id: 2, name: 'Mini 2', model: 'A1 Mini', active: true, allowed: true },
    { id: 3, name: 'Enclosed', model: 'P1S', active: true, allowed: true },
  ] };
}
function proposal(): OrderResourceProposal {
  return { proposal_token: 'b'.repeat(64), inputs_token: 'c'.repeat(64), expected_revision: 2,
    entries: [], remove_auto_ids: [2,3], remove_printer_ids: [4], preserved_auto_ids: [], preserved_printer_ids: [5],
    previous_draft_count: 8, count: 6, rows: [{ line_id: 1, filename: 'Product A.gcode.3mf', printer_model: 'A1 Mini', count: 6 }] };
}
beforeEach(() => {
  vi.clearAllMocks();
  let stored = state();
  vi.spyOn(api, 'getOrderPrinterResources').mockImplementation(async () => stored);
  vi.spyOn(api, 'updateOrderPrinterResources').mockImplementation(async (_id, _token, resources) => {
    stored = { ...stored, token: 'd'.repeat(64), resources };
    return stored;
  });
  vi.spyOn(api, 'getWorkingPlan').mockResolvedValue({ revision: 2 } as WorkingPlan);
  vi.spyOn(api, 'previewOrderResourceReplan').mockResolvedValue(proposal());
  vi.spyOn(api, 'applyOrderResourceReplan').mockResolvedValue({ working_plan: { revision: 3 } as WorkingPlan, removed_auto_ids: [2,3], removed_printer_ids: [4] });
});

it('shows a visible named action and persistent whole-farm summary', async () => {
  const configure = vi.fn();
  render(<OrderPrinterResources orderId={1} editable onConfigure={configure} />);
  await screen.findByText('Whole farm');
  fireEvent.click(screen.getByRole('button', { name: 'Configure restrictions' }));
  expect(configure).toHaveBeenCalledOnce();
});

it('saves paused-queue planning directly from the order card', async () => {
  const onChanged = vi.fn();
  render(<OrderPrinterResources orderId={1} editable onConfigure={vi.fn()} onChanged={onChanged} />);
  const toggle = await screen.findByRole('checkbox', { name: 'Include paused printers in the print plan' });
  expect(toggle).not.toBeChecked();
  fireEvent.click(toggle);
  await waitFor(() => expect(toggle).toBeChecked());
  expect(api.updateOrderPrinterResources).toHaveBeenCalledWith(1, 'a'.repeat(64), {
    models: null, printer_ids: null, include_paused_queues: true,
  });
  expect(onChanged).toHaveBeenCalledOnce();
  fireEvent.click(toggle);
  await waitFor(() => expect(toggle).not.toBeChecked());
  expect(api.updateOrderPrinterResources).toHaveBeenLastCalledWith(1, 'd'.repeat(64), {
    models: null, printer_ids: null, include_paused_queues: false,
  });
});

it('explains intersection, preserves product files and saves only explicit order choices', async () => {
  const changed = vi.fn();
  render(<OrderPrinterResourcesDialog door={{ orderId: 1, onChanged: changed }} onClose={vi.fn()} />);
  await screen.findByRole('checkbox', { name: 'All models' });
  expect(screen.getByText(/does not reserve machines/)).toBeVisible();
  expect(screen.getByText(/Models and specific printers intersect/)).toBeVisible();
  fireEvent.click(screen.getByRole('checkbox', { name: 'All models' }));
  expect(screen.getByRole('button', { name: 'Save restrictions' })).toBeDisabled();
  fireEvent.click(screen.getByRole('checkbox', { name: 'A1 Mini' }));
  fireEvent.click(screen.getByRole('checkbox', { name: 'Any printer of the allowed models' }));
  fireEvent.click(screen.getByRole('checkbox', { name: /Mini 2/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Save restrictions' }));
  await waitFor(() => expect(api.updateOrderPrinterResources).toHaveBeenCalledWith(1, 'a'.repeat(64), { models: ['A1 Mini'], printer_ids: [2] }));
  expect(api.applyOrderResourceReplan).not.toHaveBeenCalled();
});

it('requires review and acknowledgement before removing pending work, then asks for explicit enqueue', async () => {
  render(<OrderPrinterResourcesDialog door={{ orderId: 1, onChanged: vi.fn() }} onClose={vi.fn()} />);
  await screen.findByRole('checkbox', { name: 'All models' });
  fireEvent.click(screen.getByRole('button', { name: 'Review remaining work' }));
  await screen.findByText(/Remove 2 auto-queue jobs and 1 printer-queue jobs/);
  expect(api.applyOrderResourceReplan).not.toHaveBeenCalled();
  expect(screen.getByRole('button', { name: 'Remove pending jobs and prepare plan' })).toBeDisabled();
  fireEvent.click(screen.getByRole('checkbox', { name: /I reviewed the changes/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Remove pending jobs and prepare plan' }));
  await waitFor(() => expect(api.applyOrderResourceReplan).toHaveBeenCalledOnce());
  await screen.findByText(/Pending jobs removed and new draft prepared/);
});

it('invalidates a reviewed proposal when selection changes', async () => {
  render(<OrderPrinterResourcesDialog door={{ orderId: 1, onChanged: vi.fn() }} onClose={vi.fn()} />);
  await screen.findByRole('checkbox', { name: 'All models' });
  fireEvent.click(screen.getByRole('button', { name: 'Review remaining work' }));
  await screen.findByRole('checkbox', { name: /I reviewed the changes/ });
  fireEvent.click(screen.getByRole('checkbox', { name: 'All models' }));
  expect(screen.queryByRole('checkbox', { name: /I reviewed the changes/ })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Review remaining work' })).toBeDisabled();
});

it('refuses stale queue results and requires a new review instead of replaying apply', async () => {
  vi.mocked(api.applyOrderResourceReplan).mockRejectedValue(new ApiError('Changed', 409, 'order_resources_changed'));
  render(<OrderPrinterResourcesDialog door={{ orderId: 1, onChanged: vi.fn() }} onClose={vi.fn()} />);
  await screen.findByRole('checkbox', { name: 'All models' });
  fireEvent.click(screen.getByRole('button', { name: 'Review remaining work' }));
  fireEvent.click(await screen.findByRole('checkbox', { name: /I reviewed the changes/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Remove pending jobs and prepare plan' }));
  await screen.findByRole('alert');
  expect(screen.queryByRole('button', { name: 'Remove pending jobs and prepare plan' })).not.toBeInTheDocument();
  expect(api.applyOrderResourceReplan).toHaveBeenCalledOnce();
});
