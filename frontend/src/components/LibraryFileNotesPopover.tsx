import { useState, useEffect, useRef, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  X,
  ChevronLeft,
  ChevronRight,
  MessageSquarePlus,
  Pencil,
  Trash2,
  Check,
  Loader2,
} from 'lucide-react';
import { api, LIBRARY_FILE_NOTE_MAX_LENGTH, type LibraryFileNote } from '../api/client';
import { useToast } from '../contexts/ToastContext';
import { useAuth } from '../contexts/AuthContext';
import { ConfirmModal } from './ConfirmModal';

type Mode = 'view' | 'create' | 'edit';

interface Props {
  fileId: number;
  /** Open state controlled by parent (so the trigger button stays in sync). */
  open: boolean;
  /** Anchor element used for positioning. */
  anchorRef: React.RefObject<HTMLElement | null>;
  onClose: () => void;
  /** Called whenever the notes count for this file changes. */
  onCountChange?: (newCount: number) => void;
  /**
   * Horizontal alignment of the popover relative to the anchor:
   * - `'right'` (default) - popover's right edge aligns with anchor's right
   *   edge; popover extends to the left. Good for inline row actions (list view).
   * - `'left'`  - popover's left edge aligns with anchor's left edge; popover
   *   extends to the right. Good for icons anchored at a card's bottom-left.
   */
  align?: 'left' | 'right';
}

