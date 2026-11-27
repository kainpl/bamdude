import { useCallback, useEffect, useRef, useState, type DragEvent } from 'react';
import type { LibraryFileListItem, LibraryFolderTree } from '../api/client';
import { folderMoveBlock, isMovableFolder } from '../utils/folderMove';
import { canWriteInto, writableFolders } from '../utils/folderTree';
import { moveChangesProducts } from '../utils/workshopRights';

/**
 * Dragging a folder, or files, onto a folder of the File Manager.
 *
 * Native HTML5 drag-and-drop with MIME types of our own: the page's upload drop
 * zone (and `Layout`'s app-wide guard) react to the OS's `Files` type only, so
 * an internal drag never opens the upload overlay and an OS file drop is left
 * exactly as it was. What is being dragged lives here, not in the
 * `DataTransfer` — a `dragover` may read the types of a drag but not its data.
 *
 * The rules shown while dragging are the dialogs' own (`folderMoveBlock`, the
 * "Move files" dialog's products rule); the server decides all the same.
 */

export const FOLDER_DRAG_MIME = 'application/x-bamdude-folder';
export const FILES_DRAG_MIME = 'application/x-bamdude-files';

/** How long a drag rests on a collapsed folder before its branch opens. */
export const HOVER_EXPAND_MS = 700;

/** A drag of files from the operating system — never one of ours, whatever else it carries
 *  (a dragged thumbnail is an image, and browsers add `Files` to an image drag). */
export function isOsFileDrag(types: readonly string[]): boolean {
  return types.includes('Files') && !types.includes(FOLDER_DRAG_MIME) && !types.includes(FILES_DRAG_MIME);
}

type ActiveDrag =
  | { kind: 'folder'; folder: LibraryFolderTree }
  | { kind: 'files'; files: LibraryFileListItem[] };

export type DropState = 'legal' | 'illegal' | null;

type DropProps = {
  'data-drop-state'?: 'legal' | 'illegal';
  onDragEnter: (e: DragEvent<HTMLElement>) => void;
  onDragOver: (e: DragEvent<HTMLElement>) => void;
  onDragLeave: (e: DragEvent<HTMLElement>) => void;
  onDrop: (e: DragEvent<HTMLElement>) => void;
};

type DragProps = {
  draggable?: true;
  onDragStart?: (e: DragEvent<HTMLElement>) => void;
  onDragEnd?: () => void;
};

export interface LibraryDragDrop {
  folderDragProps: (folder: LibraryFolderTree) => DragProps;
  fileDragProps: (file: LibraryFileListItem) => DragProps;
  /** `targetId` null = the library root. `expandOnHover`: a collapsed folder with a branch to open. */
  dropProps: (targetId: number | null, expandOnHover?: boolean) => DropProps;
  dropState: (targetId: number | null) => DropState;
}

