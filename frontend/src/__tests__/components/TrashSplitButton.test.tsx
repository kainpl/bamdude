/**
 * The caret half opens a one-item menu portalled onto `body`. Closing it must
 * hand the focus back to the caret: the item that was clicked leaves with the
 * menu, and the purge modal the item opens reads `document.activeElement` as
 * the element to return focus to when it closes.
 */
import { describe, it, expect, vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
import { render } from '../utils';
import { TrashSplitButton } from '../../components/TrashSplitButton';

describe('TrashSplitButton', () => {
  it('purging from the caret menu gives the focus back to the caret', () => {
    const onPurgeClick = vi.fn();
    render(
      <TrashSplitButton
        trashHref="/files/trash"
        trashLabel="Trash"
        count={3}
        onPurgeClick={onPurgeClick}
        purgeLabel="Purge old"
      />,
    );
    // Read while the menu is closed: the item carries the same label.
    const caret = screen.getByRole('button', { name: 'Purge old' });

    fireEvent.click(caret);
    fireEvent.click(screen.getByRole('menuitem'));

    expect(onPurgeClick).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(document.activeElement).toBe(caret);
  });
});
