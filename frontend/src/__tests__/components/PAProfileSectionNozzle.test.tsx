/**
 * The PA-Profil picker renders one row per nozzle-specific profile (#2618).
 *
 * `fetchPrinterCalibrations` is the half that finally *retrieves* a 0.6 mm
 * profile; this is the half that has to show it. Two profiles for one filament
 * differing only in nozzle (PAHT-CF at 0.4 mm K=0.042 and 0.6 mm K=0.028) must
 * both be offered and both be identifiable — they share a name, so without the
 * diameter badge the list reads as a duplicate.
 */

import { describe, it, expect, vi } from 'vitest';
import { screen } from '@testing-library/react';
import { render } from '../utils';
import { PAProfileSection } from '../../components/spool-form/PAProfileSection';
import { defaultFormData } from '../../components/spool-form/types';
import type { PrinterWithCalibrations } from '../../components/spool-form/types';

const printers = [
  {
    printer: { id: 1, name: 'H2D', connected: true },
    calibrations: [
      // Same filament (non-generic id → matched by id), different nozzle + extruder.
      { cali_idx: 10, filament_id: 'GFN05', setting_id: '', name: 'PAHT-CF', k_value: 0.042, n_coef: 0, extruder_id: 0, nozzle_diameter: '0.4' },
      { cali_idx: 11, filament_id: 'GFN05', setting_id: '', name: 'PAHT-CF', k_value: 0.028, n_coef: 0, extruder_id: 1, nozzle_diameter: '0.6' },
    ],
  },
] as unknown as PrinterWithCalibrations[];

function renderSection(calibrations: PrinterWithCalibrations[]) {
  return render(
    <PAProfileSection
      formData={{ ...defaultFormData, material: 'PAHT-CF', slicer_filament: 'GFN05' }}
      updateField={vi.fn()}
      printersWithCalibrations={calibrations}
      selectedProfiles={new Set()}
      setSelectedProfiles={vi.fn()}
      expandedPrinters={new Set(['1'])}
      setExpandedPrinters={vi.fn()}
    />,
  );
}

describe('PAProfileSection nozzle-specific profiles (#2618)', () => {
  it('renders both nozzle profiles for one filament, each with a nozzle badge', () => {
    renderSection(printers);

    // Both nozzle-specific K values are offered — not just the 0.4 mm one.
    expect(screen.getByText('K=0.042')).toBeInTheDocument();
    expect(screen.getByText('K=0.028')).toBeInTheDocument();

    // Each is labelled by its nozzle, which is all that distinguishes them.
    expect(screen.getByText('0.4mm')).toBeInTheDocument();
    expect(screen.getByText('0.6mm')).toBeInTheDocument();
  });

  it('omits the badge when the printer reported no diameter for the profile', () => {
    renderSection([
      {
        printer: { id: 1, name: 'P1S', connected: true },
        calibrations: [
          { cali_idx: 10, filament_id: 'GFN05', setting_id: '', name: 'PAHT-CF', k_value: 0.042, n_coef: 0, extruder_id: 0, nozzle_diameter: '' },
        ],
      },
    ] as unknown as PrinterWithCalibrations[]);

    expect(screen.getByText('K=0.042')).toBeInTheDocument();
    expect(screen.queryByText('mm')).not.toBeInTheDocument();
  });
});
