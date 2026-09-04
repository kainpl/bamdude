/**
 * The card menus are a `role="menu"`, so they must be drivable from a keyboard.
 *
 * The panel is portal-rendered on `document.body` and the trigger stays in the
 * card, so nothing about the DOM order helps: without an explicit focus move,
 * opening the menu with Enter left the focus on the trigger and Tab walked into
 * the page BEHIND the panel — a menu announced to a screen reader and then
 * unreachable by the person using it.
 */

import { describe, it, expect, vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
import { render } from '../utils';
import { CardActionMenu, CardActionMenuItem } from '../../components/CardActionMenu';

function mount(onSelect = vi.fn()) {
  render(
    <CardActionMenu label="Actions" testId="card-menu">
      {(close) => (
        <>
          <CardActionMenuItem
            onSelect={() => {
              onSelect('edit');
              close();
            }}
          >
            Edit
          </CardActionMenuItem>
          <CardActionMenuItem onSelect={() => onSelect('duplicate')}>Duplicate</CardActionMenuItem>
          <CardActionMenuItem onSelect={() => onSelect('delete')} danger>
            Delete
          </CardActionMenuItem>
        </>
      )}
    </CardActionMenu>,
  );
  return { trigger: screen.getByTestId('card-menu'), onSelect };
}

const focused = () => document.activeElement?.textContent;

describe('CardActionMenu keyboard', () => {
  it('moves focus to the first item when it opens', () => {
    const { trigger } = mount();
    fireEvent.click(trigger);
    expect(screen.getAllByRole('menuitem')).toHaveLength(3);
    expect(focused()).toBe('Edit');
  });

  it('walks the items with the arrows, wrapping at both ends', () => {
    const { trigger } = mount();
    fireEvent.click(trigger);

    fireEvent.keyDown(window, { key: 'ArrowDown' });
    expect(focused()).toBe('Duplicate');
    fireEvent.keyDown(window, { key: 'ArrowDown' });
    expect(focused()).toBe('Delete');
    // A menu is a ring: past the last entry is the first one.
    fireEvent.keyDown(window, { key: 'ArrowDown' });
    expect(focused()).toBe('Edit');
    fireEvent.keyDown(window, { key: 'ArrowUp' });
    expect(focused()).toBe('Delete');
  });

  it('jumps to the ends with Home and End', () => {
    const { trigger } = mount();
    fireEvent.click(trigger);

    fireEvent.keyDown(window, { key: 'End' });
    expect(focused()).toBe('Delete');
    fireEvent.keyDown(window, { key: 'Home' });
    expect(focused()).toBe('Edit');
  });

  it('closes on Escape and gives the focus back to the trigger', () => {
    const { trigger } = mount();
    fireEvent.click(trigger);
    expect(screen.getByRole('menu')).toBeInTheDocument();

    fireEvent.keyDown(window, { key: 'Escape' });

    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    // Not `document.body`: a menu that closes into nowhere leaves a keyboard
    // user at the top of the page, several tab stops from where they were.
    expect(document.activeElement).toBe(trigger);
  });
});
