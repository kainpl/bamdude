/**
 * "+ Add to inventory" chooses its route from the Spoolman status — readable
 * with `inventory:read` — not from the settings cache: an operator without
 * `settings:read`, or a session in which nothing had loaded the settings yet,
 * read "no settings" as the built-in inventory and wrote a built-in spool and
 * slot row in Spoolman mode (review slots I-4).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { useUnknownTagPrompt } from '../../hooks/useUnknownTagPrompt';
import { api } from '../../api/client';

vi.mock('../../api/client', () => ({
  api: {
    getSpoolmanStatus: vi.fn(),
    createSpoolmanSpoolFromSlot: vi.fn().mockResolvedValue({}),
    createSpoolFromSlot: vi.fn().mockResolvedValue({}),
  },
}));

const showToast = vi.fn();
vi.mock('../../contexts/ToastContext', () => ({ useToast: () => ({ showToast }) }));
vi.mock('../../contexts/AuthContext', () => ({ useAuth: () => ({ user: { id: 1 }, authEnabled: true }) }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

const SLOT = { printer_id: 3, ams_id: 0, tray_id: 1 };

function setup(seed?: (client: QueryClient) => void) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  seed?.(client);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(() => useUnknownTagPrompt(), { wrapper });
}

async function promptAndConfirm(result: { current: ReturnType<typeof useUnknownTagPrompt> }) {
  act(() => {
    window.dispatchEvent(new CustomEvent('unknown-tag', { detail: { ...SLOT, tray_type: 'PLA' } }));
  });
  await waitFor(() => expect(result.current.prompt).not.toBeNull());
  act(() => result.current.confirm());
}

describe('useUnknownTagPrompt — which inventory the spool is added to', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('adds to Spoolman when the status says Spoolman is the inventory, with no settings loaded', async () => {
    vi.mocked(api.getSpoolmanStatus).mockResolvedValue({ enabled: true, connected: true } as never);
    const { result } = setup();

    await promptAndConfirm(result);

    await waitFor(() => expect(api.createSpoolmanSpoolFromSlot).toHaveBeenCalledWith(SLOT));
    expect(api.createSpoolFromSlot).not.toHaveBeenCalled();
  });

  it('still adds to Spoolman while it is enabled but unreachable', async () => {
    vi.mocked(api.getSpoolmanStatus).mockResolvedValue({ enabled: true, connected: false } as never);
    const { result } = setup();

    await promptAndConfirm(result);

    await waitFor(() => expect(api.createSpoolmanSpoolFromSlot).toHaveBeenCalled());
    expect(api.createSpoolFromSlot).not.toHaveBeenCalled();
  });

  it('adds to the built-in inventory when Spoolman is off', async () => {
    vi.mocked(api.getSpoolmanStatus).mockResolvedValue({ enabled: false, connected: false } as never);
    // A settings cache that disagrees is not what decides.
    const { result } = setup(client => client.setQueryData(['settings'], { spoolman_enabled: true }));

    await promptAndConfirm(result);

    await waitFor(() => expect(api.createSpoolFromSlot).toHaveBeenCalledWith(SLOT));
    expect(api.createSpoolmanSpoolFromSlot).not.toHaveBeenCalled();
  });

  it('adds nowhere when the status cannot be read', async () => {
    vi.mocked(api.getSpoolmanStatus).mockRejectedValue(new Error('offline'));
    const { result } = setup();

    await promptAndConfirm(result);

    await waitFor(() => expect(showToast).toHaveBeenCalledWith('offline', 'error'));
    expect(api.createSpoolFromSlot).not.toHaveBeenCalled();
    expect(api.createSpoolmanSpoolFromSlot).not.toHaveBeenCalled();
  });
});
