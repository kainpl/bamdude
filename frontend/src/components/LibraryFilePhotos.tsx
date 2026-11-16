/**
 * The photos attached to a library file: a gallery, a viewer, captions — and,
 * for someone who may edit the file, adding and deleting.
 *
 * The pictures are `<img>`s, so their URLs carry the media token; the server
 * checks the FILE's visibility for every one of them. A photo opens in the
 * image viewer (the preview shell), which sits above the dialog this gallery
 * is shown in.
 *
 * Several files are sent one request each, in the order chosen: a refused one
 * is named with the server's own reason and the rest still go. Dropping files
 * adds them too — onto the gallery, or onto the element the caller names as
 * the drop zone (the details dialog gives its whole body). The server decides
 * what a photo is and saves it again without its metadata — nothing here
 * inspects the bytes.
 */
import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type RefObject } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ImagePlus, Loader2, Trash2 } from 'lucide-react';
import { api, type LibraryFilePhoto } from '../api/client';
import { Button } from './Button';
import { ImagePreviewModal } from './ImagePreviewModal';

/** As the server caps it. */
const CAPTION_MAX_LENGTH = 500;
const ACCEPT = 'image/png,image/jpeg,image/webp';

interface Props {
  fileId: number;
  /** May the viewer edit the file these photos belong to? */
  canEdit: boolean;
  /** Where dropped files are taken — the gallery itself when absent. */
  dropZoneRef?: RefObject<HTMLElement | null>;
}

interface Refusal {
  name: string;
  message: string;
}

function PhotoCaption({ photo, onSave }: { photo: LibraryFilePhoto; onSave: (caption: string | null) => Promise<unknown> }) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState(photo.caption ?? '');
  const [seen, setSeen] = useState(photo.caption);
  // The saved caption changed under us (our own save, or somebody else's): follow it.
  if (seen !== photo.caption) {
    setSeen(photo.caption);
    setDraft(photo.caption ?? '');
  }
  // What was last sent and has not failed. Enter commits and so does leaving
  // the box, and the saved caption comes back only after a round trip: without
  // this the same text went out twice.
  const sent = useRef<{ value: string | null } | null>(null);
  const commit = () => {
    const next = draft.trim() || null;
    if (next === (photo.caption ?? null)) return;
    if (sent.current && sent.current.value === next) return;
    const mine = { value: next };
    sent.current = mine;
    onSave(next).catch(() => {
      // A save that failed may be asked for again, as it is.
      if (sent.current === mine) sent.current = null;
    });
  };
  const onKey = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    commit();
  };
  return (
    <input
      type="text"
      value={draft}
      maxLength={CAPTION_MAX_LENGTH}
      onChange={(e) => {
        sent.current = null;
        setDraft(e.target.value);
      }}
      onBlur={commit}
      onKeyDown={onKey}
      aria-label={t('fileDetails.photos.caption')}
      placeholder={t('fileDetails.photos.captionPlaceholder')}
      className="w-full rounded border border-transparent bg-transparent px-1 py-0.5 text-xs text-white placeholder-bambu-gray hover:border-bambu-dark-tertiary focus:border-bambu-green focus:outline-none"
    />
  );
}