export function useLibraryDragDrop({
  folders,
  canMoveFolder,
  canDragFile,
  canChangeProducts,
  filesDraggedWith,
  onMoveFolder,
  onMoveFiles,
  onHoverExpand,
}: {
  folders: LibraryFolderTree[] | undefined;
  /** May the viewer move THIS folder (the page's own rights helper; N56: the owner their own)? */
  canMoveFolder: (folder: LibraryFolderTree) => boolean;
  canDragFile: (file: LibraryFileListItem) => boolean;
  /** `products:update` — a move that changes a file's products needs it. */
  canChangeProducts: boolean;
  /** The files that travel with `file`: the whole selection when it is part of one. */
  filesDraggedWith: (file: LibraryFileListItem) => LibraryFileListItem[];
  onMoveFolder: (folder: LibraryFolderTree, parentId: number | null) => void;
  onMoveFiles: (files: LibraryFileListItem[], folderId: number | null) => void;
  onHoverExpand: (folderId: number) => void;
}): LibraryDragDrop {
  // A phone or a tablet has the menu's "Move…": no touch drag-and-drop, and no polyfill for one.
  const [enabled] = useState(() => !window.matchMedia('(pointer: coarse)').matches);
  const dragRef = useRef<ActiveDrag | null>(null);
  const [dragging, setDragging] = useState(false);
  const [over, setOver] = useState<{ id: number | null; legal: boolean } | null>(null);
  const hoverRef = useRef<{ id: number; timer: ReturnType<typeof setTimeout> } | null>(null);

  const clearHover = useCallback(() => {
    if (hoverRef.current) clearTimeout(hoverRef.current.timer);
    hoverRef.current = null;
  }, []);

  const end = useCallback(() => {
    dragRef.current = null;
    clearHover();
    setDragging(false);
    setOver(null);
  }, [clearHover]);

  // The ways a drag ends without reaching one of our targets: Escape, a drop
  // somewhere else, a release outside the window.
  useEffect(() => {
    if (!dragging) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') end();
    };
    document.addEventListener('keydown', onKeyDown);
    document.addEventListener('drop', end);
    document.addEventListener('dragend', end);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      document.removeEventListener('drop', end);
      document.removeEventListener('dragend', end);
    };
  }, [dragging, end]);
  useEffect(() => clearHover, [clearHover]);

  const legal = useCallback(
    (drag: ActiveDrag, targetId: number | null): boolean => {
      if (drag.kind === 'folder') return folderMoveBlock(drag.folder, targetId, folders) === null;
      // Files: not where they all already are, not into a read-only share, and
      // not onto a folder that would change their products without the right to.
      if (drag.files.every((file) => (file.folder_id ?? null) === targetId)) return false;
      if (targetId === null) return canChangeProducts || !moveChangesProducts(drag.files, []);
      const target = writableFolders(folders).find((row) => row.folder.id === targetId)?.folder;
      if (!target || !canWriteInto(target)) return false;
      return canChangeProducts || !moveChangesProducts(drag.files, target.products.map((p) => p.id));
    },
    [folders, canChangeProducts],
  );

  const start = (e: DragEvent<HTMLElement>, drag: ActiveDrag, mime: string, payload: string) => {
    e.stopPropagation();
    e.dataTransfer.setData(mime, payload);
    e.dataTransfer.effectAllowed = 'move';
    dragRef.current = drag;
    setDragging(true);
  };

  const folderDragProps = (folder: LibraryFolderTree): DragProps =>
    enabled && isMovableFolder(folder) && canMoveFolder(folder)
      ? {
          draggable: true,
          onDragStart: (e) => start(e, { kind: 'folder', folder }, FOLDER_DRAG_MIME, String(folder.id)),
          onDragEnd: end,
        }
      : {};

  const fileDragProps = (file: LibraryFileListItem): DragProps =>
    enabled && canDragFile(file)
      ? {
          draggable: true,
          onDragStart: (e) => {
            const files = filesDraggedWith(file);
            start(e, { kind: 'files', files }, FILES_DRAG_MIME, files.map((f) => f.id).join(','));
          },
          onDragEnd: end,
        }
      : {};

  const ours = (e: DragEvent<HTMLElement>) =>
    dragRef.current !== null &&
    (e.dataTransfer.types.includes(FOLDER_DRAG_MIME) || e.dataTransfer.types.includes(FILES_DRAG_MIME));

  const dropProps = (targetId: number | null, expandOnHover = false): DropProps => {
    const hover = (e: DragEvent<HTMLElement>) => {
      const drag = dragRef.current;
      if (!drag || !ours(e)) return;
      // Ours alone: the upload drop zone around this row never hears of it.
      e.preventDefault();
      e.stopPropagation();
      const ok = legal(drag, targetId);
      e.dataTransfer.dropEffect = ok ? 'move' : 'none';
      setOver((prev) => (prev && prev.id === targetId && prev.legal === ok ? prev : { id: targetId, legal: ok }));
      if (targetId !== null && expandOnHover) {
        if (hoverRef.current?.id !== targetId) {
          clearHover();
          hoverRef.current = { id: targetId, timer: setTimeout(() => onHoverExpand(targetId), HOVER_EXPAND_MS) };
        }
      } else if (hoverRef.current && hoverRef.current.id !== targetId) {
        clearHover();
      }
    };
    const state = over && over.id === targetId ? (over.legal ? 'legal' : 'illegal') : undefined;
    return {
      ...(state ? { 'data-drop-state': state } : {}),
      onDragEnter: hover,
      onDragOver: hover,
      onDragLeave: (e) => {
        if (!dragRef.current) return;
        // Moving onto a child of this row is not leaving it.
        if (e.relatedTarget instanceof Node && e.currentTarget.contains(e.relatedTarget)) return;
        if (hoverRef.current?.id === targetId) clearHover();
        setOver((prev) => (prev && prev.id === targetId ? null : prev));
      },
      onDrop: (e) => {
        const drag = dragRef.current;
        if (!drag || !ours(e)) return;
        e.preventDefault();
        e.stopPropagation();
        const ok = legal(drag, targetId);
        end();
        if (!ok) return;
        if (drag.kind === 'folder') onMoveFolder(drag.folder, targetId);
        else onMoveFiles(drag.files, targetId);
      },
    };
  };

  const dropState = (targetId: number | null): DropState =>
    over && over.id === targetId ? (over.legal ? 'legal' : 'illegal') : null;

  return { folderDragProps, fileDragProps, dropProps, dropState };
}
