import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { File, Folder, X } from 'lucide-react';
import { api } from '../../api/client';
import type { Product } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';

const CHIP_CLASS =
  'inline-flex items-center gap-1.5 px-2 py-1 rounded-lg text-xs bg-bambu-dark-tertiary text-bambu-gray max-w-full';

interface LinkedFilesProps {
  product: Product;
  canEdit: boolean;
}

/**
 * The library files and folders this product is a design for.
 *
 * ⚠️ **There is no picker here** (design decision 4). Linking happens in the
 * File Manager, which already owns the search, the tree and the permissions;
 * a second copy of it inside this page is how the old project page reached
 * 2.4k lines. This section shows what is linked and takes a link away.
 *
 * ⚠️ **The file list is not `library_file_ids`.** `GET /library/files?
 * product_id=` returns the product's direct files UNION the files of its
 * linked folders, which is what the operator means by "this product's files";
 * the id array on the product carries only the direct half. Unlinking a file
 * that arrived through a folder is therefore a 404 — the folder is what holds
 * it — so files are unlinked and folders are unlinked separately, each from
 * its own list.
 */
export function LinkedFiles({ product, canEdit }: LinkedFilesProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const { data: files = [] } = useQuery({
    queryKey: ['product-files', product.id],
    // The product id is the LAST positional argument of `getLibraryFiles`; the
    // ones before it are the folder/scope/tag filters this view does not use.
    queryFn: () => api.getLibraryFiles(undefined, true, undefined, undefined, [], false, product.id),
    enabled: Number.isFinite(product.id),
  });

  const { data: folders = [] } = useQuery({
    queryKey: ['product-folders', product.id],
    queryFn: () => api.getFoldersByProduct(product.id),
    enabled: Number.isFinite(product.id),
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['product', product.id] });
    queryClient.invalidateQueries({ queryKey: ['product-files', product.id] });
    queryClient.invalidateQueries({ queryKey: ['product-folders', product.id] });
    queryClient.invalidateQueries({ queryKey: ['product-plates', product.id] });
    // The File Manager's own chips carry the same links.
    queryClient.invalidateQueries({ queryKey: ['library-files'] });
  };
  const fail = (e: Error) => showToast(e.message, 'error');

  const unlinkFile = useMutation({
    mutationFn: (fileId: number) => api.unlinkProductFile(product.id, fileId),
    onSuccess: invalidate,
    onError: fail,
  });

  const unlinkFolder = useMutation({
    mutationFn: (folderId: number) => api.unlinkProductFolder(product.id, folderId),
    onSuccess: invalidate,
    onError: fail,
  });

  const empty = files.length === 0 && folders.length === 0;

  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold text-white">{t('products.files.title')}</h2>

      {empty && <p className="text-sm text-bambu-gray">{t('products.files.empty')}</p>}

      {folders.length > 0 && (
        <div className="space-y-1">
          <p className="text-xs text-bambu-gray">{t('products.files.folders')}</p>
          <div className="flex items-center gap-2 flex-wrap">
            {folders.map((folder) => (
              <span key={folder.id} className={CHIP_CLASS}>
                <Folder className="w-4 h-4 shrink-0" />
                <span className="truncate">{folder.name}</span>
                {canEdit && (
                  <button
                    type="button"
                    onClick={() => unlinkFolder.mutate(folder.id)}
                    aria-label={`${t('products.files.unlink')}: ${folder.name}`}
                    title={t('products.files.unlink')}
                    className="text-bambu-gray hover:text-white"
                  >
                    <X className="w-3.5 h-3.5" />
                  </button>
                )}
              </span>
            ))}
          </div>
        </div>
      )}

      {files.length > 0 && (
        <div className="space-y-1">
          <p className="text-xs text-bambu-gray">{t('products.files.files')}</p>
          <div className="flex items-center gap-2 flex-wrap">
            {files.map((file) => (
              <span key={file.id} className={CHIP_CLASS}>
                <File className="w-4 h-4 shrink-0" />
                <span className="truncate">{file.filename}</span>
                {canEdit && (
                  <button
                    type="button"
                    onClick={() => unlinkFile.mutate(file.id)}
                    aria-label={`${t('products.files.unlink')}: ${file.filename}`}
                    title={t('products.files.unlink')}
                    className="text-bambu-gray hover:text-white"
                  >
                    <X className="w-3.5 h-3.5" />
                  </button>
                )}
              </span>
            ))}
          </div>
        </div>
      )}

      <p className="text-xs text-bambu-gray">
        {t('products.files.linkHint')}{' '}
        <Link to="/files" className="text-bambu-green hover:underline">
          {t('products.files.openFiles')}
        </Link>
      </p>
    </section>
  );
}
