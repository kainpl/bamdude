import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api/client';
import { SpoolmanSettings } from '../../components/SpoolmanSettings';
import { ToastProvider } from '../../contexts/ToastContext';

const actor = vi.hoisted(() => ({ isAdmin: false }));
vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ isAdmin: actor.isAdmin, hasPermission: () => true }),
}));

const panel = (client: QueryClient) => <QueryClientProvider client={client}><ToastProvider><SpoolmanSettings /></ToastProvider></QueryClientProvider>;

describe('G01 A delegated Spoolman configuration', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    actor.isAdmin = false;
    const settings = {
      spoolman_enabled: 'true', spoolman_url: 'https://spool.example', spoolman_sync_mode: 'auto',
      spoolman_disable_weight_sync: 'true', spoolman_report_partial_usage: 'true', auto_add_unknown_rfid: 'false',
    };
    vi.spyOn(api, 'getSpoolmanSettings').mockResolvedValue(settings);
    vi.spyOn(api, 'getSpoolmanStatus').mockResolvedValue({ enabled: true, connected: false, url: 'https://spool.example' });
    vi.spyOn(api, 'getPrinters').mockResolvedValue([]);
    vi.spyOn(api, 'getSpoolmanModeSwitchPreview').mockResolvedValue({ assignments: 0, printing: [] });
    vi.spyOn(api, 'updateSpoolmanSettings').mockResolvedValue(settings);
    vi.spyOn(api, 'connectSpoolman').mockResolvedValue({ success: true, message: 'Connected' });
  });

  it('disables every config control and performs no write/probe/autosave after attempted delegated edits', async () => {
    const user = userEvent.setup();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    render(panel(client));
    const url = await screen.findByDisplayValue('https://spool.example');
    const toggles = screen.getAllByRole('checkbox');
    expect(toggles).toHaveLength(3);
    const modes = [screen.getByText('Built-in Inventory').closest('button')!, screen.getByText('Spoolman').closest('button')!];
    const controls = [...toggles, ...modes, url, screen.getByRole('combobox'), screen.getByRole('button', { name: 'Connect' })];
    for (const control of controls) expect(control).toBeDisabled();
    for (const control of controls) await user.click(control);
    await act(() => new Promise(resolve => setTimeout(resolve, 650)));
    expect(api.updateSpoolmanSettings).not.toHaveBeenCalled();
    expect(api.connectSpoolman).not.toHaveBeenCalled();
    expect(api.getSpoolmanModeSwitchPreview).not.toHaveBeenCalled();
  });

  it('cancels a queued admin autosave when the actor becomes delegated before the debounce ends', async () => {
    actor.isAdmin = true;
    const user = userEvent.setup();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    const { rerender } = render(panel(client));
    const url = await screen.findByDisplayValue('https://spool.example');
    await waitFor(() => expect(url).not.toBeDisabled());
    await user.type(url, '/changed');
    actor.isAdmin = false;
    rerender(panel(client));
    expect(url).toBeDisabled();
    await act(() => new Promise(resolve => setTimeout(resolve, 650)));
    expect(api.updateSpoolmanSettings).not.toHaveBeenCalled();
  });
});
