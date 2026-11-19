import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { PresetFilters } from '../../../components/slicing-setup/PresetFilters';
import type { PresetFiltersState } from '../../../components/slicing-setup/usePresetFilters';

// The family's filter controls (R2, D4): the source segment and the farm
// switches, each part only where a place asks for it.
function state(over: Partial<PresetFiltersState> = {}): PresetFiltersState {
  return {
    source: 'mine',
    setSource: vi.fn(),
    onlyFarmModels: false,
    setOnlyFarmModels: vi.fn(),
    onlyLoaded: false,
    setOnlyLoaded: vi.fn(),
    farm: undefined,
    farmStatus: 'off',
    retry: vi.fn(),
    offered: true,
    softFilters: () => [],
    ...over,
  };
}

describe('PresetFilters', () => {
  it('renders only the parts asked for', () => {
    const { rerender } = render(<PresetFilters filters={state()} parts={{ source: true }} />);
    expect(screen.getByRole('group')).toBeInTheDocument();
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
    rerender(<PresetFilters filters={state()} parts={{ farmModels: true, loaded: true }} />);
    expect(screen.queryByRole('group')).toBeNull();
    expect(screen.getAllByRole('checkbox')).toHaveLength(2);
  });

  it('segment is a group with aria-pressed', async () => {
    const filters = state();
    render(<PresetFilters filters={filters} parts={{ source: true }} />);
    const buttons = screen.getAllByRole('button');
    expect(buttons.map((b) => b.getAttribute('aria-pressed'))).toEqual(['false', 'true', 'false']);
    await userEvent.setup().click(buttons[0]);
    expect(filters.setSource).toHaveBeenCalledWith('all');
  });

  it('switches hidden when not offered', () => {
    render(<PresetFilters filters={state({ offered: false })} parts={{ source: true, farmModels: true, loaded: true }} />);
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
    expect(screen.getByRole('group')).toBeInTheDocument();
  });

  it('stale note keeps the switches', async () => {
    const filters = state({ onlyFarmModels: true, farmStatus: 'stale' });
    render(<PresetFilters filters={filters} parts={{ farmModels: true, loaded: true }} />);
    expect(screen.getAllByRole('checkbox')[0]).toBeChecked();
    expect(screen.getByRole('status')).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole('button'));
    expect(filters.retry).toHaveBeenCalled();
  });
});