export function LibraryFilePhotos({ fileId, canEdit, dropZoneRef }: Props) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [refusals, setRefusals] = useState<Refusal[]>([]);
  const [confirming, setConfirming] = useState<number | null>(null);
  const [viewing, setViewing] = useState<LibraryFilePhoto | null>(null);
  const [dragging, setDragging] = useState(false);

  const { data: photos, isLoading } = useQuery({
    queryKey: ['library-file-photos', fileId],
    queryFn: () => api.getLibraryFilePhotos(fileId),
  });

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['library-file-photos', fileId] });
    queryClient.invalidateQueries({ queryKey: ['library-file', fileId] });
    queryClient.invalidateQueries({ queryKey: ['library-files'] });
  };

  const upload = async (files: File[]) => {
    if (!canEdit || files.length === 0 || progress) return;
    const refused: Refusal[] = [];
    setRefusals([]);
    setProgress({ done: 0, total: files.length });
    for (const [index, file] of files.entries()) {
      try {
        await api.uploadLibraryFilePhoto(fileId, file);
      } catch (error) {
        refused.push({ name: file.name, message: error instanceof Error ? error.message : t('fileDetails.photos.uploadFailed') });
      }
      setProgress({ done: index + 1, total: files.length });
    }
    setProgress(null);
    setRefusals(refused);
    refresh();
  };

  const labelOf = (photo: LibraryFilePhoto | undefined, index: number) =>
    photo?.caption || photo?.original_name || t('fileDetails.photos.unnamed', { n: index + 1 });

  const caption = useMutation({
    mutationFn: ({ id, value }: { id: number; value: string | null }) => api.updateLibraryFilePhoto(fileId, id, value),
    onSuccess: refresh,
    onError: (error: unknown, { id }) =>
      setRefusals([
        {
          name: labelOf(photos?.find((p) => p.id === id), 0),
          message: error instanceof Error ? error.message : t('fileDetails.photos.uploadFailed'),
        },
      ]),
  });
  const remove = useMutation({
    mutationFn: (id: number) => api.deleteLibraryFilePhoto(fileId, id),
    onSuccess: () => {
      setConfirming(null);
      refresh();
    },
    onError: (error: unknown, id) => {
      const index = photos?.findIndex((p) => p.id === id) ?? -1;
      const reason = error instanceof Error ? error.message : '';
      setRefusals([
        {
          name: labelOf(photos?.[index], Math.max(0, index)),
          message: [t('fileDetails.photos.deleteFailed'), reason].filter(Boolean).join(' '),
        },
      ]);
    },
  });

  // Dropped files. Native listeners, on whichever element is the drop zone;
  // the newest `upload` is reached through a ref so the listeners are attached
  // once per zone. A drop handled here stops: the shell swallows file drops
  // app-wide so that a stray one cannot navigate the browser away.
  const uploadRef = useRef(upload);
  uploadRef.current = upload;
  useEffect(() => {
    const zone = dropZoneRef?.current ?? rootRef.current;
    if (!zone || !canEdit) return;
    const carriesFiles = (e: DragEvent) => Array.from(e.dataTransfer?.types ?? []).includes('Files');
    const onDragOver = (e: DragEvent) => {
      if (!carriesFiles(e)) return;
      e.preventDefault();
      setDragging(true);
    };
    const onDragLeave = (e: DragEvent) => {
      if (!zone.contains(e.relatedTarget as Node | null)) setDragging(false);
    };
    const onDrop = (e: DragEvent) => {
      setDragging(false);
      if (!carriesFiles(e)) return;
      e.preventDefault();
      e.stopPropagation();
      void uploadRef.current(Array.from(e.dataTransfer?.files ?? []));
    };
    zone.addEventListener('dragover', onDragOver);
    zone.addEventListener('dragleave', onDragLeave);
    zone.addEventListener('drop', onDrop);
    return () => {
      zone.removeEventListener('dragover', onDragOver);
      zone.removeEventListener('dragleave', onDragLeave);
      zone.removeEventListener('drop', onDrop);
    };
  }, [canEdit, dropZoneRef]);

  return (
    <div
      ref={rootRef}
      data-testid="library-file-photos"
      className={`space-y-3 rounded-lg border border-dashed p-3 transition-colors ${
        dragging ? 'border-bambu-green bg-bambu-green/5' : 'border-transparent'
      }`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-medium text-white">
          {t('fileDetails.photos.title')}
          {photos && photos.length > 0 && <span className="ml-2 text-bambu-gray tabular-nums">{photos.length}</span>}
        </h3>
        {canEdit && (
          <div className="flex items-center gap-2">
            {progress && (
              <span className="inline-flex items-center gap-1.5 text-xs text-bambu-gray" role="status">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                {t('fileDetails.photos.uploading', { done: progress.done, total: progress.total })}
              </span>
            )}
            <input
              ref={inputRef}
              type="file"
              accept={ACCEPT}
              multiple
              className="sr-only"
              tabIndex={-1}
              aria-label={t('fileDetails.photos.add')}
              onChange={(e) => {
                const chosen = Array.from(e.target.files ?? []);
                e.target.value = '';
                void upload(chosen);
              }}
            />
            <Button variant="secondary" size="sm" onClick={() => inputRef.current?.click()} disabled={progress !== null}>
              <ImagePlus className="h-4 w-4" />
              {t('fileDetails.photos.add')}
            </Button>
          </div>
        )}
      </div>

      {refusals.length > 0 && (
        <ul role="alert" className="space-y-1 text-sm text-red-700 dark:text-red-400">
          {refusals.map((refusal, index) => (
            <li key={index}>
              <span className="font-medium">{refusal.name}</span>: {refusal.message}
            </li>
          ))}
        </ul>
      )}

      {isLoading ? (
        <p className="text-sm text-bambu-gray">{t('common.loading')}</p>
      ) : !photos || photos.length === 0 ? (
        <p className="text-sm text-bambu-gray">{t('fileDetails.photos.none')}</p>
      ) : (
        <ul className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          {photos.map((photo, index) => {
            const label = labelOf(photo, index);
            return (
              <li key={photo.id} className="min-w-0 space-y-1">
                <div className="relative">
                  <button
                    type="button"
                    onClick={() => setViewing(photo)}
                    aria-label={t('fileDetails.photos.view', { name: label })}
                    className="block aspect-square w-full overflow-hidden rounded-lg bg-bambu-dark focus:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green"
                  >
                    <img
                      src={api.getLibraryFilePhotoThumbnailUrl(fileId, photo.id)}
                      alt={label}
                      loading="lazy"
                      className="h-full w-full object-cover"
                    />
                  </button>
                  {canEdit && confirming !== photo.id && (
                    <button
                      type="button"
                      onClick={() => setConfirming(photo.id)}
                      aria-label={t('fileDetails.photos.delete')}
                      title={t('fileDetails.photos.delete')}
                      className="absolute right-1 top-1 rounded-md bg-black/60 p-1.5 text-white transition-colors hover:bg-red-600"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  )}
                </div>
                {canEdit && confirming === photo.id ? (
                  <div className="flex flex-wrap items-center gap-2 text-xs text-white">
                    <span>{t('fileDetails.photos.confirmDelete')}</span>
                    <Button variant="danger" size="sm" onClick={() => remove.mutate(photo.id)} disabled={remove.isPending}>
                      {t('common.delete')}
                    </Button>
                    <Button variant="secondary" size="sm" onClick={() => setConfirming(null)} disabled={remove.isPending}>
                      {t('common.cancel')}
                    </Button>
                  </div>
                ) : canEdit ? (
                  <PhotoCaption photo={photo} onSave={(value) => caption.mutateAsync({ id: photo.id, value })} />
                ) : (
                  photo.caption && <p className="break-words px-1 text-xs text-bambu-gray">{photo.caption}</p>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {canEdit && <p className="text-xs text-bambu-gray">{t('fileDetails.photos.hint')}</p>}

      {viewing && (
        <ImagePreviewModal
          src={api.getLibraryFilePhotoUrl(fileId, viewing.id)}
          title={labelOf(viewing, photos?.findIndex((p) => p.id === viewing.id) ?? 0)}
          fileSize={viewing.file_size}
          onClose={() => setViewing(null)}
        />
      )}
    </div>
  );
}
