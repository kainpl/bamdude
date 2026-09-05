import { useState, useEffect, useId } from 'react';
import DOMPurify from 'dompurify';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  X,
  User,
  Calendar,
  FileText,
  Image,
  Edit3,
  Save,
  ExternalLink,
  ChevronLeft,
  ChevronRight,
  Copyright,
  Download,
  Loader2,
  Package,
  RefreshCw,
} from 'lucide-react';
import { api, getAuthToken, withStreamToken } from '../api/client';
import { invalidateOrderViews } from '../utils/queryInvalidation';
import type { CardAux } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { useToast } from '../contexts/ToastContext';
import { cardNotesText } from './products/cardNotes';
import { formatFileSize } from '../utils/file';
import { Button } from './Button';
import { RichTextEditor } from './RichTextEditor';
import { useDialogFocus } from '../hooks/useDialogFocus';

/**
 * Which 3MF the card is read from.
 *
 * ⚠️ **The two are not the same document and must never be merged.** An ARCHIVE
 * is BamDude's own copy of a print that happened, and its card is editable —
 * `PATCH /archives/{id}/project-page` rewrites the metadata inside that copy. A
 * library FILE is somebody's source of truth: it is read on every request and
 * never written (spec §Risks — "never write into a library 3MF"). What the file
 * half offers instead is a way out to a product, which IS database data.
 */
export type ModelCardSource =
  | { kind: 'archive'; id: number; name?: string }
  | {
      kind: 'file';
      id: number;
      name?: string;
      /** Products this file is already linked to. Their cards can be re-read
       *  from it; a file linked to nothing offers no re-read at all. */
      linkedProductIds?: number[];
    };

interface ModelCardModalProps {
  source: ModelCardSource;
  onClose: () => void;
}

/**
 * The model card of an archive or of a library file.
 *
 * One entry point, two bodies — see :type:`ModelCardSource` for why they stay
 * apart rather than sharing a "card" abstraction that would have to carry an
 * `editable` flag through every field.
 */
export function ModelCardModal({ source, onClose }: ModelCardModalProps) {
  return source.kind === 'archive' ? (
    <ArchiveCard archiveId={source.id} archiveName={source.name} onClose={onClose} />
  ) : (
    <FileCard fileId={source.id} fileName={source.name} linkedProductIds={source.linkedProductIds} onClose={onClose} />
  );
}

interface ArchiveCardProps {
  archiveId: number;
  archiveName?: string;
  onClose: () => void;
}

