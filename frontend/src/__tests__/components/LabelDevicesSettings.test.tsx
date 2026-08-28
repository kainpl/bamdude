/**
 * Settings → Printing → Label printers.
 *
 * ⚠️ The one behaviour worth pinning here is that a device arrives switched
 * off. Everything else on this screen is display; that one is a decision about
 * whether a machine in another room may receive our labels.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { LabelDevicesSettings } from '../../components/settings/LabelDevicesSettings';
import { api, type LabelDevice } from '../../api/client';

vi.mock('../../contexts/ToastContext', () => ({
  useToast: () => ({ showToast: vi.fn() }),
}));

vi.mock('react-i18next', () => ({
  // The key itself is the label, so an assertion names the key rather than a
  // translation that could be reworded without breaking anything.
  useTranslation: () => ({ t: (key: string) => key }),
}));

function device(overrides: Partial<LabelDevice> = {}): LabelDevice {
  return {
    id: 1,
    installation_id: '11111111-2222-3333-4444-555555555555',
    driver: 'niimbot',
    model: 'B1',
    protocol_version: 3,
    transport: 'serial',
    address: 'COM6',
    name: null,
    enabled: false,
    density: 3,
    app_version: '0.1.0',
    last_seen_at: null,
    cassette_barcode: null,
    cassette_width_mm: null,
    cassette_height_mm: null,
    paper_state: 1,
    power_level: 3,
    printer_reachable: true,
    queued: 0,
    ...overrides,
  };
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <LabelDevicesSettings />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, 'getLabelCassettes').mockResolvedValue([]);
  vi.spyOn(api, 'getLabelJobs').mockResolvedValue([]);
});

describe('LabelDevicesSettings', () => {
  it('asks nothing about devices while the subsystem is off', async () => {
    vi.spyOn(api, 'getSettings').mockResolvedValue({ device_labels_enabled: false } as never);
    const devices = vi.spyOn(api, 'getLabelDevices').mockResolvedValue([]);

    renderPanel();

    await screen.findByText('labelDevices.offHint');
    // A farm with no bridge should not be asking a question whose answer is
    // always the same empty list.
    expect(devices).not.toHaveBeenCalled();
  });

  it('lists a device that has introduced itself as waiting for approval', async () => {
    vi.spyOn(api, 'getSettings').mockResolvedValue({ device_labels_enabled: true } as never);
    vi.spyOn(api, 'getLabelDevices').mockResolvedValue([device()]);

    renderPanel();

    await screen.findByText('labelDevices.waitingHeading');
    expect(screen.getByText('labelDevices.enable')).toBeInTheDocument();
    expect(screen.queryByText('labelDevices.disable')).not.toBeInTheDocument();
  });

  it('shows the installation id, which is how somebody matches the machine', async () => {
    vi.spyOn(api, 'getSettings').mockResolvedValue({ device_labels_enabled: true } as never);
    vi.spyOn(api, 'getLabelDevices').mockResolvedValue([device()]);

    renderPanel();

    await screen.findByText('11111111-2222-3333-4444-555555555555');
  });

  it('adopting a device is an explicit act, not a side effect of it appearing', async () => {
    vi.spyOn(api, 'getSettings').mockResolvedValue({ device_labels_enabled: true } as never);
    vi.spyOn(api, 'getLabelDevices').mockResolvedValue([device()]);
    const update = vi.spyOn(api, 'updateLabelDevice').mockResolvedValue(device({ enabled: true }));

    renderPanel();
    await userEvent.click(await screen.findByText('labelDevices.enable'));

    await waitFor(() => expect(update).toHaveBeenCalledWith(1, { enabled: true }));
  });

  it('separates a bridge that answers from a printer that answers', async () => {
    // ⚠️ Two different failures with two different fixes: the desktop process
    // can be up while the USB cable is out, and only one of those is the
    // operator's to fix here.
    vi.spyOn(api, 'getSettings').mockResolvedValue({ device_labels_enabled: true } as never);
    vi.spyOn(api, 'getLabelDevices').mockResolvedValue([
      device({ enabled: true, printer_reachable: false }),
    ]);

    renderPanel();

    await screen.findByText(/labelDevices\.printerUnreachable/);
  });

  it('says a cassette size is unknown rather than inventing one', async () => {
    vi.spyOn(api, 'getSettings').mockResolvedValue({ device_labels_enabled: true } as never);
    vi.spyOn(api, 'getLabelDevices').mockResolvedValue([
      device({ enabled: true, cassette_barcode: '6972842748577' }),
    ]);

    const { container } = renderPanel();

    // The line is built from two nodes, so it is the rendered text that has to
    // carry the claim — findByText only ever sees one element at a time.
    await waitFor(() =>
      expect(container.textContent).toContain('labelDevices.cassetteUnknown'),
    );
  });

  it('offers a loaded barcode nobody has taught yet', async () => {
    // It is exactly what is blocking that printer from being used.
    vi.spyOn(api, 'getSettings').mockResolvedValue({ device_labels_enabled: true } as never);
    vi.spyOn(api, 'getLabelDevices').mockResolvedValue([
      device({ enabled: true, cassette_barcode: '6972842748577' }),
    ]);

    renderPanel();

    await screen.findByText(/labelDevices\.cassetteUnknownLoaded/);
  });
});
