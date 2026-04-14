import { useState, useEffect, useRef, useCallback } from 'react';
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
}

export function LibraryFileNotesPopover({ fileId, open, anchorRef, onClose, onCountChange }: Props) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const { authEnabled } = useAuth();
  const queryClient = useQueryClient();
  const popoverRef = useRef<HTMLDivElement | null>(null);

  const [mode, setMode] = useState<Mode>('view');
  const [currentIndex, setCurrentIndex] = useState(0);
  const [draft, setDraft] = useState('');
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null);

  const queryKey = ['library-file-notes', fileId];

  const { data: notes, isLoading } = useQuery<LibraryFileNote[]>({
    queryKey,
    queryFn: () => api.getLibraryFileNotes(fileId),
    enabled: open,
  });

  // Switch into create mode automatically when opening with no notes.
  useEffect(() => {
    if (open && !isLoading && (notes?.length ?? 0) === 0) {
      setMode('create');
      setDraft('');
    } else if (open && (notes?.length ?? 0) > 0 && mode === 'create') {
      // Notes appeared (e.g. after a new one was just saved) — go to view.
      setMode('view');
      setCurrentIndex(0);
    }
  }, [open, isLoading, notes, mode]);

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

  return (
    <>
      <div
        ref={popoverRef}
        role="dialog"
        aria-label={t('libraryNotes.title')}
        className="absolute z-50 mt-2 right-0 w-80 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl"
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
