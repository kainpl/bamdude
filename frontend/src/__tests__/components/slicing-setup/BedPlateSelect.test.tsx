import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { BedPlateSelect } from '../../../components/slicing-setup/BedPlateSelect';

// The family's bed-plate select (R8).
const optionValues = () =>
  Array.from((screen.getByRole('combobox') as HTMLSelectElement).options).map((o) => o.value);

describe('BedPlateSelect', () => {
  it('Textured Cool Plate only with OrcaSlicer or when current', () => {
    const { rerender } = render(<BedPlateSelect value="Cool Plate" onChange={() => {}} slicer="bambu_studio" note={null} />);
    expect(optionValues()).not.toContain('Textured Cool Plate');
    rerender(<BedPlateSelect value="Cool Plate" onChange={() => {}} slicer="orcaslicer" note={null} />);
    expect(optionValues()).toContain('Textured Cool Plate');
    rerender(<BedPlateSelect value="Textured Cool Plate" onChange={() => {}} slicer={null} note={null} />);
    expect(optionValues()).toContain('Textured Cool Plate');
  });

  it('note outside the label', () => {
    render(
      <BedPlateSelect value="Cool Plate" onChange={() => {}} slicer="bambu_studio" note={{ kind: 'unknown', bed: 'Glass' }} />,
    );
    const select = screen.getByRole('combobox');
    const note = screen.getByRole('status');
    expect(select.closest('label')?.contains(note) ?? false).toBe(false);
    expect(select).toHaveAccessibleName();
  });

  it('keyboard changes the plate', async () => {
    const onChange = vi.fn();
    render(<BedPlateSelect value="Cool Plate" onChange={onChange} slicer="bambu_studio" note={null} />);
    const user = userEvent.setup();
    await user.tab();
    expect(screen.getByRole('combobox')).toHaveFocus();
    await user.selectOptions(screen.getByRole('combobox'), 'Engineering Plate');
    expect(onChange).toHaveBeenCalledWith('Engineering Plate');
  });
});