function ArchiveCard({ archiveId, archiveName, onClose }: ArchiveCardProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const titleId = useId();
  // Mounted only while it is open, so "open" is simply `true`.
  const dialog = useDialogFocus<HTMLDivElement>(true);
  const [isEditing, setIsEditing] = useState(false);
  const [selectedImageIndex, setSelectedImageIndex] = useState<number | null>(null);
  // The picture overlay over this one, same treatment: focus in on open, back
  // to the thumbnail on close. Not a trap — see `useDialogFocus`.
  const lightbox = useDialogFocus<HTMLDivElement>(selectedImageIndex !== null);
  const [editData, setEditData] = useState<{
    title?: string;
    description?: string;
    designer?: string;
    license?: string;
    profile_title?: string;
    profile_description?: string;
  }>({});

  const { data: projectPage, isLoading, error } = useQuery({
    queryKey: ['archive-project-page', archiveId],
    queryFn: () => api.getArchiveProjectPage(archiveId),
  });

  const updateMutation = useMutation({
    mutationFn: (data: typeof editData) => api.updateArchiveProjectPage(archiveId, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['archive-project-page', archiveId] });
      setIsEditing(false);
      setEditData({});
    },
  });

  // Handle escape key to close modal
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (selectedImageIndex !== null) {
          setSelectedImageIndex(null);
        } else if (isEditing) {
          handleCancelEdit();
        } else {
          onClose();
        }
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [selectedImageIndex, isEditing, onClose]);

  // Combine all images for gallery
  const allImages = [
    ...(projectPage?.model_pictures || []),
    ...(projectPage?.profile_pictures || []),
  ];

  const handleStartEdit = () => {
    setEditData({
      title: projectPage?.title || '',
      description: projectPage?.description || '',
      designer: projectPage?.designer || '',
      license: projectPage?.license || '',
      profile_title: projectPage?.profile_title || '',
      profile_description: projectPage?.profile_description || '',
    });
    setIsEditing(true);
  };

  const handleSave = () => {
    updateMutation.mutate(editData);
  };

  const handleCancelEdit = () => {
    setIsEditing(false);
    setEditData({});
  };

  // Sanitize HTML content using DOMPurify
  const sanitizeHtml = (html: string) => {
    return DOMPurify.sanitize(html, {
      ALLOWED_TAGS: ['p', 'br', 'b', 'strong', 'i', 'em', 'u', 'a', 'ul', 'ol', 'li', 'figure', 'img'],
      ALLOWED_ATTR: ['href', 'src', 'target', 'rel', 'style'],
      ADD_ATTR: ['target'],
    });
  };

  const hasContent = projectPage && (
    projectPage.title ||
    projectPage.description ||
    projectPage.designer ||
    projectPage.profile_title ||
    allImages.length > 0
  );

  // Handle backdrop click to close modal
  const handleBackdropClick = (e: React.MouseEvent) => {
    if (e.target === e.currentTarget) {
      onClose();
    }
  };

  return (
    <div
      className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-50 p-4"
      onClick={handleBackdropClick}
    >
      {/* ⚠️ The role, the name and the focus, as one unit — see
          `useDialogFocus`, which lists every overlay that uses it and says
          exactly what it does and does not do. Without them the overlay is an
          anonymous `<div>` a screen reader never announces, and a keyboard user
          opening it starts at the top of the PAGE behind. */}
      <div
        ref={dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className="bg-bambu-dark-secondary rounded-xl max-w-4xl w-full max-h-[90vh] overflow-hidden flex flex-col outline-none"
      >
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
          <div className="flex items-center gap-3">
            <FileText className="w-5 h-5 text-bambu-green" />
            {/* ⚠️ The load, the edit form and the PATCH below are the
                project-page dialog byte for byte and are pinned by
                `__tests__/components/ModelCardModal.test.tsx`. Pass 6 moved the
                STRINGS and nothing else: they were written into this JSX in
                English, on a screen whose other half has been translatable
                since the day it was written.

                FOUR pieces of text changed, and one of them is not a pure
                move: `Edit`, `Cancel` and `Save` became `modelCard.edit` /
                `.cancel` / `.save`, while the `Print Profile` heading took the
                SHARED `modelCard.printProfile` the file half already used — so
                the English now reads "Print profile", one heading in one
                spelling on both halves, instead of two capitalisations of the
                same word depending on which 3MF you opened. */}
            <h2 id={titleId} className="text-lg font-semibold text-white">
              {t('modelCard.title')}
              {archiveName && <span className="text-bambu-gray ml-2">- {archiveName}</span>}
            </h2>
          </div>
          <div className="flex items-center gap-2">
            {!isEditing && hasContent && (
              <Button variant="ghost" size="sm" onClick={handleStartEdit}>
                <Edit3 className="w-4 h-4 mr-1" />
                {t('modelCard.edit')}
              </Button>
            )}
            {isEditing && (
              <>
                <Button variant="ghost" size="sm" onClick={handleCancelEdit}>
                  {t('modelCard.cancel')}
                </Button>
                <Button
                  variant="primary"
                  size="sm"
                  onClick={handleSave}
                  disabled={updateMutation.isPending}
                >
                  <Save className="w-4 h-4 mr-1" />
                  {t('modelCard.save')}
                </Button>
              </>
            )}
            <button
              onClick={onClose}
              aria-label={t('common.close')}
              className="p-2 hover:bg-bambu-dark-tertiary rounded-lg transition-colors"
            >
              <X className="w-5 h-5 text-bambu-gray" />
            </button>
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-6">
          {isLoading && (
            <div className="flex items-center justify-center py-12">
              <div className="animate-spin rounded-full h-8 w-8 border-2 border-bambu-green border-t-transparent" />
            </div>
          )}

          {error && (
            <div className="text-red-700 dark:text-red-400 text-center py-12">
              {t('modelCard.archiveLoadFailed')}
            </div>
          )}

          {projectPage && !hasContent && (
            <div className="text-bambu-gray text-center py-12">
              <FileText className="w-12 h-12 mx-auto mb-4 opacity-50" />
              <p>{t('modelCard.archiveEmpty')}</p>
              <p className="text-sm mt-2">{t('modelCard.archiveEmptyHint')}</p>
            </div>
          )}

          {projectPage && hasContent && (
            <div className="space-y-6">
              {/* Title & Designer */}
              <div className="space-y-4">
                {isEditing ? (
                  <input
                    type="text"
                    value={editData.title || ''}
                    onChange={(e) => setEditData({ ...editData, title: e.target.value })}
                    placeholder={t('modelCard.field.title')}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded-lg px-4 py-2 text-white text-xl font-semibold"
                  />
                ) : (
                  projectPage.title && (
                    <h3 className="text-xl font-semibold text-white">{projectPage.title}</h3>
                  )
                )}

                <div className="flex flex-wrap gap-4 text-sm">
                  {isEditing ? (
                    <div className="flex items-center gap-2">
                      <User className="w-4 h-4 text-bambu-gray" />
                      <input
                        type="text"
                        value={editData.designer || ''}
                        onChange={(e) => setEditData({ ...editData, designer: e.target.value })}
                        placeholder={t('modelCard.field.designer')}
                        className="bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1 text-white"
                      />
                    </div>
                  ) : (
                    projectPage.designer && (
                      <div className="flex items-center gap-2 text-bambu-gray">
                        <User className="w-4 h-4" />
                        <span>{projectPage.designer}</span>
                        {projectPage.designer_user_id && (
                          <a
                            href={`https://makerworld.com/en/@${projectPage.designer_user_id}`}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-bambu-green hover:underline"
                          >
                            <ExternalLink className="w-3 h-3" />
                          </a>
                        )}
                      </div>
                    )
                  )}

                  {projectPage.creation_date && (
                    <div className="flex items-center gap-2 text-bambu-gray">
                      <Calendar className="w-4 h-4" />
                      <span>{projectPage.creation_date}</span>
                    </div>
                  )}

                  {isEditing ? (
                    <div className="flex items-center gap-2">
                      <FileText className="w-4 h-4 text-bambu-gray" />
                      <input
                        type="text"
                        value={editData.license || ''}
                        onChange={(e) => setEditData({ ...editData, license: e.target.value })}
                        placeholder={t('modelCard.field.license')}
                        className="bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1 text-white"
                      />
                    </div>
                  ) : (
                    projectPage.license && (
                      <div className="flex items-center gap-2 text-bambu-gray">
                        <FileText className="w-4 h-4" />
                        <span>{projectPage.license}</span>
                      </div>
                    )
                  )}

                  {projectPage.origin && (
                    <span className="px-2 py-0.5 bg-bambu-dark rounded text-bambu-gray">
                      {projectPage.origin}
                    </span>
                  )}
                </div>
              </div>

              {/* Description */}
              {(projectPage.description || isEditing) && (
                <div className="space-y-2">
                  <h4 className="text-sm font-medium text-bambu-gray uppercase tracking-wide">
                    {t('modelCard.description')}
                  </h4>
                  {isEditing ? (
                    <RichTextEditor
                      content={editData.description || ''}
                      onChange={(html) => setEditData({ ...editData, description: html })}
                      placeholder={t('modelCard.field.description')}
                    />
                  ) : (
                    <div
                      className="prose prose-invert prose-sm max-w-none text-bambu-gray-light"
                      dangerouslySetInnerHTML={{
                        __html: sanitizeHtml(projectPage.description || ''),
                      }}
                    />
                  )}
                </div>
              )}

              {/* Profile Info */}
              {(projectPage.profile_title || projectPage.profile_description || isEditing) && (
                <div className="space-y-2 p-4 bg-bambu-dark rounded-lg">
                  <h4 className="text-sm font-medium text-bambu-gray uppercase tracking-wide">
                    {t('modelCard.printProfile')}
                  </h4>
                  {isEditing ? (
                    <div className="space-y-2">
                      <input
                        type="text"
                        value={editData.profile_title || ''}
                        onChange={(e) => setEditData({ ...editData, profile_title: e.target.value })}
                        placeholder={t('modelCard.field.profileTitle')}
                        className="w-full bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded px-3 py-2 text-white"
                      />
                      <RichTextEditor
                        content={editData.profile_description || ''}
                        onChange={(html) => setEditData({ ...editData, profile_description: html })}
                        placeholder={t('modelCard.field.profileDescription')}
                      />
                    </div>
                  ) : (
                    <>
                      {projectPage.profile_title && (
                        <p className="text-white font-medium">{projectPage.profile_title}</p>
                      )}
                      {projectPage.profile_description && (
                        <div
                          className="prose prose-invert prose-sm max-w-none text-bambu-gray-light"
                          dangerouslySetInnerHTML={{
                            __html: sanitizeHtml(projectPage.profile_description),
                          }}
                        />
                      )}
                      {projectPage.profile_user_name && (
                        <p className="text-sm text-bambu-gray">
                          {t('modelCard.byAuthor', { name: projectPage.profile_user_name })}
                        </p>
                      )}
                    </>
                  )}
                </div>
              )}

              {/* Image Gallery */}
              {allImages.length > 0 && (
                <div className="space-y-2">
                  <h4 className="text-sm font-medium text-bambu-gray uppercase tracking-wide flex items-center gap-2">
                    <Image className="w-4 h-4" />
                    {t('modelCard.images', { count: allImages.length })}
                  </h4>
                  <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 gap-2">
                    {allImages.map((img, index) => (
                      <button
                        key={img.path}
                        onClick={() => setSelectedImageIndex(index)}
                        className="aspect-square rounded-lg overflow-hidden border border-bambu-dark-tertiary hover:border-bambu-green transition-colors"
                      >
                        <img
                          src={img.url}
                          alt={img.name}
                          className="w-full h-full object-cover"
                        />
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {/* MakerWorld Link */}
              {projectPage.design_model_id && (
                <div className="pt-4 border-t border-bambu-dark-tertiary">
                  <a
                    href={`https://makerworld.com/en/models/${projectPage.design_model_id}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-2 text-bambu-green hover:underline"
                  >
                    <ExternalLink className="w-4 h-4" />
                    {t('modelCard.viewOnMakerWorld')}
                  </a>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Image Lightbox */}
      {selectedImageIndex !== null && allImages[selectedImageIndex] && (
        <div
          ref={lightbox}
          role="dialog"
          aria-modal="true"
          aria-label={t('modelCard.pictureViewer')}
          tabIndex={-1}
          data-testid="archive-lightbox"
          className="fixed inset-0 bg-black/90 backdrop-blur-sm flex items-center justify-center z-60 outline-none"
          onClick={() => setSelectedImageIndex(null)}
        >
          <button
            onClick={(e) => {
              e.stopPropagation();
              setSelectedImageIndex(Math.max(0, selectedImageIndex - 1));
            }}
            disabled={selectedImageIndex === 0}
            aria-label={t('modelCard.previous')}
            className="absolute left-4 p-2 bg-bambu-dark-secondary rounded-full hover:bg-bambu-dark-tertiary disabled:opacity-30"
          >
            <ChevronLeft className="w-6 h-6 text-white" />
          </button>

          <img
            src={allImages[selectedImageIndex].url}
            alt={allImages[selectedImageIndex].name}
            className="max-w-[90vw] max-h-[90vh] object-contain"
            onClick={(e) => e.stopPropagation()}
          />

          <button
            onClick={(e) => {
              e.stopPropagation();
              setSelectedImageIndex(Math.min(allImages.length - 1, selectedImageIndex + 1));
            }}
            disabled={selectedImageIndex === allImages.length - 1}
            aria-label={t('modelCard.next')}
            className="absolute right-4 p-2 bg-bambu-dark-secondary rounded-full hover:bg-bambu-dark-tertiary disabled:opacity-30"
          >
            <ChevronRight className="w-6 h-6 text-white" />
          </button>

          <button
            onClick={() => setSelectedImageIndex(null)}
            aria-label={t('common.close')}
            className="absolute top-4 right-4 p-2 bg-bambu-dark-secondary rounded-full hover:bg-bambu-dark-tertiary"
          >
            <X className="w-6 h-6 text-white" />
          </button>

          <div className="absolute bottom-4 text-white text-sm">
            {selectedImageIndex + 1} / {allImages.length}
          </div>
        </div>
      )}
    </div>
  );
}

interface FileCardProps {
  fileId: number;
  fileName?: string;
  linkedProductIds?: number[];
  onClose: () => void;
}

/** A member the server said may go behind a camera stream token — i.e. one an
 *  `<img>` can render. ⚠️ Read off the URL the server BUILT, never re-derived
 *  from the category: which of the two routes serves a member is a server rule
 *  (a designer's stray `.txt` inside `Model Pictures/` is a download), and a
 *  second copy of that rule here would be the copy that goes stale.
 *
 *  ⚠️ Anchored to the WHOLE route — `^/api/v1/library/files/<id>/card-file/` —
 *  not a bare `includes`, and not the tail of it either. The rest of the url is
 *  the member's own path INSIDE the 3MF, percent-encoded, so a designer who put
 *  their bill of materials in a folder called `card-file` would have had it
 *  rendered as a broken `<img>` on a token surface it is deliberately not
 *  served from — and a folder called `files/9/card-file` still satisfied the
 *  un-anchored form. `_card_route` in `routes/library.py` builds exactly this
 *  prefix and nothing else can produce it. */
const isPicture = (member: CardAux) => /^\/api\/v1\/library\/files\/\d+\/card-file\//.test(member.url);

/**
 * What a LIBRARY 3MF says about itself — read-only, and a way out to a product.
 *
 * ⚠️ **Nothing here writes.** The archive half of this modal edits its own copy
 * of the metadata; a library file is somebody's source of truth and the card is
 * database data (spec §Risks). So the two actions are "make a product of this
 * file" and "fill an existing product's BLANK fields from it" — both of which
 * change a product row and leave the 3MF untouched.
 *
 * ⚠️ **Pictures carry the stream token, documents carry the bearer.** An
 * `<img src>` cannot send an Authorization header, so it goes through the
 * token-gated `card-file` route; a bill of materials is a document about
 * somebody's business and has no reason to sit behind a long-lived kiosk
 * credential, so it is fetched with the bearer and saved as a blob.
 */
function FileCard({ fileId, fileName, linkedProductIds, onClose }: FileCardProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { hasPermission } = useAuth();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const [lightbox, setLightbox] = useState<number | null>(null);
  const [rereadOpen, setRereadOpen] = useState(false);
  const [downloading, setDownloading] = useState<string | null>(null);
  const titleId = useId();
  // Mounted only while it is open, so "open" is simply `true`.
  const dialog = useDialogFocus<HTMLDivElement>(true);

  const { data: card, isLoading, error } = useQuery({
    queryKey: ['library-file-card', fileId],
    queryFn: () => api.getLibraryFileCard(fileId),
  });

  const linked = linkedProductIds ?? [];

  // The names of the products this file is linked to. One LIST request rather
  // than N detail requests: the picker needs a name per id and nothing else,
  // and the catalog is the only answer that holds every name at once. No
  // `active` filter — a product taken out of the catalog still has a card to
  // fill. Fetched only once the picker is opened.
  const { data: products = [] } = useQuery({
    queryKey: ['products', 'card-reread-targets'],
    queryFn: () => api.getProducts(),
    enabled: rereadOpen && linked.length > 0,
  });

  const members = Object.entries(card?.auxiliaries ?? {}).flatMap(([category, list]) =>
    (list ?? []).map((member) => ({ ...member, category })),
  );
  const pictures = members.filter(isPicture);
  const documents = members.filter((member) => !isPicture(member));

  // Everything the body below can actually RENDER. `design_model_id` and
  // `copyright` are in here because the body renders both — a file whose card
  // carries only a MakerWorld id would otherwise be reported as having no model
  // card at all, with the link to that model sitting right underneath.
  const hasContent = Boolean(
    card &&
      (card.title ||
        card.description ||
        card.designer ||
        card.license ||
        card.copyright ||
        card.profile_title ||
        card.design_model_id ||
        members.length > 0),
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      if (lightbox !== null) setLightbox(null);
      else if (rereadOpen) setRereadOpen(false);
      else onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [lightbox, rereadOpen, onClose]);

  // ⚠️ Focus moves INTO the overlay when it opens and back to the thumbnail
  // when it closes — and that is ALL it does. **Tab is not trapped**: it walks
  // out of the overlay and into the modal behind it. What the move fixes is the
  // two ends, which without it leave a keyboard user starting at the top of the
  // document and, on close, with the focus ring on `<body>` — nowhere.
  const overlay = useDialogFocus<HTMLDivElement>(lightbox !== null);

  const sanitizeHtml = (html: string) =>
    DOMPurify.sanitize(html, {
      ALLOWED_TAGS: ['p', 'br', 'b', 'strong', 'i', 'em', 'u', 'a', 'ul', 'ol', 'li', 'figure', 'img'],
      ALLOWED_ATTR: ['href', 'src', 'target', 'rel', 'style'],
      ADD_ATTR: ['target'],
    });

  const create = useMutation({
    mutationFn: () => api.createProductFromFile(fileId),
    onSuccess: (product) => {
      queryClient.invalidateQueries({ queryKey: ['products'] });
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      onClose();
      navigate(`/products/${product.id}`);
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const reread = useMutation({
    mutationFn: (productId: number) => api.rereadProductCard(productId, fileId),
    onSuccess: (result) => {
      // ⚠️ The product keys ARE order views since Ruling 29 (the shelf moves
      // with an order's lines), so this one call covers them. It is needed for
      // its own sake too: the re-read can give the product its FIRST cover, and
      // an order card renders that cover off the projects query.
      invalidateOrderViews(queryClient);
      setRereadOpen(false);
      showToast(cardNotesText(t, result.notes));
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  /** Bearer download of one non-picture member. An `<a href>` cannot carry the
   *  token and would save the 401 body under the operator's filename. */
  const download = async (member: CardAux) => {
    setDownloading(member.zip_path);
    try {
      const token = getAuthToken();
      const response = await fetch(member.url, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
      if (!response.ok) {
        showToast(t('modelCard.downloadFailed', { name: member.name, status: response.status }), 'error');
        return;
      }
      const url = window.URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = url;
      link.download = member.name;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      window.URL.revokeObjectURL(url);
    } catch (e) {
      showToast((e as Error).message, 'error');
    } finally {
      setDownloading(null);
    }
  };

  const handleBackdropClick = (e: React.MouseEvent) => {
    if (e.target === e.currentTarget) onClose();
  };

  return (
    <div
      className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-50 p-4"
      onClick={handleBackdropClick}
    >
      {/* ⚠️ The role, the name and the focus, as one unit — see
          `useDialogFocus`, which lists every overlay that uses it and says
          exactly what it does and does not do. Without them the overlay is an
          anonymous `<div>` a screen reader never announces, and a keyboard user
          opening it starts at the top of the PAGE behind. */}
      <div
        ref={dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className="bg-bambu-dark-secondary rounded-xl max-w-4xl w-full max-h-[90vh] overflow-hidden flex flex-col outline-none"
      >
        <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
          <div className="flex items-center gap-3 min-w-0">
            <FileText className="w-5 h-5 text-bambu-green shrink-0" />
            <h2 id={titleId} className="text-lg font-semibold text-white truncate">
              {t('modelCard.title')}
              {fileName && <span className="text-bambu-gray ml-2">- {fileName}</span>}
            </h2>
          </div>
          <div className="flex items-center gap-2">
            {hasPermission('projects:create') && (
              <Button variant="secondary" size="sm" onClick={() => create.mutate()} disabled={create.isPending}>
                {create.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Package className="w-4 h-4" />}
                {t('modelCard.createProduct')}
              </Button>
            )}
            {hasPermission('projects:update') && linked.length > 0 && (
              <div className="relative">
                {/* ⚠️ `haspopup` + `expanded` on the TRIGGER, not on the menu.
                    The menu below already has `role="menu"`, but a screen reader
                    reaches the button first and, without these, announces it as
                    an ordinary button — nothing says a menu is about to open, or
                    that one is already open. */}
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  aria-haspopup="menu"
                  aria-expanded={rereadOpen}
                  onClick={() => setRereadOpen((v) => !v)}
                  disabled={reread.isPending}
                >
                  {reread.isPending ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : (
                    <RefreshCw className="w-4 h-4" />
                  )}
                  {t('modelCard.rereadInto')}
                </Button>
                {rereadOpen && (
                  <>
                    <div className="fixed inset-0 z-10" onClick={() => setRereadOpen(false)} />
                    <div
                      role="menu"
                      className="absolute right-0 top-full mt-1 z-20 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl py-1 min-w-[220px] max-h-64 overflow-y-auto"
                    >
                      {linked.map((productId) => (
                        <button
                          key={productId}
                          type="button"
                          role="menuitem"
                          className="w-full px-3 py-2 text-left text-sm text-white hover:bg-bambu-dark truncate"
                          onClick={() => reread.mutate(productId)}
                        >
                          {products.find((p) => p.id === productId)?.name ?? `#${productId}`}
                        </button>
                      ))}
                    </div>
                  </>
                )}
              </div>
            )}
            <button
              onClick={onClose}
              aria-label={t('common.close')}
              className="p-2 hover:bg-bambu-dark-tertiary rounded-lg transition-colors"
            >
              <X className="w-5 h-5 text-bambu-gray" />
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-6">
          {isLoading && (
            <div className="flex items-center justify-center py-12">
              <div className="animate-spin rounded-full h-8 w-8 border-2 border-bambu-green border-t-transparent" />
            </div>
          )}

          {error && <div className="text-red-700 dark:text-red-400 text-center py-12">{t('modelCard.loadFailed')}</div>}

          {/* The request SUCCEEDED and the parser did not: the card degrades to
              a sentence naming why, rather than an empty form that reads like a
              3MF with nothing in it. */}
          {card?.error && (
            <div className="mb-4 rounded-lg bg-bambu-dark p-3 text-sm text-yellow-600 dark:text-yellow-400">
              {t('modelCard.unreadable', { error: card.error })}
            </div>
          )}

          {card && !card.error && !hasContent && (
            <div className="text-bambu-gray text-center py-12">
              <FileText className="w-12 h-12 mx-auto mb-4 opacity-50" />
              <p>{t('modelCard.empty')}</p>
            </div>
          )}

          {card && hasContent && (
            <div className="space-y-6">
              <div className="space-y-4">
                {card.title && <h3 className="text-xl font-semibold text-white">{card.title}</h3>}

                <div className="flex flex-wrap gap-4 text-sm">
                  {card.designer && (
                    <div className="flex items-center gap-2 text-bambu-gray">
                      <User className="w-4 h-4" />
                      <span>{card.designer}</span>
                      {card.designer_user_id && (
                        <a
                          href={`https://makerworld.com/en/@${card.designer_user_id}`}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-bambu-green hover:underline"
                          aria-label={t('modelCard.designerProfile')}
                        >
                          <ExternalLink className="w-3 h-3" />
                        </a>
                      )}
                    </div>
                  )}
                  {card.creation_date && (
                    <div className="flex items-center gap-2 text-bambu-gray">
                      <Calendar className="w-4 h-4" />
                      <span>{card.creation_date}</span>
                    </div>
                  )}
                  {card.license && (
                    <div className="flex items-center gap-2 text-bambu-gray">
                      <FileText className="w-4 h-4" />
                      <span>{card.license}</span>
                    </div>
                  )}
                  {/* The © glyph is the label — a legal notice is the designer's
                      own words in the designer's own language, and translating
                      the word "Copyright" around it would say nothing extra. */}
                  {card.copyright && (
                    <div className="flex items-center gap-2 text-bambu-gray">
                      <Copyright className="w-4 h-4" />
                      <span>{card.copyright}</span>
                    </div>
                  )}
                  {card.origin && (
                    <span className="px-2 py-0.5 bg-bambu-dark rounded text-bambu-gray">{card.origin}</span>
                  )}
                </div>
              </div>

              {card.description && (
                <div className="space-y-2">
                  <h4 className="text-sm font-medium text-bambu-gray uppercase tracking-wide">
                    {t('modelCard.description')}
                  </h4>
                  <div
                    className="prose prose-invert prose-sm max-w-none text-bambu-gray-light"
                    dangerouslySetInnerHTML={{ __html: sanitizeHtml(card.description) }}
                  />
                </div>
              )}

              {(card.profile_title || card.profile_description) && (
                <div className="space-y-2 p-4 bg-bambu-dark rounded-lg">
                  <h4 className="text-sm font-medium text-bambu-gray uppercase tracking-wide">
                    {t('modelCard.printProfile')}
                  </h4>
                  {card.profile_title && <p className="text-white font-medium">{card.profile_title}</p>}
                  {card.profile_description && (
                    <div
                      className="prose prose-invert prose-sm max-w-none text-bambu-gray-light"
                      dangerouslySetInnerHTML={{ __html: sanitizeHtml(card.profile_description) }}
                    />
                  )}
                  {card.profile_user_name && <p className="text-sm text-bambu-gray">{card.profile_user_name}</p>}
                </div>
              )}

              {pictures.length > 0 && (
                <div className="space-y-2">
                  <h4 className="text-sm font-medium text-bambu-gray uppercase tracking-wide flex items-center gap-2">
                    <Image className="w-4 h-4" />
                    {t('modelCard.pictures', { count: pictures.length })}
                  </h4>
                  <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 gap-2">
                    {pictures.map((picture, index) => (
                      <button
                        key={picture.zip_path}
                        type="button"
                        onClick={() => setLightbox(index)}
                        className="aspect-square rounded-lg overflow-hidden border border-bambu-dark-tertiary hover:border-bambu-green transition-colors"
                      >
                        <img src={withStreamToken(picture.url)} alt={picture.name} className="w-full h-full object-cover" />
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {documents.length > 0 && (
                <div className="space-y-2">
                  <h4 className="text-sm font-medium text-bambu-gray uppercase tracking-wide">
                    {t('modelCard.documents')}
                  </h4>
                  <ul className="space-y-2">
                    {documents.map((member) => (
                      <li
                        key={member.zip_path}
                        className="flex items-center gap-3 rounded-lg bg-bambu-dark border border-bambu-dark-tertiary px-3 py-2"
                      >
                        <div className="min-w-0 flex-1">
                          <p className="text-sm text-white truncate">{member.name}</p>
                          <p className="text-xs text-bambu-gray">
                            {t(`products.attachments.category.${member.category}`, { defaultValue: member.category })}
                            {member.size > 0 && ` · ${formatFileSize(member.size)}`}
                          </p>
                        </div>
                        <button
                          type="button"
                          onClick={() => download(member)}
                          disabled={downloading === member.zip_path}
                          aria-label={`${t('common.download')}: ${member.name}`}
                          className="p-1.5 rounded text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary transition-colors disabled:opacity-50"
                        >
                          <Download className="w-4 h-4" />
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {card.design_model_id && (
                <div className="pt-4 border-t border-bambu-dark-tertiary">
                  <a
                    href={`https://makerworld.com/en/models/${card.design_model_id}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-2 text-bambu-green hover:underline"
                  >
                    <ExternalLink className="w-4 h-4" />
                    {t('modelCard.viewOnMakerWorld')}
                  </a>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {lightbox !== null && pictures[lightbox] && (
        <div
          ref={overlay}
          role="dialog"
          aria-modal="true"
          aria-label={t('modelCard.pictureViewer')}
          tabIndex={-1}
          data-testid="card-lightbox"
          className="fixed inset-0 bg-black/90 backdrop-blur-sm flex items-center justify-center z-60 outline-none"
          onClick={() => setLightbox(null)}
        >
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              setLightbox(Math.max(0, lightbox - 1));
            }}
            disabled={lightbox === 0}
            aria-label={t('modelCard.previous')}
            className="absolute left-4 p-2 bg-bambu-dark-secondary rounded-full hover:bg-bambu-dark-tertiary disabled:opacity-30"
          >
            <ChevronLeft className="w-6 h-6 text-white" />
          </button>
          <img
            src={withStreamToken(pictures[lightbox].url)}
            alt={pictures[lightbox].name}
            className="max-w-[90vw] max-h-[90vh] object-contain"
            onClick={(e) => e.stopPropagation()}
          />
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              setLightbox(Math.min(pictures.length - 1, lightbox + 1));
            }}
            disabled={lightbox === pictures.length - 1}
            aria-label={t('modelCard.next')}
            className="absolute right-4 p-2 bg-bambu-dark-secondary rounded-full hover:bg-bambu-dark-tertiary disabled:opacity-30"
          >
            <ChevronRight className="w-6 h-6 text-white" />
          </button>
          <button
            type="button"
            onClick={() => setLightbox(null)}
            aria-label={t('common.close')}
            className="absolute top-4 right-4 p-2 bg-bambu-dark-secondary rounded-full hover:bg-bambu-dark-tertiary"
          >
            <X className="w-6 h-6 text-white" />
          </button>
        </div>
      )}
    </div>
  );
}
