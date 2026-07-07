/**
 * Tests for the LabelTemplatePickerModal.
 *
 * BamDude posts ``{ spools: [{ id, display_name }], template, monochrome }``
 * (the ``spools`` object shape carries the per-spool display-name override —
 * see client.ts ``SpoolLabelRequest``), which diverges from upstream's flat
 * ``spool_ids`` payload. These tests focus on the #1870 monochrome toggle:
 * the checkbox threads a boolean through to the render request, defaulting off.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { render } from '../utils';
import { LabelTemplatePickerModal } from '../../components/LabelTemplatePickerModal';
import type { InventorySpool } from '../../api/client';

vi.mock('../../api/client', () => ({
  setAuthToken: vi.fn(),
  getAuthToken: vi.fn(() => 'test-admin-token'),
  api: {
    printSpoolLabels: vi.fn(),
    printSpoolmanSpoolLabels: vi.fn(),
    getSettings: vi.fn().mockResolvedValue({}),
    getAuthStatus: vi.fn().mockResolvedValue({ auth_enabled: false }),
  },
}));

import { api } from '../../api/client';

const PDF_BLOB = new Blob([new Uint8Array([0x25, 0x50, 0x44, 0x46])], { type: 'application/pdf' });

const SPOOLS = [
  { id: 1, material: 'PLA', subtype: 'Basic', brand: 'Polymaker', color_name: 'Red', rgba: 'FF0000FF' },
  { id: 2, material: 'PETG', subtype: null, brand: 'Sunlu', color_name: 'Blue', rgba: '0000FFFF' },
] as unknown as InventorySpool[];

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.printSpoolLabels).mockResolvedValue(PDF_BLOB);
  vi.mocked(api.printSpoolmanSpoolLabels).mockResolvedValue(PDF_BLOB);
  Object.defineProperty(window.URL, 'createObjectURL', {
    value: vi.fn(() => 'blob:mock'),
    configurable: true,
  });
  Object.defineProperty(window.URL, 'revokeObjectURL', {
    value: vi.fn(),
    configurable: true,
  });
  vi.spyOn(window, 'open').mockImplementation(() => ({}) as Window);
});

describe('LabelTemplatePickerModal', () => {
  it('renders the monochrome toggle when open', () => {
    render(
      <LabelTemplatePickerModal
        isOpen={true}
        onClose={vi.fn()}
        availableSpools={SPOOLS}
        initialSelectedIds={[1]}
        spoolmanMode={false}
      />,
    );
    expect(screen.getByText(/Print spool labels/i)).toBeInTheDocument();
    expect(screen.getByText(/black & white printer/i)).toBeInTheDocument();
  });

  it('defaults monochrome:false in the render request (#1870)', async () => {
    render(
      <LabelTemplatePickerModal
        isOpen={true}
        onClose={vi.fn()}
        availableSpools={SPOOLS}
        initialSelectedIds={[1]}
        spoolmanMode={false}
      />,
    );

    fireEvent.click(screen.getByText(/Box label \(40 × 30 mm\)/i));

    await waitFor(() => {
      expect(api.printSpoolLabels).toHaveBeenCalledWith(
        expect.objectContaining({
          template: 'box_40x30',
          monochrome: false,
          spools: expect.arrayContaining([expect.objectContaining({ id: 1 })]),
        }),
      );
    });
  });

  it('sends monochrome:true when the black & white checkbox is ticked (#1870)', async () => {
    render(
      <LabelTemplatePickerModal
        isOpen={true}
        onClose={vi.fn()}
        availableSpools={SPOOLS}
        initialSelectedIds={[1]}
        spoolmanMode={false}
      />,
    );

    fireEvent.click(screen.getByText(/black & white printer/i));
    fireEvent.click(screen.getByText(/Box label \(40 × 30 mm\)/i));

    await waitFor(() => {
      expect(api.printSpoolLabels).toHaveBeenCalledWith(
        expect.objectContaining({ template: 'box_40x30', monochrome: true }),
      );
    });
  });

  it('threads monochrome through the Spoolman endpoint too (#1870)', async () => {
    render(
      <LabelTemplatePickerModal
        isOpen={true}
        onClose={vi.fn()}
        availableSpools={SPOOLS}
        initialSelectedIds={[1]}
        spoolmanMode={true}
      />,
    );

    fireEvent.click(screen.getByText(/black & white printer/i));
    fireEvent.click(screen.getByText(/AMS holder - large \(75 × 55 mm\)/i));

    await waitFor(() => {
      expect(api.printSpoolmanSpoolLabels).toHaveBeenCalledWith(
        expect.objectContaining({ template: 'ams_holder_75x55', monochrome: true }),
      );
    });
    expect(api.printSpoolLabels).not.toHaveBeenCalled();
  });
});
