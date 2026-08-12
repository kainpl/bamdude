import { FolderOpen } from 'lucide-react';
import type { LibraryFolderTree } from '../api/client';
import { flattenFolderTree, writableFolders } from '../utils/folderTree';

/**
 * The library folder tree as a pickable list — the look the "Move files" dialog
 * already had, lifted out so every dialog that chooses a folder reads the same.
 *
 * ⚠️ **Indentation is the whole point.** A `<select>` shows the same names with
 * no shape, and where a folder sits is most of what you are choosing between.
 * That is why this is a list rather than a dropdown, and why it is worth the
 * vertical space in a dialog.
 *
 * ⚠️ **Read-only external folders are dropped by default.** The backend answers
 * an upload into one with 403. A picker that only moves rows between internal
 * folders can pass `includeReadOnly` — but nothing does today.
 */

export interface FolderTreePickerProps {
  folders: LibraryFolderTree[] | undefined | null;
  value: number | null;
  onChange: (folderId: number | null) => void;
  /** Label for the "no folder / library root" entry, which is always first. */
  rootLabel: string;
  /** Rendered greyed-out and unselectable, with `disabledLabel` beside it. */
  disabledId?: number | null;
  disabledLabel?: string;
  includeReadOnly?: boolean;
  className?: string;
}

export function FolderTreePicker({
  folders,
  value,
  onChange,
  rootLabel,
  disabledId = undefined,
  disabledLabel,
  includeReadOnly = false,
  className = 'max-h-64',
}: FolderTreePickerProps) {
  // ⚠️ `writableFolders`, never its filter re-spelled here — that filter living
  // inline in every picker is exactly what it was extracted to stop.
  const rows = includeReadOnly
    ? (folders ?? []).flatMap((f) => flattenFolderTree(f))
    : writableFolders(folders);

  const entry = (
    id: number | null,
    name: string,
    depth: number,
    disabled: boolean,
  ) => (
    <button
      key={id ?? 'root'}
      type="button"
      onClick={() => !disabled && onChange(id)}
      disabled={disabled}
      className={`w-full text-left px-3 py-2 rounded transition-colors flex items-center gap-2 ${
        value === id
          ? 'bg-bambu-green/20 text-bambu-green'
          : disabled
            ? 'opacity-50 cursor-not-allowed text-bambu-gray'
            : 'hover:bg-bambu-dark text-white'
      }`}
      style={{ paddingLeft: `${12 + depth * 16}px` }}
    >
      <FolderOpen className="w-4 h-4 shrink-0" />
      <span className="truncate">{name}</span>
      {disabled && disabledLabel && (
        <span className="text-xs text-bambu-gray ml-auto shrink-0">({disabledLabel})</span>
      )}
    </button>
  );

  return (
    <div className={`${className} overflow-y-auto space-y-1`}>
      {entry(null, rootLabel, 0, disabledId === null)}
      {rows.map(({ folder, depth }) => entry(folder.id, folder.name, depth, folder.id === disabledId))}
    </div>
  );
}
