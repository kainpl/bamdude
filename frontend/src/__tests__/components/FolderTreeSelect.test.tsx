import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { LibraryFolderTree } from '../../api/client';
import { FolderTreeSelect } from '../../components/FolderTreeSelect';

/**
 * The same folder tree, folded behind a trigger, for the two places that put
 * this choice on one line beside something else — the MakerWorld header and the
 * virtual-printer card.
 *
 * What is pinned here is what a `<select>` gave for free and a hand-rolled
 * popover does not: it closes when you click away, it closes on Escape, and the
 * trigger says what is currently chosen.
 */

const folder = (
  id: number,
  name: string,
  children: LibraryFolderTree[] = [],
  extra: Partial<LibraryFolderTree> = {},
): LibraryFolderTree =>
  ({ id, name, children, is_external: false, external_readonly: false, ...extra }) as LibraryFolderTree;

const TREE = [folder(1, 'Prints', [folder(2, 'Minis')])];

const open = async () => userEvent.click(screen.getByRole('button', { expanded: false }));

describe('the trigger', () => {
  it('shows the root label when nothing is chosen', () => {
    render(<FolderTreeSelect folders={TREE} value={null} onChange={vi.fn()} rootLabel="Auto" />);

    expect(screen.getByRole('button')).toHaveTextContent('Auto');
  });

  it('shows the chosen folder by name', () => {
    render(<FolderTreeSelect folders={TREE} value={2} onChange={vi.fn()} rootLabel="Auto" />);

    expect(screen.getByRole('button')).toHaveTextContent('Minis');
  });

  it('does not claim the root when the chosen folder is gone', () => {
    // ⚠️ A folder deleted under a saved choice. Falling back to the root label
    // would show a destination nobody picked.
    render(<FolderTreeSelect folders={TREE} value={77} onChange={vi.fn()} rootLabel="Auto" />);

    expect(screen.getByRole('button')).not.toHaveTextContent('Auto');
    expect(screen.getByRole('button')).toHaveTextContent('#77');
  });

  it('opens nothing while disabled', async () => {
    render(<FolderTreeSelect folders={TREE} value={null} onChange={vi.fn()} rootLabel="Auto" disabled />);

    await userEvent.click(screen.getByRole('button'));

    expect(screen.queryByText('Prints')).not.toBeInTheDocument();
  });
});

describe('the popover', () => {
  it('is shut until asked for', () => {
    render(<FolderTreeSelect folders={TREE} value={null} onChange={vi.fn()} rootLabel="Auto" />);

    expect(screen.queryByText('Prints')).not.toBeInTheDocument();
  });

  it('shows the tree, nesting and all', async () => {
    render(<FolderTreeSelect folders={TREE} value={null} onChange={vi.fn()} rootLabel="Auto" />);

    await open();

    expect(screen.getByText('Prints')).toBeInTheDocument();
    expect(screen.getByText('Minis')).toBeInTheDocument();
  });

  it('reports the choice and closes itself', async () => {
    const onChange = vi.fn();
    render(<FolderTreeSelect folders={TREE} value={null} onChange={onChange} rootLabel="Auto" />);

    await open();
    await userEvent.click(screen.getByText('Minis'));

    expect(onChange).toHaveBeenCalledWith(2);
    expect(screen.queryByText('Prints')).not.toBeInTheDocument();
  });

  it('closes on a click outside — it sits over the row it is in', async () => {
    render(
      <div>
        <FolderTreeSelect folders={TREE} value={null} onChange={vi.fn()} rootLabel="Auto" />
        <button type="button">Import all</button>
      </div>,
    );

    await open();
    await userEvent.click(screen.getByText('Import all'));

    expect(screen.queryByText('Prints')).not.toBeInTheDocument();
  });

  it('closes on Escape', async () => {
    render(<FolderTreeSelect folders={TREE} value={null} onChange={vi.fn()} rootLabel="Auto" />);

    await open();
    await userEvent.keyboard('{Escape}');

    expect(screen.queryByText('Prints')).not.toBeInTheDocument();
  });

  it('stays open when the click lands inside it', async () => {
    render(<FolderTreeSelect folders={TREE} value={null} onChange={vi.fn()} rootLabel="Auto" />);

    await open();
    await userEvent.click(screen.getByText('Prints').closest('div')!);

    expect(screen.getByText('Minis')).toBeInTheDocument();
  });
});
