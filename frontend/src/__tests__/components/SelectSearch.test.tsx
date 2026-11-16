/**
 * The Select primitive's opt-in search.
 *
 * It narrows the options of the same real `<select>` — the system picker on a
 * phone stays — and is asked for with `search`. Without the prop the field is
 * the one `<select>` it has always been, which is what `Select.test.tsx` pins.
 */
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { Select, type SelectSearch } from '../../components/Select';

const SEARCH: SelectSearch = {
  label: 'Search filament profiles',
  placeholder: 'Type to narrow',
  noMatches: 'No matching presets',
};

function Field({ onPick, disabled }: { onPick?: (value: string) => void; disabled?: boolean }) {
  const [value, setValue] = useState('std:pla');
  return (
    <Select
      aria-label="Filament profile"
      className="flex-1 min-w-0"
      value={value}
      disabled={disabled}
      search={SEARCH}
      onChange={(e) => {
        onPick?.(e.target.value);
        setValue(e.target.value);
      }}
    >
      <option value="">— Select —</option>
      <optgroup label="Imported">
        <option value="loc:petg">My PETG HF</option>
      </optgroup>
      <optgroup label="Standard">
        <option value="std:pla">Bambu PLA Basic</option>
        <option value="std:petg">Bambu PETG HF</option>
        <option value="std:abs">Generic ABS</option>
      </optgroup>
    </Select>
  );
}

const select = () => screen.getByRole('combobox', { name: 'Filament profile' }) as HTMLSelectElement;
const searchButton = () => screen.getByRole('button', { name: SEARCH.label });
const optionTexts = () => within(select()).getAllByRole('option').map((o) => o.textContent);
const groupLabels = () => Array.from(select().querySelectorAll('optgroup')).map((g) => g.label);

afterEach(() => vi.restoreAllMocks());

describe('Select — search', () => {
  it('stays a real select with every option until a search is opened', () => {
    render(<Field />);

    expect(select().tagName).toBe('SELECT');
    expect(optionTexts()).toEqual(['— Select —', 'My PETG HF', 'Bambu PLA Basic', 'Bambu PETG HF', 'Generic ABS']);
    expect(screen.queryByRole('searchbox')).toBeNull();
    expect(searchButton()).toHaveAttribute('aria-expanded', 'false');
  });

  it('opens a named search box on the button and puts the caret in it', async () => {
    const user = userEvent.setup();
    render(<Field />);

    await user.click(searchButton());

    const box = screen.getByRole('searchbox', { name: SEARCH.label });
    expect(box).toHaveFocus();
    expect(box).toHaveAttribute('placeholder', 'Type to narrow');
    expect(searchButton()).toHaveAttribute('aria-expanded', 'true');
  });

  it('keeps the options whose text has every word, in any order and case', async () => {
    const user = userEvent.setup();
    render(<Field />);

    await user.click(searchButton());
    await user.type(screen.getByRole('searchbox'), 'hf PETG');

    // The chosen option and the placeholder always stay: search narrows what
    // is listed, never what is chosen.
    expect(optionTexts()).toEqual(['— Select —', 'My PETG HF', 'Bambu PLA Basic', 'Bambu PETG HF']);
  });

  it('drops a group the query emptied', async () => {
    const user = userEvent.setup();
    render(<Field />);

    await user.click(searchButton());
    await user.type(screen.getByRole('searchbox'), 'generic');

    expect(groupLabels()).toEqual(['Standard']);
    expect(optionTexts()).toEqual(['— Select —', 'Bambu PLA Basic', 'Generic ABS']);
  });

  it('says so when nothing matches, and keeps the current choice listed', async () => {
    const user = userEvent.setup();
    render(<Field />);

    await user.click(searchButton());
    expect(screen.getByRole('status')).toHaveTextContent('');
    await user.type(screen.getByRole('searchbox'), 'nylon');

    expect(screen.getByRole('status')).toHaveTextContent('No matching presets');
    expect(optionTexts()).toEqual(['— Select —', 'Bambu PLA Basic']);
    expect(select().value).toBe('std:pla');
  });

  it('picks the first match on Enter, through the select itself', async () => {
    const user = userEvent.setup();
    const picked: string[] = [];
    render(<Field onPick={(v) => picked.push(v)} />);

    await user.click(searchButton());
    await user.type(screen.getByRole('searchbox'), 'petg{Enter}');

    expect(picked).toEqual(['loc:petg']);
    expect(select().value).toBe('loc:petg');
    // The search is over: the box is gone, the full list is back, and the
    // keyboard is on the field that now shows the choice.
    expect(screen.queryByRole('searchbox')).toBeNull();
    expect(optionTexts()).toHaveLength(5);
    expect(select()).toHaveFocus();
  });

  it('does nothing on Enter when nothing matches', async () => {
    const user = userEvent.setup();
    const picked: string[] = [];
    render(<Field onPick={(v) => picked.push(v)} />);

    await user.click(searchButton());
    await user.type(screen.getByRole('searchbox'), 'nylon{Enter}');

    expect(picked).toEqual([]);
    expect(screen.getByRole('searchbox')).toBeInTheDocument();
  });

  it('moves to the narrowed list on ArrowDown', async () => {
    const user = userEvent.setup();
    render(<Field />);

    await user.click(searchButton());
    await user.type(screen.getByRole('searchbox'), 'petg{ArrowDown}');

    expect(select()).toHaveFocus();
    expect(optionTexts()).toEqual(['— Select —', 'My PETG HF', 'Bambu PLA Basic', 'Bambu PETG HF']);
  });

  it('closes on Escape without the dialog around it hearing the key', async () => {
    // Every modal's Escape is one listener on `window` (modalStack). The
    // search box has to take the key before it gets there.
    const user = userEvent.setup();
    const modalEscape = vi.fn();
    const onWindowKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') modalEscape();
    };
    window.addEventListener('keydown', onWindowKey);
    try {
      render(<Field />);
      await user.click(searchButton());
      await user.type(screen.getByRole('searchbox'), 'petg{Escape}');

      expect(modalEscape).not.toHaveBeenCalled();
      expect(screen.queryByRole('searchbox')).toBeNull();
      expect(optionTexts()).toHaveLength(5);
      expect(searchButton()).toHaveFocus();

      // With the search closed, Escape is the dialog's again.
      await user.keyboard('{Escape}');
      expect(modalEscape).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener('keydown', onWindowKey);
    }
  });

  it('closes on the button too, and forgets the query', async () => {
    const user = userEvent.setup();
    render(<Field />);

    await user.click(searchButton());
    await user.type(screen.getByRole('searchbox'), 'generic');
    await user.click(searchButton());
    expect(optionTexts()).toHaveLength(5);

    await user.click(searchButton());
    expect(screen.getByRole('searchbox')).toHaveValue('');
  });

  it('cannot be opened on a disabled field', () => {
    render(<Field disabled />);

    expect(searchButton()).toBeDisabled();
    expect(select()).toBeDisabled();
  });

  it('sizes the group with the caller classes and lets the select fill it', () => {
    render(<Field />);

    const group = select().parentElement?.parentElement as HTMLElement;
    expect(group.className).toContain('flex-1');
    expect(select().className).toContain('min-w-0');
  });

  it('will not compile a searchable filter chip', () => {
    // @ts-expect-error a chip is one element; search belongs to a field
    const chip = <Select tone="filter" active value="" search={SEARCH} onChange={() => {}}><option value="">x</option></Select>;
    expect(chip).toBeTruthy();
  });
});
