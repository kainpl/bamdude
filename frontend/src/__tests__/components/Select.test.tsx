/**
 * Tests for the Select component.
 *
 * Deliberately thin on class strings: pinning every utility would just be the
 * component written twice, and the density pass of 2026-09-19 showed what that
 * costs — a layout tweak fails a test that was asserting nothing about
 * behaviour. What is pinned is what a caller can actually get wrong: that it is
 * still a real `<select>`, that it takes a value and reports a change, that the
 * size and tone tables are closed, and the one rule that cannot be expressed at
 * a call site — the text colour belongs to `tone`, because `className` cannot
 * win that fight against Tailwind's emit order.
 */

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Select } from '../../components/Select';

function options() {
  return (
    <>
      <option value="a">Alpha</option>
      <option value="b">Beta</option>
    </>
  );
}

describe('Select', () => {
  it('is a real select, so a phone gets the system picker', () => {
    render(<Select value="a" onChange={() => {}}>{options()}</Select>);

    expect(screen.getByRole('combobox').tagName).toBe('SELECT');
  });

  it('reports a change with the chosen value', async () => {
    const user = userEvent.setup();
    // Read inside the handler: `target` is the live node, and a controlled
    // select that is never re-rendered with the new value has already been
    // reverted by the time an assertion outside could look at it.
    const seen: string[] = [];

    render(<Select value="a" onChange={(e) => seen.push(e.target.value)}>{options()}</Select>);
    await user.selectOptions(screen.getByRole('combobox'), 'b');

    expect(seen).toEqual(['b']);
  });

  it('can be disabled', () => {
    render(<Select value="a" onChange={() => {}} disabled>{options()}</Select>);

    expect(screen.getByRole('combobox')).toBeDisabled();
  });

  it('passes the attributes a caller still owns', () => {
    render(
      <Select value="a" onChange={() => {}} aria-label="Sort by" title="Sort by" name="sort">
        {options()}
      </Select>,
    );

    const select = screen.getByRole('combobox');
    expect(select).toHaveAccessibleName('Sort by');
    expect(select).toHaveAttribute('name', 'sort');
  });

  it('keeps the caller layout classes it is given', () => {
    render(<Select value="a" onChange={() => {}} className="min-w-[9rem]">{options()}</Select>);

    expect(screen.getByRole('combobox').className).toContain('min-w-[9rem]');
  });

  it('owns the text colour, since a className could not win that one', () => {
    // By token, not by substring: `muted` carries `hover:text-white`, and a
    // naive contains() would call that a resting white.
    const tokens = () => screen.getByRole('combobox').className.split(/\s+/);

    const { rerender } = render(<Select value="a" onChange={() => {}}>{options()}</Select>);
    expect(tokens()).toContain('text-white');

    rerender(<Select value="a" onChange={() => {}} tone="muted">{options()}</Select>);
    expect(tokens()).toContain('text-bambu-gray');
    expect(tokens()).not.toContain('text-white');
  });

  it('will not compile a filter that forgets to say whether it is on', () => {
    // These are typecheck assertions, not runtime ones: `npm run typecheck`
    // enters this directory, so a union that stopped enforcing the pair would
    // fail the build here rather than ship a filter stuck looking empty.
    // @ts-expect-error `active` is required with tone="filter"
    const missingActive = <Select tone="filter" value="" onChange={() => {}}>{options()}</Select>;
    // @ts-expect-error a chip has one shape, so it takes no size
    const sizedChip = <Select tone="filter" active size="sm" value="" onChange={() => {}}>{options()}</Select>;
    // @ts-expect-error and `active` means nothing to an ordinary field
    const activeField = <Select active value="" onChange={() => {}}>{options()}</Select>;

    expect([missingActive, sizedChip, activeField]).toHaveLength(3);
  });

  it('says out loud whether a filter is set', () => {
    // The whole reason this tone exists: a filter that looks the same set and
    // unset is a filter nobody notices they left on.
    const { rerender } = render(
      <Select tone="filter" active={false} value="" onChange={() => {}}>{options()}</Select>,
    );
    expect(screen.getByRole('combobox').className).toContain('bg-transparent');

    rerender(<Select tone="filter" active value="a" onChange={() => {}}>{options()}</Select>);
    const cls = screen.getByRole('combobox').className;
    expect(cls).toContain('bg-bambu-green/20');
    expect(cls).toContain('text-bambu-green');
    expect(cls).not.toContain('bg-transparent');
  });

  it('gives every size but xs a touch target, and xs none', () => {
    // xs lives in a dense header row that has no room for a 44px control; the
    // others are the ones a finger is expected to hit.
    const { rerender } = render(<Select value="a" onChange={() => {}} size="xs">{options()}</Select>);
    expect(screen.getByRole('combobox').className).not.toContain('min-h-');

    for (const size of ['sm', 'md', 'lg'] as const) {
      rerender(<Select value="a" onChange={() => {}} size={size}>{options()}</Select>);
      expect(screen.getByRole('combobox').className, size).toContain('md:min-h-0');
    }
  });
});
