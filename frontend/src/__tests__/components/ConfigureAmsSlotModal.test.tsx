/**
 * Tests for the ConfigureAmsSlotModal component.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { ConfigureAmsSlotModal } from '../../components/ConfigureAmsSlotModal';
import { api } from '../../api/client';

// Mock the API client
vi.mock('../../api/client', () => ({
  setAuthToken: vi.fn(),
  getAuthToken: vi.fn(() => 'test-admin-token'),
  api: {
    triggerFilamentPresetSync: vi.fn().mockResolvedValue({ queued: true }),
    getFilamentFamilies: vi.fn().mockResolvedValue([
      { filament_id: 'GFG99', ecosystem: 'bambu', alias: 'Generic PETG', vendor: 'Generic', filament_type: 'PETG', origin: 'system' },
      { filament_id: 'GFA00', ecosystem: 'bambu', alias: 'Bambu PLA Basic', vendor: 'Bambu Lab', filament_type: 'PLA', origin: 'system' },
      { filament_id: 'P122e532', ecosystem: 'bambu', alias: 'test PETG Basic', vendor: 'test', filament_type: 'PETG', origin: 'cloud_bambu' },
    ]),
    getSpools: vi.fn().mockResolvedValue([]),
    getCloudSettings: vi.fn(),
    getKProfiles: vi.fn(),
    configureAmsSlot: vi.fn(),
    getCloudSettingDetail: vi.fn(),
    saveSlotPreset: vi.fn(),
    getSettings: vi.fn().mockResolvedValue({}),
    updateSettings: vi.fn().mockResolvedValue({}),
    getLocalPresets: vi.fn(),
    getBuiltinFilaments: vi.fn(),
    searchColors: vi.fn(),
    getColorCatalog: vi.fn(),
    resetAmsSlot: vi.fn(),
    getPrinterModels: vi.fn(),
  },
}));

// Backend PRINTER_MODEL_MAP shape ("Bambu Lab <long>" → short code). Drives
// extractPresetModel's long-form @-suffix + body-scan strategies (#1623) and
// the fullPrinterName derivation for the local-preset compatible_printers filter.
const mockPrinterModels: Record<string, string> = {
  'Bambu Lab X1 Carbon': 'X1C',
  'Bambu Lab H2D': 'H2D',
  'Bambu Lab A1 Mini': 'A1 Mini',
  'Bambu Lab A1': 'A1',
};

const mockCloudSettings = {
  filament: [
    {
      setting_id: 'GFSL05_09',
      name: 'Bambu PLA Basic @BBL X1C',
      filament_id: 'GFL05',
    },
    {
      setting_id: 'PFUScd84f663d2c2ef',
      name: '# Overture Matte PLA @BBL H2D',
      filament_id: null,
    },
  ],
};

const mockKProfiles = {
  profiles: [
    {
      id: 1,
      name: 'PLA Basic',
      k_value: '0.020',
      filament_id: 'GFL05',
      setting_id: '',
      extruder_id: 1,
      cali_idx: 1,
    },
  ],
};

const defaultProps = {
  isOpen: true,
  onClose: vi.fn(),
  printerId: 1,
  slotInfo: {
    amsId: 0,
    trayId: 0,
    trayCount: 4,
    trayType: 'PLA',
    trayColor: 'FFFFFF',
    traySubBrands: 'PLA Basic',
  },
  nozzleDiameter: '0.4',
  onSuccess: vi.fn(),
};

describe('ConfigureAmsSlotModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Mock scrollIntoView which is not available in jsdom
    Element.prototype.scrollIntoView = vi.fn();
    (api.getCloudSettings as ReturnType<typeof vi.fn>).mockResolvedValue(mockCloudSettings);
    (api.getKProfiles as ReturnType<typeof vi.fn>).mockResolvedValue(mockKProfiles);
    (api.configureAmsSlot as ReturnType<typeof vi.fn>).mockResolvedValue({ success: true });
    (api.saveSlotPreset as ReturnType<typeof vi.fn>).mockResolvedValue({ success: true });
    (api.getLocalPresets as ReturnType<typeof vi.fn>).mockResolvedValue({ filament: [] });
    (api.getBuiltinFilaments as ReturnType<typeof vi.fn>).mockResolvedValue([]);
    (api.searchColors as ReturnType<typeof vi.fn>).mockResolvedValue([]);
    (api.getColorCatalog as ReturnType<typeof vi.fn>).mockResolvedValue([]);
    (api.resetAmsSlot as ReturnType<typeof vi.fn>).mockResolvedValue({ success: true, message: 'ok' });
    (api.getPrinterModels as ReturnType<typeof vi.fn>).mockResolvedValue(mockPrinterModels);
  });

  it('renders nothing visible when closed', () => {
    render(<ConfigureAmsSlotModal {...defaultProps} isOpen={false} />);
    expect(screen.queryByText('Configure AMS Slot')).not.toBeInTheDocument();
  });

  it('renders modal when open', async () => {
    render(<ConfigureAmsSlotModal {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByText(/Configure AMS/)).toBeInTheDocument();
    });
  });

  it('displays basic color buttons', async () => {
    render(<ConfigureAmsSlotModal {...defaultProps} />);
    await waitFor(() => {
      // Check for basic color buttons by their title attribute
      expect(screen.getByTitle('White')).toBeInTheDocument();
      expect(screen.getByTitle('Black')).toBeInTheDocument();
      expect(screen.getByTitle('Red')).toBeInTheDocument();
      expect(screen.getByTitle('Blue')).toBeInTheDocument();
      expect(screen.getByTitle('Green')).toBeInTheDocument();
      expect(screen.getByTitle('Yellow')).toBeInTheDocument();
      expect(screen.getByTitle('Orange')).toBeInTheDocument();
      expect(screen.getByTitle('Gray')).toBeInTheDocument();
    });
  });

  it('does not show extended colors by default', async () => {
    render(<ConfigureAmsSlotModal {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByTitle('White')).toBeInTheDocument();
    });
    // Extended colors should not be visible initially
    expect(screen.queryByTitle('Cyan')).not.toBeInTheDocument();
    expect(screen.queryByTitle('Purple')).not.toBeInTheDocument();
    expect(screen.queryByTitle('Coral')).not.toBeInTheDocument();
  });

  it('shows extended colors when expand button is clicked', async () => {
    render(<ConfigureAmsSlotModal {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByTitle('White')).toBeInTheDocument();
    });

    // Click the expand button (+ button)
    const expandButton = screen.getByTitle('Show more colors');
    fireEvent.click(expandButton);

    // Extended colors should now be visible
    await waitFor(() => {
      expect(screen.getByTitle('Cyan')).toBeInTheDocument();
      expect(screen.getByTitle('Purple')).toBeInTheDocument();
      expect(screen.getByTitle('Pink')).toBeInTheDocument();
      expect(screen.getByTitle('Brown')).toBeInTheDocument();
      expect(screen.getByTitle('Coral')).toBeInTheDocument();
    });
  });

  it('hides extended colors when collapse button is clicked', async () => {
    render(<ConfigureAmsSlotModal {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByTitle('White')).toBeInTheDocument();
    });

    // Click the expand button
    const expandButton = screen.getByTitle('Show more colors');
    fireEvent.click(expandButton);

    // Wait for extended colors to appear
    await waitFor(() => {
      expect(screen.getByTitle('Cyan')).toBeInTheDocument();
    });

    // Click the collapse button
    const collapseButton = screen.getByTitle('Show less colors');
    fireEvent.click(collapseButton);

    // Extended colors should be hidden again
    await waitFor(() => {
      expect(screen.queryByTitle('Cyan')).not.toBeInTheDocument();
    });
  });

  it('selects a color when color button is clicked', async () => {
    render(<ConfigureAmsSlotModal {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByTitle('Red')).toBeInTheDocument();
    });

    // Click the red color button
    const redButton = screen.getByTitle('Red');
    fireEvent.click(redButton);

    // The color input should now show "Red"
    const colorInput = screen.getByPlaceholderText(/Color name or hex/);
    expect(colorInput).toHaveValue('Red');
  });

  it('lists families with cloud badges and sends the family id on configure', async () => {
    render(<ConfigureAmsSlotModal {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByText('test PETG Basic')).toBeInTheDocument();
    });
    // Cloud-originated custom family carries the Bambu Cloud source badge.
    expect(screen.getAllByText('Bambu Cloud').length).toBeGreaterThan(0);

    fireEvent.click(screen.getByText('test PETG Basic'));
    fireEvent.click(screen.getByRole('button', { name: /Configure Slot/i }));
    await waitFor(() => {
      expect(api.configureAmsSlot).toHaveBeenCalled();
    });
    const [, , , payload] = (api.configureAmsSlot as ReturnType<typeof vi.fn>).mock.calls[0];
    // The FAMILY id goes out as tray_info_idx — the backend builder resolves
    // the versioned setting_id / temps / type from the catalog.
    expect(payload.tray_info_idx).toBe('P122e532');
    expect(payload.setting_id).toBe('');
  });

  it('renders configure slot button', async () => {
    render(<ConfigureAmsSlotModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByText(/Configure AMS/)).toBeInTheDocument();
    });

    // Find the Configure Slot button
    const configureButton = screen.getByRole('button', { name: /Configure Slot/i });
    expect(configureButton).toBeInTheDocument();
  });

  it('pre-selects the family the tray itself reports', async () => {
    const slotInfo = {
      ...defaultProps.slotInfo,
      trayInfoIdx: 'GFA00',
    };
    render(<ConfigureAmsSlotModal {...defaultProps} slotInfo={slotInfo} />);
    await waitFor(() => {
      const familyButton = screen.getByText('Bambu PLA Basic').closest('button');
      expect(familyButton).toHaveClass('bg-bambu-green/20');
    });
  });

  it('pre-populates color from trayColor', async () => {
    const slotInfo = {
      ...defaultProps.slotInfo,
      trayColor: 'FF0000FF',  // Red with alpha
    };
    render(<ConfigureAmsSlotModal {...defaultProps} slotInfo={slotInfo} />);
    await waitFor(() => {
      expect(screen.getByTitle('White')).toBeInTheDocument();
    });
    // The hex display should show the pre-populated color
    expect(screen.getByText('Hex: #FF0000', { exact: false })).toBeInTheDocument();
  });

  it('uses translated text for modal elements', async () => {
    render(<ConfigureAmsSlotModal {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByText('Configure AMS Slot')).toBeInTheDocument();
      expect(screen.getByText('Filament Profile')).toBeInTheDocument();
    });
    // Check footer buttons
    expect(screen.getByRole('button', { name: /Configure Slot/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Cancel/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Reset Slot/i })).toBeInTheDocument();
  });

  it('search narrows the family list', async () => {
    render(<ConfigureAmsSlotModal {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByText('Generic PETG')).toBeInTheDocument();
    });
    // Families are model-agnostic (the backend picks the per-printer preset),
    // so there is no model filter to test — searching filters the list.
    fireEvent.change(screen.getByPlaceholderText(/Search/i), { target: { value: 'PLA' } });
    await waitFor(() => {
      expect(screen.queryByText('Generic PETG')).not.toBeInTheDocument();
    });
    await waitFor(() => {
      expect(screen.getByText('Bambu PLA Basic')).toBeInTheDocument();
    });
  });

  it('surfaces the bound K-profile of a slot even when it matches neither by id nor name (#1689)', async () => {
    // Profile 7 is bound on the printer (cali_idx 7) but carries a different
    // filament_id AND an unrelated name — under the old id-only filter the
    // dropdown came up empty and Save would have written cali_idx: -1.
    (api.getKProfiles as ReturnType<typeof vi.fn>).mockResolvedValue({
      profiles: [
        { id: 1, name: 'PLA Basic', k_value: '0.020', filament_id: 'GFL05', setting_id: '', extruder_id: 1, cali_idx: 1, slot_id: 1 },
        { id: 7, name: 'Sunlu custom blend', k_value: '0.031', filament_id: 'GFG99', setting_id: '', extruder_id: 1, cali_idx: 7, slot_id: 7 },
      ],
    });
    const slotInfo = { ...defaultProps.slotInfo, trayInfoIdx: 'GFA00', caliIdx: 7, extruderId: 1 };
    render(<ConfigureAmsSlotModal {...defaultProps} slotInfo={slotInfo} />);

    await waitFor(() => {
      expect(screen.getByText('Bambu PLA Basic')).toBeInTheDocument();
    });
    // The bound profile is present despite matching nothing about the preset.
    await waitFor(() => {
      expect(screen.getAllByText(/Sunlu custom blend/).length).toBeGreaterThan(0);
    });
  });

  it('does not inject an unrelated profile when nothing is bound (caliIdx 0/absent)', async () => {
    (api.getKProfiles as ReturnType<typeof vi.fn>).mockResolvedValue({
      profiles: [
        { id: 7, name: 'Sunlu custom blend', k_value: '0.031', filament_id: 'GFG99', setting_id: '', extruder_id: 1, cali_idx: 7, slot_id: 7 },
      ],
    });
    const slotInfo = { ...defaultProps.slotInfo, trayInfoIdx: 'GFA00', caliIdx: 0, extruderId: 1 };
    render(<ConfigureAmsSlotModal {...defaultProps} slotInfo={slotInfo} />);

    await waitFor(() => {
      expect(screen.getByText('Bambu PLA Basic')).toBeInTheDocument();
    });
    expect(screen.queryByText(/Sunlu custom blend/)).not.toBeInTheDocument();
  });

  it('shows the bound K-profile even when no preset resolves at all (#1689 follow-up)', async () => {
    // Audit row D1 of the same cycle: a physically-loaded but unconfigured
    // slot resolves no preset, so the old `!targetFilamentId` guard returned
    // an empty list and the modal claimed "no profile, default 0.020" while
    // the printer had one bound. The guard no longer short-circuits, so the
    // safety net still runs.
    (api.getKProfiles as ReturnType<typeof vi.fn>).mockResolvedValue({
      profiles: [
        { id: 7, name: 'Sunlu custom blend', k_value: '0.031', filament_id: 'GFG99', setting_id: '', extruder_id: 1, cali_idx: 7, slot_id: 7 },
      ],
    });
    const slotInfo = { ...defaultProps.slotInfo, caliIdx: 7, extruderId: 1 };
    render(<ConfigureAmsSlotModal {...defaultProps} slotInfo={slotInfo} />);

    await waitFor(() => {
      expect(screen.getAllByText(/Sunlu custom blend/).length).toBeGreaterThan(0);
    });
  });
});
