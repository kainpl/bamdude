import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { LibraryFolderTree } from '../../api/client';
import { FolderTreePicker } from '../../components/FolderTreePicker';

/**
 * The folder picker every dialog shares.
 *
 * It started as the "Move files" dialog's private list, next to three
 * `<select>`s spelling the same choice differently and a fourth private copy of
 * the flatten. These tests pin the parts that were divergent: the nesting is
 * VISIBLE, read-only external mounts are absent, and root is always offered.
 */

const folder = (
  id: number,
  name: string,
  children: LibraryFolderTree[] = [],
  extra: Partial<LibraryFolderTree> = {},
): LibraryFolderTree =>
  ({ id, name, children, is_external: false, external_readonly: false, ...extra }) as LibraryFolderTree;

const TREE = [folder(1, 'Prints', [folder(2, 'Minis', [folder(3, 'Bases')])]), folder(4, 'Spares')];

describe('what it offers', () => {
  it('always offers the root, first, whatever the tree', () => {
    render(<FolderTreePicker folders={TREE} value={null} onChange={vi.fn()} rootLabel="Library root" />);

    expect(screen.getAllByRole('button')[0]).toHaveTextContent('Library root');
  });

  it('shows every folder at every depth', () => {
    render(<FolderTreePicker folders={TREE} value={null} onChange={vi.fn()} rootLabel="Root" />);

    for (const name of ['Prints', 'Minis', 'Bases', 'Spares']) {
      expect(screen.getByText(name)).toBeInTheDocument();
    }
  });

  it('survives folders it has not been given yet', () => {
    render(<FolderTreePicker folders={undefined} value={null} onChange={vi.fn()} rootLabel="Root" />);

    expect(screen.getByText('Root')).toBeInTheDocument();
  });
});

describe('the nesting is the point', () => {
  it('indents each level further than its parent', () => {
    render(<FolderTreePicker folders={TREE} value={null} onChange={vi.fn()} rootLabel="Root" />);

    const padding = (name: string) =>
      parseInt(screen.getByText(name).closest('button')!.style.paddingLeft, 10);

    expect(padding('Prints')).toBeLessThan(padding('Minis'));
    expect(padding('Minis')).toBeLessThan(padding('Bases'));
  });

  it('puts siblings at the same indent', () => {
    render(<FolderTreePicker folders={TREE} value={null} onChange={vi.fn()} rootLabel="Root" />);

    const padding = (name: string) =>
      screen.getByText(name).closest('button')!.style.paddingLeft;

    expect(padding('Spares')).toBe(padding('Prints'));
  });
});

describe('read-only external mounts', () => {
  const WITH_READONLY = [
    folder(1, 'Prints'),
    folder(9, 'NAS archive', [], { is_external: true, external_readonly: true }),
  ];

  it('leaves them out — the backend answers 403 for writing into one', () => {
    render(<FolderTreePicker folders={WITH_READONLY} value={null} onChange={vi.fn()} rootLabel="Root" />);

    expect(screen.queryByText('NAS archive')).not.toBeInTheDocument();
  });

  it('shows them when the caller only reads folders', () => {
    render(
      <FolderTreePicker folders={WITH_READONLY} value={null} onChange={vi.fn()} rootLabel="Root" includeReadOnly />,
    );

    expect(screen.getByText('NAS archive')).toBeInTheDocument();
  });

  it('keeps a writable external mount', () => {
    const mounted = [folder(9, 'NAS drop', [], { is_external: true, external_readonly: false })];
    render(<FolderTreePicker folders={mounted} value={null} onChange={vi.fn()} rootLabel="Root" />);

    expect(screen.getByText('NAS drop')).toBeInTheDocument();
  });
});

describe('choosing', () => {
  it('reports the folder id', async () => {
    const onChange = vi.fn();
    render(<FolderTreePicker folders={TREE} value={null} onChange={onChange} rootLabel="Root" />);

    await userEvent.click(screen.getByText('Minis'));

    expect(onChange).toHaveBeenCalledWith(2);
  });

  it('reports null for the root, which is a real destination and not "unset"', async () => {
    const onChange = vi.fn();
    render(<FolderTreePicker folders={TREE} value={2} onChange={onChange} rootLabel="Root" />);

    await userEvent.click(screen.getByText('Root'));

    expect(onChange).toHaveBeenCalledWith(null);
  });
});

describe('the folder you are already in', () => {
  it('cannot be chosen', async () => {
    const onChange = vi.fn();
    render(
      <FolderTreePicker folders={TREE} value={null} onChange={onChange} rootLabel="Root" disabledId={1} disabledLabel="current" />,
    );

    await userEvent.click(screen.getByText('Prints'));

    expect(onChange).not.toHaveBeenCalled();
  });

  it('says why it is greyed out', () => {
    render(
      <FolderTreePicker folders={TREE} value={null} onChange={vi.fn()} rootLabel="Root" disabledId={1} disabledLabel="current" />,
    );

    expect(screen.getByText('Prints').closest('button')).toHaveTextContent('current');
  });

  it('can be the root — a file sitting in the root has nowhere to move to there either', () => {
    render(
      <FolderTreePicker folders={TREE} value={null} onChange={vi.fn()} rootLabel="Root" disabledId={null} disabledLabel="current" />,
    );

    expect(screen.getByText('Root').closest('button')).toBeDisabled();
  });

  it('disables nothing when the caller has no such folder', () => {
    render(<FolderTreePicker folders={TREE} value={null} onChange={vi.fn()} rootLabel="Root" />);

    for (const button of screen.getAllByRole('button')) {
      expect(button).not.toBeDisabled();
    }
  });
});

describe('folders the user may not add to (N56)', () => {
  // Someone's private folder the user only passes on the way to a file of theirs.
  const OWNED = [folder(1, 'Passage', [folder(2, 'Shared below', [], { can_write: true })], { can_write: false })];

  it('lists them for the tree’s shape, greyed out as view only, and keeps what is below them', async () => {
    const onChange = vi.fn();
    render(<FolderTreePicker folders={OWNED} value={null} onChange={onChange} rootLabel="Root" />);

    const passage = screen.getByText('Passage').closest('button')!;
    expect(passage).toBeDisabled();
    expect(passage).toHaveTextContent('view only');
    await userEvent.click(screen.getByText('Shared below'));
    expect(onChange).toHaveBeenCalledWith(2);
  });

  it('offers them in a picker that only reads folders', () => {
    render(<FolderTreePicker folders={OWNED} value={null} onChange={vi.fn()} rootLabel="Root" includeReadOnly readOnly />);

    expect(screen.getByText('Passage').closest('button')).not.toBeDisabled();
  });

  it('treats a folder from an answer without the flag as writable', () => {
    render(<FolderTreePicker folders={TREE} value={null} onChange={vi.fn()} rootLabel="Root" />);

    expect(screen.getByText('Prints').closest('button')).not.toBeDisabled();
  });
});
