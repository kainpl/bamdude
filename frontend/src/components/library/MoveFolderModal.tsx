import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Briefcase, FolderOpen, Link2, Loader2 } from 'lucide-react';
import type { LibraryFolderTree } from '../../api/client';
import { Button } from '../Button';
import { FolderTreePicker } from '../FolderTreePicker';
import { Modal } from '../Modal';
import { folderMoveBlock, folderMoveRefusalText, type FolderMoveBlock } from '../../utils/folderMove';

/** A linked folder's product mark — read-only here: it says what is being moved, or where to. */
function ProductMark({ folder }: { folder: LibraryFolderTree }) {
  if (folder.products.length === 0) return null;
  return (
    <span
      className="shrink-0 flex items-center gap-1 text-xs px-1.5 py-0.5 rounded bg-blue-100 dark:bg-blue-500/20 text-blue-700 dark:text-blue-400"
      title={folder.products.map((p) => p.name).join(', ')}
    >
      <Link2 className="w-3 h-3" />
      <Briefcase className="w-3 h-3" />
      {folder.products.length > 1 && <span className="text-[10px] font-semibold">×{folder.products.length}</span>}
    </span>
  );
}

const BLOCK_LABELS: Record<Exclude<FolderMoveBlock, 'current'>, string> = {
  self: 'fileManager.moveFolder.blocked.self',
  descendant: 'fileManager.moveFolder.blocked.descendant',
  external: 'fileManager.moveFolder.blocked.external',
};

/**
 * "Move…" for one folder: the whole tree, with every target the server would
 * refuse shown disabled and saying why. The server decides all the same — a
 * refusal is shown here by its code and the dialog stays open.
 */
export function MoveFolderModal({
  folder,
  folders,
  onMove,
  onClose,
}: {
  folder: LibraryFolderTree;
  folders: LibraryFolderTree[];
  /** Resolves when the folder has moved; rejects with the server's refusal. */
  onMove: (parentId: number | null) => Promise<unknown>;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const currentParent = folder.parent_id ?? null;
  // `undefined` = nothing chosen yet: the root is a real choice (`null`).
  const [target, setTarget] = useState<number | null | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const blockOf = (id: number | null) => folderMoveBlock(folder, id, folders);
  const lockedLabel = (id: number | null) => {
    const block = blockOf(id);
    return block && block !== 'current' ? t(BLOCK_LABELS[block]) : undefined;
  };

  const submit = async () => {
    if (target === undefined || blockOf(target)) return;
    setBusy(true);
    setError(null);
    try {
      await onMove(target);
      onClose();
    } catch (refusal) {
      setError(folderMoveRefusalText(t, refusal));
      setBusy(false);
    }
  };

  return (
    <Modal
      onClose={onClose}
      // In flight the dialog stays: a refusal that arrives is shown here, and nowhere else.
      closeDisabled={busy}
      title={t('fileManager.moveFolder.title', { name: folder.name })}
      size="sm"
    >
      <div className="p-4 space-y-4">
        <div className="flex items-center gap-2 text-sm text-white min-w-0">
          <FolderOpen className="w-4 h-4 shrink-0 text-bambu-green" />
          <span className="truncate">{folder.name}</span>
          <ProductMark folder={folder} />
        </div>
        <p className="text-xs text-bambu-gray">{t('fileManager.moveFolder.hint')}</p>
        <FolderTreePicker
          folders={folders}
          value={target === undefined ? -1 : target}
          onChange={(id) => {
            setTarget(id);
            setError(null);
          }}
          rootLabel={t('fileManager.moveFolder.root')}
          disabledId={currentParent}
          disabledLabel={t('fileManager.moveFolder.blocked.current')}
          locked={(id) => blockOf(id) !== null}
          lockedLabel={lockedLabel}
          mark={(row) => <ProductMark folder={row} />}
          includeReadOnly
        />
        {error && (
          <p role="alert" className="text-sm text-red-700 dark:text-red-400">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </Button>
          <Button onClick={submit} disabled={busy || target === undefined || blockOf(target) !== null}>
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : t('common.move')}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
