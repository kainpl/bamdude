import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { FolderOpen, FolderSymlink } from 'lucide-react';
import type { LibraryFolderTree } from '../api/client';
import { canWriteInto, flattenFolderTree, writableFolders } from '../utils/folderTree';

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
 *
 * ⚠️ **A folder the user may not add to is listed but not offered** (N56): one they
 * only pass on the way to a file of theirs keeps the tree's shape, greyed out as
 * "view only". Every dialog that chooses where something GOES reads this picker, so
 * this is the one place that says so; a picker that only reads (`readOnly`) offers
 * every folder.
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
  /** Entries a caller may not choose for a reason of its own — greyed out with `lockedLabel`. */
  locked?: (folderId: number | null) => boolean;
  /** One label for every locked entry, or the reason of each. */
  lockedLabel?: string | ((folderId: number | null) => string | undefined);
  /** Something shown beside a folder's name — the product mark of a linked folder. */
  mark?: (folder: LibraryFolderTree) => ReactNode;
  includeReadOnly?: boolean;
  /** The picker only READS folders (browsing): every folder may be chosen. */
  readOnly?: boolean;
  className?: string;
}

export function FolderTreePicker({
  folders,
  value,
  onChange,
  rootLabel,
  disabledId = undefined,
  disabledLabel,
  locked,
  lockedLabel,
  mark,
  includeReadOnly = false,
  readOnly = false,
  className = 'max-h-64',
}: FolderTreePickerProps) {
  const { t } = useTranslation();
  // ⚠️ `writableFolders`, never its filter re-spelled here — that filter living
  // inline in every picker is exactly what it was extracted to stop.
  const rows = includeReadOnly
    ? (folders ?? []).flatMap((f) => flattenFolderTree(f))
    : writableFolders(folders);

  const entry = (
    id: number | null,
    name: string,
    depth: number,
    current: boolean,
    folder?: LibraryFolderTree,
  ) => {
    const viewOnly = !current && !readOnly && folder !== undefined && !canWriteInto(folder);
    const lockedHere = !current && !viewOnly && (locked?.(id) ?? false);
    const disabled = current || viewOnly || lockedHere;
    const label = current
      ? disabledLabel
      : viewOnly
        ? t('fileManager.folderViewOnly')
        : lockedHere
          ? typeof lockedLabel === 'function' ? lockedLabel(id) : lockedLabel
          : undefined;
    return (
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
        {folder?.is_external ? <FolderSymlink className="w-4 h-4 shrink-0" /> : <FolderOpen className="w-4 h-4 shrink-0" />}
        <span className="truncate">{name}</span>
        {folder && mark?.(folder)}
        {label && <span className="text-xs text-bambu-gray ml-auto shrink-0">({label})</span>}
      </button>
    );
  };

  return (
    <div className={`${className} overflow-y-auto space-y-1`}>
      {entry(null, rootLabel, 0, disabledId === null)}
      {rows.map(({ folder, depth }) => entry(folder.id, folder.name, depth, folder.id === disabledId, folder))}
    </div>
  );
}
