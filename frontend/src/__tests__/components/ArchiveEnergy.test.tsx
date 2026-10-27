/**
 * An archive's energy says how it was obtained (spec plug-meter-watchdog W11): an estimate and a
 * recovered window carry «≈», a figure the server could not trust says so instead of reading as
 * nothing at all — or as a zero.
 */
import { describe, expect, it } from 'vitest';
import { screen } from '@testing-library/react';

import { render } from '../utils';
import { ArchiveEnergyFact } from '../../components/ArchiveEnergy';
import { hasEnergyFact } from '../../utils/archiveEnergy';
import type { ArchiveEnergyFields } from '../../utils/archiveEnergy';

const fact = (fields: Partial<ArchiveEnergyFields>) => ({
  energy_kwh: null,
  energy_cost: null,
  energy_status: null,
  energy_coverage: null,
  energy_reason: null,
  ...fields,
});

function show(archive: ArchiveEnergyFields) {
  return render(<ArchiveEnergyFact archive={archive} currency="$" className="fact" iconClassName="icon" />);
}

describe('ArchiveEnergyFact', () => {
  it('marks an estimate and names how much of the print the history covered', () => {
    show(fact({ energy_kwh: 0.2, energy_cost: 0.04, energy_status: 'estimated', energy_coverage: 0.97 }));

    const shown = screen.getByText(/0\.04/);
    expect(shown).toHaveTextContent('≈ $0.04');
    expect(shown.getAttribute('title')).toMatch(/0\.200 kWh/);
    expect(shown.getAttribute('title')).toMatch(/97%/);
  });

  it('marks a recovered window as approximate', () => {
    show(fact({ energy_kwh: 0.4, energy_cost: 0.08, energy_status: 'approximate' }));

    expect(screen.getByText(/0\.08/)).toHaveTextContent('≈ $0.08');
  });

  it('shows a measured figure as it is', () => {
    show(fact({ energy_kwh: 0.1, energy_cost: 0.02, energy_status: 'measured' }));

    expect(screen.getByText(/0\.02/)).toHaveTextContent(/^\$0\.02$/);
  });

  it('says an untrusted figure is unknown, with the reason, never a zero', () => {
    show(fact({ energy_status: 'unavailable', energy_reason: 'source_changed' }));

    const shown = screen.getByText('Energy unknown');
    expect(shown.closest('[title]')?.getAttribute('title')).toMatch(/reconfigured or replaced/);
    expect(screen.queryByText(/\$0/)).not.toBeInTheDocument();
  });

  it('falls back to a general reason it does not know', () => {
    show(fact({ energy_status: 'unavailable', energy_reason: 'something_new' }));

    expect(screen.getByText('Energy unknown').closest('[title]')?.getAttribute('title')).toMatch(
      /no reliable energy figure/,
    );
  });

  it('has nothing to show for an archive without energy', () => {
    expect(hasEnergyFact(fact({}))).toBe(false);
    expect(hasEnergyFact(fact({ energy_status: 'unavailable' }))).toBe(true);
    expect(hasEnergyFact(fact({ energy_cost: 0 }))).toBe(true);
  });
});
