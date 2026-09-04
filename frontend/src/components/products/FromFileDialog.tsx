import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { FileBox, Loader2, Search, X } from 'lucide-react';
import { api } from '../../api/client';
import type { LibraryFolderTree, Product } from '../../api/client';
import { Button } from '../Button';
import { useToast } from '../../contexts/ToastContext';

/** How long the search box waits before it becomes a request. */
const DEBOUNCE_MS = 300;
/** One screenful. The dialog pages nothing — it narrows by typing instead. */
const PAGE_SIZE = 20;

interface FromFileDialogProps {
  onClose: () => void;
  onCreated: (created: Product) => void;
}

/**
 * Full path of every folder, so two files of the same name in different places
 * stay tellable apart. `LibraryFileListItem` carries `folder_id` only — there
 * is no `folder_path` on the wire, and the tree is the only thing that knows
 * the ancestry.
 */
function folderPaths(trees: LibraryFolderTree[] | undefined): Map<number, string> {
  const paths = new Map<number, string>();
  const walk = (node: LibraryFolderTree, prefix: string) => {
    const path = `${prefix}/${node.name}`;
    paths.set(node.id, path);
    for (const child of node.children ?? []) walk(child, path);
  };
  for (const root of trees ?? []) walk(root, '');
  return paths;
}

/**
 * Pick one library file and make a product of it.
 *
 * ⚠️ **Not `LibraryPickerModal`.** That dialog exists to pick a BATCH of files
 * for the print queue: it filters to what is sliced for one printer model
 * (`offerableFiles`), multi-selects into a `Map`, carries a folder tree and
 * hands back `SequencedFile[]`. Every one of those is wrong here — a product
 * can be made from a file no printer was ever chosen for, exactly one file is
 * picked, and the answer is a `Product`. Adapting it would have meant deleting
 * its filter, its selection model and its confirm signature, which is not an
 * adaptation. What IS shared is the folder-name resolution, and that already
 * lives in the tree the server sends.
 *
 * The search goes to the SERVER (`getLibraryFilesPaged`), not through a
 * whole-library fetch filtered in the browser: this dialog opens over a
 * library that may hold tens of thousands of files, and it needs one of them.
 */
export function FromFileDialog({ onClose, onCreated }: FromFileDialogProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const [typed, setTyped] = useState('');
  const [q, setQ] = useState('');

  useEffect(() => {
    const timer = setTimeout(() => setQ(typed.trim()), DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [typed]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const { data: page, isLoading } = useQuery({
    queryKey: ['library-files', 'pick', q],
    queryFn: () => api.getLibraryFilesPaged({ ...(q ? { q } : {}), page: 1, per_page: PAGE_SIZE }),
  });
  const { data: folders } = useQuery({ queryKey: ['library-folders'], queryFn: api.getLibraryFolders });

  const paths = useMemo(() => folderPaths(folders), [folders]);
  const files = page?.items ?? [];

  const create = useMutation({
    mutationFn: (fileId: number) => api.createProductFromFile(fileId),
    onSuccess: (created) => {
      queryClient.invalidateQueries({ queryKey: ['products'] });
      // Its own key, not `products.toast.saved`: nothing was saved here — a
      // product was CREATED, out of a file the operator picked, and the toast
      // is the only confirmation of which of the two happened.
      showToast(t('products.toast.createdFromFile'));
      onCreated(created);
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  return (
    <div className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-50 p-4">
      <div className="bg-bambu-dark-secondary rounded-lg w-full max-w-2xl max-h-[80vh] flex flex-col">
        <div className="flex items-center justify-between gap-3 p-4 border-b border-bambu-dark shrink-0">
          <h2 className="text-lg font-semibold text-white truncate">{t('products.fromFile.title')}</h2>
          <button onClick={onClose} className="p-1 hover:bg-bambu-dark rounded" aria-label={t('common.close')}>
            <X className="w-5 h-5 text-bambu-gray" />
          </button>
        </div>

        <div className="p-3 border-b border-bambu-dark shrink-0">
          <div className="relative">
            <Search className="w-4 h-4 text-bambu-gray absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" />
            <input
              type="search"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              placeholder={t('products.fromFile.search')}
              aria-label={t('products.fromFile.search')}
              className="w-full pl-9 pr-3 py-2 rounded bg-bambu-dark text-sm text-white placeholder:text-bambu-gray focus:outline-none focus:ring-1 focus:ring-bambu-green"
            />
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-3 space-y-2">
          {isLoading ? (
            <div className="py-8 flex items-center justify-center">
              <Loader2 className="w-6 h-6 text-bambu-green animate-spin" />
            </div>
          ) : files.length === 0 ? (
            <p className="text-sm text-bambu-gray italic p-4 text-center">{t('products.fromFile.empty')}</p>
          ) : (
            files.map((file) => {
              const where = file.folder_id === null ? null : paths.get(file.folder_id);
              return (
                <div
                  key={file.id}
                  className="flex items-center gap-3 p-2 rounded border border-bambu-dark-tertiary bg-bambu-dark"
                >
                  <span className="w-10 h-10 shrink-0 rounded bg-bambu-dark-tertiary overflow-hidden flex items-center justify-center">
                    {file.thumbnail_path ? (
                      <img src={api.getLibraryFileThumbnailUrl(file.id)} alt="" className="w-full h-full object-contain" />
                    ) : (
                      <FileBox className="w-5 h-5 text-bambu-gray/50" />
                    )}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block text-sm text-white truncate">{file.filename}</span>
                    {where && <span className="block text-xs text-bambu-gray truncate">{where}</span>}
                  </span>
                  <Button onClick={() => create.mutate(file.id)} disabled={create.isPending}>
                    {t('products.fromFile.create')}
                  </Button>
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}
