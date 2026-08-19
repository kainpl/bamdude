/**
 * Tests for the VirtualPrinterCard component.
 *
 * Tests the auto-dispatch toggle behavior:
 * - Visibility based on mode (print_queue only)
 * - Default state (on)
 * - API mutation on toggle click
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { VirtualPrinterCard } from '../../components/VirtualPrinterCard';
import type { VirtualPrinterConfig } from '../../api/client';

// Mock the API client
vi.mock('../../api/client', () => ({
  setAuthToken: vi.fn(),
  getAuthToken: vi.fn(() => 'test-admin-token'),
  multiVirtualPrinterApi: {
    update: vi.fn().mockResolvedValue({}),
    remove: vi.fn().mockResolvedValue({}),
  },
  api: {
    getSettings: vi.fn().mockResolvedValue({}),
    getPrinters: vi.fn().mockResolvedValue([]),
    getNetworkInterfaces: vi.fn().mockResolvedValue({ interfaces: [] }),
  },
}));

import { multiVirtualPrinterApi } from '../../api/client';

const models: Record<string, string> = {
  'BL-P001': 'X1C',
  'C12': 'P1S',
};

const createMockPrinter = (overrides: Partial<VirtualPrinterConfig> = {}): VirtualPrinterConfig => ({
  id: 1,
  name: 'Test VP',
  enabled: false,
  mode: 'file_manager',
  model: 'BL-P001',
  model_name: 'X1C',
  access_code_set: false,
  serial: '00M00A391800001',
  target_printer_id: null,
  target_folder_id: null,
  auto_dispatch: true,
  queue_force_color_match: false,
  gcode_injection: false,
  bind_ip: null,
  remote_interface_ip: null,
  tailscale_disabled: true,
  position: 0,
  status: { running: false, pending_files: 0 },
  ...overrides,
});

describe('VirtualPrinterCard - auto-dispatch toggle', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(multiVirtualPrinterApi.update).mockResolvedValue(createMockPrinter());
  });

  it('renders auto-dispatch toggle when mode is print_queue', async () => {
    const printer = createMockPrinter({ mode: 'print_queue' });
    render(<VirtualPrinterCard printer={printer} models={models} />);

    await waitFor(() => {
      expect(screen.getByText('Auto-dispatch')).toBeInTheDocument();
    });
  });

  it('does not render auto-dispatch toggle when mode is immediate', async () => {
    const printer = createMockPrinter({ mode: 'file_manager' });
    render(<VirtualPrinterCard printer={printer} models={models} />);

    // Wait for the card to render fully (check for something that should be there)
    await waitFor(() => {
      expect(screen.getByText('Test VP')).toBeInTheDocument();
    });

    expect(screen.queryByText('Auto-dispatch')).not.toBeInTheDocument();
  });

  it('does not render auto-dispatch toggle when mode is proxy', async () => {
    const printer = createMockPrinter({ mode: 'proxy' });
    render(<VirtualPrinterCard printer={printer} models={models} />);

    await waitFor(() => {
      expect(screen.getByText('Test VP')).toBeInTheDocument();
    });

    expect(screen.queryByText('Auto-dispatch')).not.toBeInTheDocument();
  });

  it('auto-dispatch toggle defaults to on', async () => {
    const printer = createMockPrinter({ mode: 'print_queue', auto_dispatch: true });
    render(<VirtualPrinterCard printer={printer} models={models} />);

    await waitFor(() => {
      expect(screen.getByText('Auto-dispatch')).toBeInTheDocument();
    });

    // The auto-dispatch section container has the toggle button as a sibling of the text div
    const title = screen.getByText('Auto-dispatch');
    const section = title.closest('.flex.items-center.justify-between');
    expect(section).toBeTruthy();
    const toggleButton = section!.querySelector('button');
    expect(toggleButton).toBeTruthy();
    expect(toggleButton!.className).toContain('bg-bambu-green');
  });

  it('clicking auto-dispatch toggle calls update API', async () => {
    const user = userEvent.setup();
    const printer = createMockPrinter({ mode: 'print_queue', auto_dispatch: true });
    vi.mocked(multiVirtualPrinterApi.update).mockResolvedValue(
      createMockPrinter({ mode: 'print_queue', auto_dispatch: false })
    );

    render(<VirtualPrinterCard printer={printer} models={models} />);

    await waitFor(() => {
      expect(screen.getByText('Auto-dispatch')).toBeInTheDocument();
    });

    // Find the auto-dispatch toggle via the section container
    const title = screen.getByText('Auto-dispatch');
    const section = title.closest('.flex.items-center.justify-between');
    expect(section).toBeTruthy();
    const toggleButton = section!.querySelector('button');
    expect(toggleButton).toBeTruthy();

    await user.click(toggleButton!);

    await waitFor(() => {
      expect(multiVirtualPrinterApi.update).toHaveBeenCalledWith(1, { auto_dispatch: false });
    });
  });
});

describe('VirtualPrinterCard - collapsed header layout', () => {
  beforeEach(() => vi.clearAllMocks());

  /** The metadata group: name, mode, model, target, and the two addresses. */
  function metadataGroup(container: HTMLElement): HTMLElement {
    return container.querySelector('.flex-wrap') as HTMLElement;
  }

  it('lets the metadata wrap instead of widening the row', async () => {
    // ⚠️ Every item in this row used to be flex-shrink-0, so nothing could
    // give and the row was as wide as its contents — which Card does not clip,
    // so the last address and the enable toggle painted outside the border.
    const printer = createMockPrinter({
      name: 'Printer at 192.168.10.240',
      bind_ip: '192.168.10.5',
      remote_interface_ip: '192.168.20.5',
    });
    const { container } = render(<VirtualPrinterCard printer={printer} models={models} />);

    await waitFor(() => expect(screen.getByText('192.168.20.5')).toBeInTheDocument());

    const group = metadataGroup(container);
    expect(group).not.toBeNull();
    expect(group.className).toContain('flex-1');
    // ⚠️ min-w-0 is what makes `truncate` able to engage at all: a flex item
    // defaults to min-width:auto and cannot shrink below its own text.
    expect(group.className).toContain('min-w-0');
  });

  it('keeps both addresses rather than truncating one away', async () => {
    // These are the values an operator opened this page to check, so hiding
    // one behind an ellipsis would trade the bug for a quieter one.
    const printer = createMockPrinter({
      bind_ip: '192.168.10.5',
      remote_interface_ip: '192.168.20.5',
    });
    render(<VirtualPrinterCard printer={printer} models={models} />);

    await waitFor(() => expect(screen.getByText('192.168.10.5')).toBeInTheDocument());
    expect(screen.getByText('192.168.20.5')).toBeInTheDocument();
  });
});
