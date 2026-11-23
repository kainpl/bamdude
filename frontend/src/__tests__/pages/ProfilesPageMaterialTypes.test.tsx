import { useEffect } from 'react';
import { useQueryClient, type QueryClient } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { ProfilesPage } from '../../pages/ProfilesPage';
import { server } from '../mocks/server';
import { act, render, screen, waitFor } from '../utils';

const base = '*/api/v1';
const custom = { setting_id: 'PFUS-types-1', name: 'My Custom Type @BBL X1C', type: 'filament', is_custom: true };
const builtin = { setting_id: 'GFSA00', name: 'Bambu PLA Basic @BBL X1C', type: 'filament', is_custom: false };
const importedType = ['HSPLA+2.0'];
let slicer: 'bambu_studio' | 'orcaslicer';
let queryClient: QueryClient;

function CaptureQueryClient() {
  const client = useQueryClient();
  useEffect(() => { queryClient = client; }, [client]);
  return null;
}

function materialSelect() {
  return screen.getByText('Filament Type').closest('div')!.parentElement!.querySelector('select')!;
}

const options = () => [...materialSelect().options].map((option) => option.value).filter(Boolean);

beforeEach(() => {
  slicer = 'bambu_studio';
  server.use(
    http.get(`${base}/cloud/status`, () =>
      HttpResponse.json({ is_authenticated: true, email: 'user@example.com', region: 'global' }),
    ),
    http.get(`${base}/printers/`, () => HttpResponse.json([])),
    http.get(`${base}/slicer/printer-models`, () => HttpResponse.json({})),
    http.get(`${base}/settings/ui-preferences`, () => HttpResponse.json({ preferred_slicer: slicer })),
    http.get(`${base}/cloud/settings`, () =>
      HttpResponse.json({ filament: [custom, builtin], printer: [], process: [] }),
    ),
    http.get(`${base}/cloud/settings/:id`, ({ params }) => HttpResponse.json({
      setting_id: params.id,
      name: params.id === custom.setting_id ? custom.name : builtin.name,
      base_id: builtin.setting_id,
      setting: params.id === custom.setting_id
        ? { inherits: builtin.name, filament_type: importedType }
        : { filament_type: 'PLA' },
    })),
    http.get(`${base}/cloud/fields/filament`, () => HttpResponse.json({
      version: '1',
      fields: [{
        key: 'filament_type',
        label: 'Filament Type',
        type: 'select',
        category: 'basic',
        options: ['PLA', slicer === 'orcaslicer' ? 'SBS' : 'TPU-AMS'].map((value) => ({ value, label: value })),
      }],
    })),
  );
});

describe('Profiles material types', () => {
  it('refreshes choices for the preferred slicer and keeps the selected value', async () => {
    const user = userEvent.setup();
    let createdType: unknown;
    server.use(http.post(`${base}/cloud/settings`, async ({ request }) => {
      const body = await request.json() as { setting: Record<string, unknown> };
      createdType = body.setting.filament_type;
      return HttpResponse.json({ setting_id: 'PFUS-types-new' });
    }));
    render(<><ProfilesPage /><CaptureQueryClient /></>);
    await user.click(await screen.findByRole('button', { name: 'New Preset' }));
    await screen.findByText('Create New Preset');
    const baseSelect = screen.getByText('Base Preset').parentElement!.querySelector('select')!;
    await user.selectOptions(baseSelect, builtin.setting_id);
    await waitFor(() => expect(options()).toEqual(['PLA', 'TPU-AMS']));
    await user.selectOptions(materialSelect(), 'TPU-AMS');

    slicer = 'orcaslicer';
    act(() => { queryClient.setQueryData(['ui-preferences'], { preferred_slicer: slicer }); });
    await waitFor(() => expect(options()).toEqual(['TPU-AMS', 'PLA', 'SBS']));
    expect(materialSelect()).toHaveValue('TPU-AMS');
    await user.selectOptions(materialSelect(), 'SBS');
    const nameInput = screen.getByText('Preset Name').parentElement!.querySelector('input')!;
    await user.type(nameInput, 'Orca material preset');
    await user.click(screen.getByRole('button', { name: 'Create' }));
    await waitFor(() => expect(createdType).toBe('SBS'));
  });

  it('shows an imported type outside the list and saves its original value shape', async () => {
    const user = userEvent.setup();
    let savedType: unknown;
    server.use(http.put(`${base}/cloud/settings/${custom.setting_id}`, async ({ request }) => {
      const body = await request.json() as { setting: Record<string, unknown> };
      savedType = body.setting.filament_type;
      return HttpResponse.json({ success: true });
    }));
    render(<ProfilesPage />);

    await user.click(await screen.findByText(custom.name));
    const edit = await screen.findByRole('button', { name: 'Edit' });
    await waitFor(() => expect(edit).not.toBeDisabled());
    await user.click(edit);

    await waitFor(() => expect(materialSelect()).toHaveValue(importedType[0]));
    expect(options()).toEqual([importedType[0], 'PLA', 'TPU-AMS']);
    await user.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(savedType).toEqual(importedType));
  });
});
