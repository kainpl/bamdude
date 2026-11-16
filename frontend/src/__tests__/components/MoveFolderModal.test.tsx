/**
 * The folder Move dialog while its request is in flight: it stays, so the
 * server's answer — the move, or a refusal — is shown to the operator who
 * asked for it.
 */

import { describe, it, expect, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { MoveFolderModal } from '../../components/library/MoveFolderModal';
import { ApiError, type LibraryFolderTree } from '../../api/client';

const folder = (id: number, name: string, parent_id: number | null): LibraryFolderTree =>
  ({
    id,
    name,
    parent_id,
    is_external: false,
    external_readonly: false,
    external_path: null,
    file_count: 0,
    latest_activity_at: null,
    products: [],
    children: [],
  }) as unknown as LibraryFolderTree;

const tree = [folder(1, 'Parts', null), folder(4, 'Spares', null)];

function show() {
  let settle: (error?: unknown) => void = () => {};
  const onMove = vi.fn(
    () =>
      new Promise<void>((resolve, reject) => {
        settle = (error) => (error ? reject(error) : resolve());
      }),
  );
  const onClose = vi.fn();
  render(<MoveFolderModal folder={tree[0]} folders={tree} onMove={onMove} onClose={onClose} />);
  const dialog = screen.getByRole('dialog', { name: 'Move “Parts”' });
  const target = (name: string) =>
    within(dialog)
      .getAllByRole('button')
      .find((b) => b.querySelector('span.truncate')?.textContent === name) as HTMLButtonElement;
  return { dialog, target, onMove, onClose, settle: (error?: unknown) => settle(error) };
}

describe('MoveFolderModal', () => {
  it('cannot be closed while the move is in flight, and shows a refusal that arrives', async () => {
    const { dialog, target, onMove, onClose, settle } = show();

    await userEvent.click(target('Spares'));
    await userEvent.click(within(dialog).getByRole('button', { name: 'Move' }));
    expect(onMove).toHaveBeenCalledWith(4);

    expect(within(dialog).getByRole('button', { name: 'Close' })).toBeDisabled();
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeDisabled();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).not.toHaveBeenCalled();

    settle(new ApiError('The folder is linked to products', 409));
    expect(await within(dialog).findByRole('alert')).toBeInTheDocument();
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Close' })).toBeEnabled());
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeEnabled();
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('closes by itself once the folder has moved', async () => {
    const { dialog, target, onClose, settle } = show();

    await userEvent.click(target('Spares'));
    await userEvent.click(within(dialog).getByRole('button', { name: 'Move' }));
    settle();

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });
});
