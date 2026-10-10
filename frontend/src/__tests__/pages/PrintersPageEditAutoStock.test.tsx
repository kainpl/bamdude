import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { EditPrinterModal } from '../../pages/PrintersPage';
import type { Printer, AutoStockSpoolPolicy } from '../../api/client';

const group = { material: 'PETG', rgba: '000000FF', brand: 'Fixture', subtype: 'Basic', filament_family_id: 'GFG99', label_weight: 1000 };
const printer = { id: 77, name: 'Synthetic A', model: 'P1S', nozzle_count: 1, is_active: true, tags: [], tag_ids: [], serial_number: 'SYNTHETIC77', ip_address: '192.0.2.77',
  ams_policies: { backup_compatibility: { normalize_color: true, canonical_color_rgba: '000000FF', generic_base_material: false } } } as unknown as Printer;

function handlers(groups = [{ ...group, available_count: 3 }]) {
  server.use(
    http.get('/api/v1/macros/swap-profiles', () => HttpResponse.json([])),
    http.post('/api/v1/printers/diagnostic', () => HttpResponse.json({ checks: [] })),
    http.get('/api/v1/inventory/spools/auto-stock-groups', () => HttpResponse.json(groups)),
  );
}

describe('Edit Printer — automatic stock assignment', () => {
  it('defaults off, selects a strict group and saves alongside backup policy', async () => {
    handlers();
    const patches: { ams_policies: { auto_stock_spool: AutoStockSpoolPolicy; backup_compatibility: { normalize_color: boolean } } }[] = [];
    server.use(http.patch('/api/v1/printers/77', async ({ request }) => {
      const body = await request.json() as typeof patches[number];
      patches.push(body);
      return HttpResponse.json({ ...printer, ...body });
    }));
    render(<EditPrinterModal printer={printer} onClose={() => {}} />);
    const checkbox = await screen.findByLabelText('Assign a full stock spool on loading');
    expect(checkbox).not.toBeChecked();
    await screen.findByRole('option', { name: /Fixture PETG Basic/ });
    await userEvent.selectOptions(screen.getByLabelText('Inventory filament group'), screen.getByRole('option', { name: /Fixture PETG Basic/ }));
    await userEvent.click(checkbox);
    await userEvent.click(screen.getByText('How automatic assignment works'));
    expect(screen.getByText(/For AMS slots, only fresh local empty/)).toBeVisible();
    expect(screen.getByText(/External holders: after an unambiguous runout/)).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Save Changes' }));
    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0].ams_policies.auto_stock_spool).toEqual({ enabled: true, group });
    expect(patches[0].ams_policies.backup_compatibility.normalize_color).toBe(true);
  });

  it('keeps an exhausted selected group and reports zero full stock', async () => {
    handlers([]);
    render(<EditPrinterModal printer={{ ...printer, ams_policies: { ...printer.ams_policies!, auto_stock_spool: { enabled: true, group } } }} onClose={() => {}} />);
    await screen.findByText(/No full, unused, unassigned stock groups/);
    expect(screen.getByLabelText('Inventory filament group')).toHaveValue(JSON.stringify(Object.values(group)));
    expect(screen.getByRole('option', { name: /0 full in stock/ })).toBeInTheDocument();
    expect(screen.getByLabelText('Assign a full stock spool on loading')).toBeChecked();
  });

  it('requires a group before enabling can be saved', async () => {
    handlers();
    render(<EditPrinterModal printer={printer} onClose={() => {}} />);
    await screen.findByRole('option', { name: /Fixture PETG Basic/ });
    await userEvent.click(screen.getByLabelText('Assign a full stock spool on loading'));
    expect(screen.getByLabelText('Inventory filament group')).toBeRequired();
    expect((screen.getByLabelText('Inventory filament group') as HTMLSelectElement).checkValidity()).toBe(false);
  });
});
