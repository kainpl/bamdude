import { useEffect, useRef, useState } from 'react';
import { ChevronDown, FolderOpen } from 'lucide-react';
import type { LibraryFolderTree } from '../api/client';
import { writableFolders } from '../utils/folderTree';
import { FolderTreePicker } from './FolderTreePicker';

/**
 * `FolderTreePicker` for the places that have no room for it — a toolbar row, a
 * settings line in a card. A button showing the current choice, opening the
 * same tree in a popover.
 *
 * ⚠️ **Why not just put the tree inline everywhere.** The tree is the better
 * control and the dialogs use it directly. But the MakerWorld header and the
 * virtual-printer card put this choice on ONE line beside a button, and a
 * ~250px list there pushes everything else off the row. Same tree, same
 * indentation, folded behind a trigger — rather than a `<select>` that spells
 * the nesting as em-dashes and reads nothing like the dialogs.
 */

export interface FolderTreeSelectProps {
  folders: LibraryFolderTree[] | undefined | null;
  value: number | null;
  onChange: (folderId: number | null) => void;
  /** Shown on the trigger when nothing is chosen, and as the first tree entry. */
  rootLabel: string;
  disabled?: boolean;
  /** Trigger width — a toolbar wants it to hug, a card row wants `w-full`. */
  className?: string;
  buttonClassName?: string;
}

export function FolderTreeSelect({
  folders,
  value,
  onChange,
  rootLabel,
  disabled = false,
  className = '',
  buttonClassName = 'text-sm px-2 py-1 border rounded bg-white dark:bg-gray-800 border-gray-300 dark:border-gray-700',
}: FolderTreeSelectProps) {
  const [open, setOpen] = useState(false);
  const wrapper = useRef<HTMLDivElement>(null);

  // ⚠️ The popover must close on an outside click and on Escape. Without both,
  // one left open sits over whatever the row's other controls are.
  useEffect(() => {
    if (!open) return;

    const onPointerDown = (e: MouseEvent) => {
      if (!wrapper.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };

    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const chosen = value === null ? null : writableFolders(folders).find(({ folder }) => folder.id === value);

  return (
    <div ref={wrapper} className={`relative ${className}`}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        disabled={disabled}
        aria-haspopup="true"
        aria-expanded={open}
        className={`flex items-center gap-1.5 disabled:opacity-50 disabled:cursor-not-allowed ${buttonClassName}`}
      >
        <FolderOpen className="w-3.5 h-3.5 shrink-0 opacity-70" />
        {/* ⚠️ A folder deleted out from under a saved choice must not blank the
            trigger — falling back to the root label would claim a destination
            that was not chosen, so the id is shown instead. */}
        <span className="truncate">{chosen ? chosen.folder.name : value === null ? rootLabel : `#${value}`}</span>
        <ChevronDown className="w-3.5 h-3.5 shrink-0 opacity-70 ml-auto" />
      </button>

      {open && (
        <div className="absolute right-0 z-50 mt-1 w-64 rounded-md border border-bambu-dark-tertiary bg-bambu-dark-secondary p-2 shadow-xl">
          <FolderTreePicker
            folders={folders}
            value={value}
            onChange={(id) => {
              onChange(id);
              setOpen(false);
            }}
            rootLabel={rootLabel}
            className="max-h-64"
          />
        </div>
      )}
    </div>
  );
}