export function LibraryFileNotesPopover({ fileId, open, anchorRef, onClose, onCountChange, align = 'right' }: Props) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const { authEnabled } = useAuth();
  const queryClient = useQueryClient();
  const popoverRef = useRef<HTMLDivElement | null>(null);

  const [mode, setMode] = useState<Mode>('view');
  const [currentIndex, setCurrentIndex] = useState(0);
  const [draft, setDraft] = useState('');
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null);
  // Viewport-relative coords for the portal-rendered popover (so it escapes
  // the card's overflow:hidden clip). Either `left` or `right` is set depending
  // on `align`; the other stays undefined so CSS doesn't stretch the popover.
  // Recomputed on resize/scroll.
  const [coords, setCoords] = useState<{ top: number; left?: number; right?: number } | null>(null);

  const POPOVER_WIDTH = 320; // matches the w-80 utility below
  const POPOVER_GAP = 8;     // 8 px gap between anchor and popover

  const queryKey = ['library-file-notes', fileId];

  const { data: notes, isLoading } = useQuery<LibraryFileNote[]>({
    queryKey,
    queryFn: () => api.getLibraryFileNotes(fileId),
    enabled: open,
  });

  // On open with an empty file, jump straight to the create form. We do this
  // once per (open, isLoading, notes) transition; manual mode switches via
  // handleStartCreate / handleStartEdit are NOT reverted by this effect
  // (which used to auto-flip 'create' back to 'view' the moment notes were
  // non-empty - clashing with the user clicking the "+" button).
  const didAutoOpenRef = useRef(false);
  useEffect(() => {
    if (!open) {
      didAutoOpenRef.current = false;
      return;
    }
    if (isLoading) return;
    if (didAutoOpenRef.current) return;
    if ((notes?.length ?? 0) === 0) {
      setMode('create');
      setDraft('');
    }
    didAutoOpenRef.current = true;
  }, [open, isLoading, notes]);

  // Reset state when closed so the next open starts fresh.
  useEffect(() => {
    if (!open) {
      setMode('view');
      setCurrentIndex(0);
      setDraft('');
      setConfirmDeleteId(null);
    }
  }, [open]);

  // Clamp index when notes get shorter (e.g. after delete).
  useEffect(() => {
    if (notes && currentIndex >= notes.length && notes.length > 0) {
      setCurrentIndex(notes.length - 1);
    }
  }, [notes, currentIndex]);

  // Click-outside to close.
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      const target = e.target as Node;
      if (popoverRef.current?.contains(target)) return;
      if (anchorRef.current?.contains(target)) return;
      onClose();
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open, onClose, anchorRef]);

  // Position the portal-rendered popover relative to the viewport, anchored
  // to the button. `align='right'` mirrors the FileListActions dropdown (right
  // edges aligned, extends left - used in list view rows). `align='left'` is
  // used for card-view overlays anchored at the thumbnail's bottom-left so
  // the popover extends into the card grid's whitespace on the right.
  // Flips above when there isn't enough room below; clamps to viewport.
  useEffect(() => {
    if (!open) return;
    const updatePosition = () => {
      const anchor = anchorRef.current;
      if (!anchor) return;
      const rect = anchor.getBoundingClientRect();
      const viewportW = window.innerWidth;
      const viewportH = window.innerHeight;
      const margin = 8;

      let top = rect.bottom + POPOVER_GAP;

      // Flip above when not enough room below.
      const estimatedPopoverHeight = 260;
      if (top + estimatedPopoverHeight > viewportH - margin && rect.top > estimatedPopoverHeight) {
        top = rect.top - estimatedPopoverHeight - POPOVER_GAP;
      }

      if (align === 'left') {
        // Align popover's left edge with the trigger's left edge.
        let left = rect.left;
        if (left + POPOVER_WIDTH > viewportW - margin) {
          left = Math.max(margin, viewportW - POPOVER_WIDTH - margin);
        }
        if (left < margin) left = margin;
        setCoords({ top, left });
      } else {
        // Align popover's right edge with the trigger's right edge.
        let right = Math.max(margin, viewportW - rect.right);
        if (viewportW - right - POPOVER_WIDTH < margin) {
          right = viewportW - POPOVER_WIDTH - margin;
        }
        setCoords({ top, right });
      }
    };

    updatePosition();
    window.addEventListener('resize', updatePosition);
    window.addEventListener('scroll', updatePosition, true); // capture: true catches scrolls in nested containers
    return () => {
      window.removeEventListener('resize', updatePosition);
      window.removeEventListener('scroll', updatePosition, true);
    };
  }, [open, anchorRef, align]);

  const invalidateLibraryLists = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['library-files'] });
  }, [queryClient]);

  const createMutation = useMutation({
    mutationFn: (body: string) => api.createLibraryFileNote(fileId, body),
    onSuccess: (note) => {
      queryClient.setQueryData<LibraryFileNote[] | undefined>(queryKey, (prev) => {
        const next = prev ? [note, ...prev] : [note];
        onCountChange?.(next.length);
        return next;
      });
      invalidateLibraryLists();
      setDraft('');
      setMode('view');
      setCurrentIndex(0);
      showToast(t('libraryNotes.created'), 'success');
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const updateMutation = useMutation({
    mutationFn: ({ noteId, body }: { noteId: number; body: string }) =>
      api.updateLibraryFileNote(noteId, body),
    onSuccess: (updated) => {
      queryClient.setQueryData<LibraryFileNote[] | undefined>(queryKey, (prev) =>
        prev?.map((n) => (n.id === updated.id ? updated : n))
      );
      setDraft('');
      setMode('view');
      showToast(t('libraryNotes.updated'), 'success');
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const deleteMutation = useMutation({
    mutationFn: (noteId: number) => api.deleteLibraryFileNote(noteId),
    onSuccess: (_data, noteId) => {
      queryClient.setQueryData<LibraryFileNote[] | undefined>(queryKey, (prev) => {
        const next = prev?.filter((n) => n.id !== noteId) ?? [];
        onCountChange?.(next.length);
        return next;
      });
      invalidateLibraryLists();
      setConfirmDeleteId(null);
      showToast(t('libraryNotes.deleted'), 'success');
      // If we just removed the last note, drop into create mode for next add.
      const remaining = (notes?.length ?? 1) - 1;
      if (remaining === 0) {
        setMode('create');
        setDraft('');
      }
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  if (!open) return null;

  const currentNote = notes && notes.length > 0 ? notes[Math.min(currentIndex, notes.length - 1)] : null;
  const charsLeft = LIBRARY_FILE_NOTE_MAX_LENGTH - draft.length;
  const draftValid = draft.trim().length > 0 && draft.length <= LIBRARY_FILE_NOTE_MAX_LENGTH;

  const handleSave = () => {
    if (!draftValid) return;
    if (mode === 'create') {
      createMutation.mutate(draft);
    } else if (mode === 'edit' && currentNote) {
      updateMutation.mutate({ noteId: currentNote.id, body: draft });
    }
  };

  const handleStartEdit = () => {
    if (!currentNote) return;
    setDraft(currentNote.body);
    setMode('edit');
  };

  const handleStartCreate = () => {
    setDraft('');
    setMode('create');
  };

  const handleCancelEdit = () => {
    setDraft('');
    setMode('view');
  };

  const isSaving = createMutation.isPending || updateMutation.isPending;
  const formatMeta = (note: LibraryFileNote) => {
    const author = note.user_username ?? (authEnabled ? t('libraryNotes.unknownAuthor') : t('libraryNotes.anonymous'));
    const ts = new Date(note.updated_at);
    const datePart = ts.toLocaleString();
    return `${author} · ${datePart}`;
  };

  const popoverNode = (
    <div
      ref={popoverRef}
      role="dialog"
      aria-label={t('libraryNotes.title')}
      style={{
        position: 'fixed',
        top: coords?.top ?? 0,
        left: coords?.left,   // undefined unless align='left'
        right: coords?.right, // undefined unless align='right'
        width: POPOVER_WIDTH,
        visibility: coords ? 'visible' : 'hidden',
      }}
      className="z-[60] bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl"
    >
        {/* Header: pagination + add + close */}
        <div className="flex items-center justify-between p-2 border-b border-bambu-dark-tertiary">
          <div className="flex items-center gap-1 text-sm text-bambu-gray">
            {notes && notes.length > 0 && mode === 'view' && (
              <>
                <button
                  type="button"
                  aria-label={t('libraryNotes.previous')}
                  onClick={() => setCurrentIndex((i) => Math.max(0, i - 1))}
                  disabled={currentIndex === 0}
                  className="p-1 rounded hover:bg-bambu-dark-tertiary disabled:opacity-30"
                >
                  <ChevronLeft className="w-4 h-4" />
                </button>
                <span className="px-1 tabular-nums">
                  {currentIndex + 1} / {notes.length}
                </span>
                <button
                  type="button"
                  aria-label={t('libraryNotes.next')}
                  onClick={() => setCurrentIndex((i) => Math.min((notes?.length ?? 1) - 1, i + 1))}
                  disabled={currentIndex >= notes.length - 1}
                  className="p-1 rounded hover:bg-bambu-dark-tertiary disabled:opacity-30"
                >
                  <ChevronRight className="w-4 h-4" />
                </button>
              </>
            )}
          </div>
          <div className="flex items-center gap-1">
            {mode === 'view' && (
              <button
                type="button"
                aria-label={t('libraryNotes.add')}
                title={t('libraryNotes.add')}
                onClick={handleStartCreate}
                className="p-1.5 rounded text-bambu-green hover:bg-bambu-green/20"
              >
                <MessageSquarePlus className="w-4 h-4" />
              </button>
            )}
            <button
              type="button"
              aria-label={t('common.close')}
              onClick={onClose}
              className="p-1.5 rounded text-bambu-gray hover:bg-bambu-dark-tertiary hover:text-white"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* Body */}
        <div className="p-3 space-y-2">
          {isLoading ? (
            <div className="flex justify-center py-4">
              <Loader2 className="w-5 h-5 animate-spin text-bambu-gray" />
            </div>
          ) : mode === 'create' || mode === 'edit' ? (
            <>
              <textarea
                autoFocus
                value={draft}
                maxLength={LIBRARY_FILE_NOTE_MAX_LENGTH}
                onChange={(e) => setDraft(e.target.value)}
                placeholder={t('libraryNotes.placeholder')}
                rows={5}
                className="w-full px-2 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded text-sm text-white placeholder:text-bambu-gray/50 focus:outline-none focus:border-bambu-green resize-none"
              />
              <div className="flex items-center justify-between text-xs">
                <span
                  className={charsLeft <= 50 ? 'text-yellow-400' : 'text-bambu-gray'}
                  data-testid="notes-char-counter"
                >
                  {t('libraryNotes.charsLeft', { count: charsLeft })}
                </span>
                <div className="flex items-center gap-1">
                  {mode === 'edit' && (
                    <button
                      type="button"
                      aria-label={t('common.cancel')}
                      onClick={handleCancelEdit}
                      className="p-1.5 rounded text-bambu-gray hover:bg-bambu-dark-tertiary hover:text-white"
                    >
                      <X className="w-4 h-4" />
                    </button>
                  )}
                  <button
                    type="button"
                    aria-label={t('common.save')}
                    onClick={handleSave}
                    disabled={!draftValid || isSaving}
                    className="p-1.5 rounded text-bambu-green hover:bg-bambu-green/20 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {isSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
                  </button>
                </div>
              </div>
            </>
          ) : currentNote ? (
            <>
              <p className="text-sm text-white whitespace-pre-wrap break-words">{currentNote.body}</p>
              <div className="flex items-center justify-between text-xs text-bambu-gray pt-1 border-t border-bambu-dark-tertiary">
                <span className="truncate" title={formatMeta(currentNote)}>{formatMeta(currentNote)}</span>
                <div className="flex items-center gap-1 shrink-0">
                  {currentNote.can_edit && (
                    <>
                      <button
                        type="button"
                        aria-label={t('libraryNotes.edit')}
                        title={t('libraryNotes.edit')}
                        onClick={handleStartEdit}
                        className="p-1 rounded hover:bg-bambu-dark-tertiary hover:text-white"
                      >
                        <Pencil className="w-3.5 h-3.5" />
                      </button>
                      <button
                        type="button"
                        aria-label={t('libraryNotes.delete')}
                        title={t('libraryNotes.delete')}
                        onClick={() => setConfirmDeleteId(currentNote.id)}
                        className="p-1 rounded hover:bg-red-500/20 hover:text-red-400"
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </>
                  )}
                </div>
              </div>
            </>
          ) : null}
        </div>
    </div>
  );

  return (
    <>
      {createPortal(popoverNode, document.body)}
      {confirmDeleteId !== null && (
        <ConfirmModal
          title={t('libraryNotes.deleteConfirmTitle')}
          message={t('libraryNotes.deleteConfirmMessage')}
          confirmText={t('common.delete')}
          variant="danger"
          onConfirm={() => deleteMutation.mutate(confirmDeleteId)}
          onCancel={() => setConfirmDeleteId(null)}
        />
      )}
    </>
  );
}
