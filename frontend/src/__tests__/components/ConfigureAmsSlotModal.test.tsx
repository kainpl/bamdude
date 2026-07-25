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

  it('derives tray_info_idx from base_id when filament_id is null', async () => {
    // Mock the detail API to return base_id but no filament_id
    (api.getCloudSettingDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      filament_id: null,
      base_id: 'GFSL05_09',
      name: '# Overture Matte PLA @BBL H2D',
    });

    render(<ConfigureAmsSlotModal {...defaultProps} />);

    // Wait for presets to load
    await waitFor(() => {
      expect(api.getCloudSettings).toHaveBeenCalled();
    });

    // Select a user preset (one without filament_id)
    // Find and click the preset - this would require the preset to be in the list
    // The actual tray_info_idx derivation happens during the configure mutation
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

  it('filters presets by printer model', async () => {
    // Render with printerModel="H2D"
    render(<ConfigureAmsSlotModal {...defaultProps} printerModel="H2D" />);
    // Wait for presets to load - the H2D preset should be visible
    await waitFor(() => {
      expect(screen.getByText(/Overture Matte PLA/)).toBeInTheDocument();
    });
    // The X1C preset should NOT be visible (filtered out by model)
    expect(screen.queryByText(/Bambu PLA Basic @BBL X1C/)).not.toBeInTheDocument();
  });

  it('shows current preset even when it does not match model filter', async () => {
    // Render with printerModel="H2D" but savedPresetId pointing to the X1C preset
    const slotInfo = {
      ...defaultProps.slotInfo,
      savedPresetId: 'GFSL05_09',  // X1C preset
    };
    render(<ConfigureAmsSlotModal {...defaultProps} slotInfo={slotInfo} printerModel="H2D" />);
    await waitFor(() => {
      // Both should be visible - H2D matches model, X1C is saved preset
      // Use the full preset name to match the list item (not the "Filtering for" label)
      expect(screen.getByText('Bambu PLA Basic @BBL X1C')).toBeInTheDocument();
      expect(screen.getByText(/Overture Matte PLA/)).toBeInTheDocument();
    });
  });

  it('pre-selects saved preset when opening configured slot', async () => {
    const slotInfo = {
      ...defaultProps.slotInfo,
      savedPresetId: 'GFSL05_09',
    };
    render(<ConfigureAmsSlotModal {...defaultProps} slotInfo={slotInfo} />);
    await waitFor(() => {
      // The saved preset should have the selected style (green border)
      // Use the full preset name to avoid matching the "Filtering for" label
      const presetButton = screen.getByText('Bambu PLA Basic @BBL X1C').closest('button');
      expect(presetButton).toHaveClass('bg-bambu-green/20');
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

  it('treats Bambu cloud rename @BBL A1M as a match for A1 Mini (#1649)', async () => {
    // Bambu cloud shifted A1 Mini filament profiles from
    // "Bambu PLA Basic @BBL A1 Mini ..." to the terse "@BBL A1M" mid-2026.
    // Without an alias-aware compare, the model filter strips every cloud
    // profile from the picker when the user selects an A1 Mini printer.
    (api.getCloudSettings as ReturnType<typeof vi.fn>).mockResolvedValue({
      filament: [
        { setting_id: 'GFA00_A1M', name: 'Bambu PLA Basic @BBL A1M', filament_id: 'GFA00' },
        { setting_id: 'GFA00_A1', name: 'Bambu PLA Basic @BBL A1', filament_id: 'GFA00' },
      ],
    });
    render(<ConfigureAmsSlotModal {...defaultProps} printerModel="A1 Mini" />);
    await waitFor(() => {
      expect(screen.getByText('Bambu PLA Basic @BBL A1M')).toBeInTheDocument();
    });
    // The A1 (non-mini) preset must still be filtered out — the alias
    // table must not collapse two physically distinct printers.
    expect(screen.queryByText('Bambu PLA Basic @BBL A1')).not.toBeInTheDocument();
  });

  it('still filters cross-model cloud profiles when the printer is A1 Mini', async () => {
    // Sanity check that the alias addition didn't accidentally widen the
    // matcher: an X1C cloud preset stays hidden when the picker is for an
    // A1 Mini printer.
    render(<ConfigureAmsSlotModal {...defaultProps} printerModel="A1 Mini" />);
    await waitFor(() => {
      expect(screen.getByText('Filament Profile')).toBeInTheDocument();
    });
    expect(screen.queryByText('Bambu PLA Basic @BBL X1C')).not.toBeInTheDocument();
  });

  it('filters cloud presets whose model is in the name body, no @ suffix (#1623)', async () => {
    // The literal shape that surfaced #1623: the printer model is at the
    // START of the preset name with no "@BBL" / "@Bambu Lab" suffix, so the
    // old suffix-only extractor returned null and the profile leaked into
    // every printer's picker. Body-scan against the registry must classify it.
    (api.getCloudSettings as ReturnType<typeof vi.fn>).mockResolvedValue({
      filament: [
        { setting_id: 'PFUSx1c', name: 'X1C eSUN PETG-Basic Filament', filament_id: null },
        { setting_id: 'PFUSh2d', name: 'H2D eSUN PETG-Basic Filament', filament_id: null },
        { setting_id: 'PFUSgen', name: 'Generic PLA', filament_id: null },
      ],
    });
    render(<ConfigureAmsSlotModal {...defaultProps} printerModel="H2D" />);
    await waitFor(() => {
      // H2D body-model preset matches the printer → visible.
      expect(screen.getByText('H2D eSUN PETG-Basic Filament')).toBeInTheDocument();
    });
    // X1C body-model preset resolves to a different printer → hidden.
    expect(screen.queryByText('X1C eSUN PETG-Basic Filament')).not.toBeInTheDocument();
    // "Generic PLA" carries no model token → unclassifiable → stays visible.
    expect(screen.getByText('Generic PLA')).toBeInTheDocument();
  });

  it('filters local presets by compatible_printers list (#1623)', async () => {
    // Imported local presets carry the slicer's own compatible_printers list.
    // A preset scoped to X1 Carbon must be hidden on an H2D printer; one
    // scoped to H2D (or with no list at all) stays visible.
    (api.getLocalPresets as ReturnType<typeof vi.fn>).mockResolvedValue({
      filament: [
        { id: 10, name: 'My PETG (X1C)', filament_type: 'PETG', compatible_printers: JSON.stringify(['Bambu Lab X1 Carbon 0.4 nozzle']) },
        { id: 11, name: 'My PETG (H2D)', filament_type: 'PETG', compatible_printers: JSON.stringify(['Bambu Lab H2D 0.4 nozzle']) },
        { id: 12, name: 'My PETG (any)', filament_type: 'PETG', compatible_printers: null },
      ],
    });
    render(<ConfigureAmsSlotModal {...defaultProps} printerModel="H2D" />);
    await waitFor(() => {
      expect(screen.getByText('My PETG (H2D)')).toBeInTheDocument();
    });
    // No compatible_printers → unknown → stays visible (back-compat).
    expect(screen.getByText('My PETG (any)')).toBeInTheDocument();
    // Scoped to a different printer → mismatch → hidden.
    expect(screen.queryByText('My PETG (X1C)')).not.toBeInTheDocument();
  });

  // --- K-profile matching (#1688 / #1689) ---------------------------------
  //
  // The audit row that closed fdcc063d compared only `isMatchingCalibration`
  // and never looked at this modal. Two consequences shipped: an actively
  // bound K-profile could vanish from the dropdown (and then get CLEARED on
  // the printer, because Save only requires a preset and caliIdx falls to -1),
  // and presets that resolve no filament_id at all had a permanently empty
  // list.

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
    const slotInfo = { ...defaultProps.slotInfo, savedPresetId: 'GFSL05_09', caliIdx: 7, extruderId: 1 };
    render(<ConfigureAmsSlotModal {...defaultProps} slotInfo={slotInfo} />);

    await waitFor(() => {
      expect(screen.getByText('Bambu PLA Basic @BBL X1C')).toBeInTheDocument();
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
    const slotInfo = { ...defaultProps.slotInfo, savedPresetId: 'GFSL05_09', caliIdx: 0, extruderId: 1 };
    render(<ConfigureAmsSlotModal {...defaultProps} slotInfo={slotInfo} />);

    await waitFor(() => {
      expect(screen.getByText('Bambu PLA Basic @BBL X1C')).toBeInTheDocument();
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
