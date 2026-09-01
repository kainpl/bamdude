import { useState, useEffect, useMemo, useRef } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Tag, Plus, Loader2, Pencil, Trash2, X, Search } from 'lucide-react';

import { api, type LibraryTag } from '../api/client';
import { Button } from './Button';
import { useToast } from '../contexts/ToastContext';
import { libraryTagsQueryKey } from '../utils/libraryTagsQuery';
import { getTagStyle } from '../lib/fileTags';

interface LibraryTagsModalProps {
  open: boolean;
  onClose: () => void;
}

/**
 * Tag catalog: both kinds, one list, no nested dialogs.
 *
 * The dialog this replaces had one fault that dwarfed the others — the whole
 * ROW was a click target that applied a filter and closed the window. Missing
 * the 16-pixel trash icon therefore punished the cheapest possible mistake with
 * the most expensive outcome: the library silently narrowed and the window you
 * were working in disappeared. Nothing here reacts to a row click.
 *
 * The rest follows from refusing to stack surfaces. Renaming happens in the row
 * rather than in a modal on a modal; deleting turns the row into a confirm
 * strip rather than opening a third layer, and states the file count — which
 * used to be on screen in its own column but not in the sentence you were
 * agreeing to.
 *
 * System tags (m128) are shown and locked: "what exists and how much is on it"
 * is worth answering for them too, but they are derived from the file, so every
 * mutation the backend offers refuses them.
 */
export function LibraryTagsModal({ open, onClose }: LibraryTagsModalProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const [search, setSearch] = useState('');
  const [editingId, setEditingId] = useState<number | null>(null);
  const [draftName, setDraftName] = useState('');
  const [confirmingId, setConfirmingId] = useState<number | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [confirmingBulk, setConfirmingBulk] = useState(false);
  const [creating, setCreating] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const { data: catalog = [], isLoading } = useQuery({
    queryKey: libraryTagsQueryKey,
    queryFn: api.getLibraryTags,
    enabled: open,
  });

  const [systemTags, userTags] = useMemo(() => {
    const q = search.trim().toLowerCase();
    const match = (tag: LibraryTag) => !q || tag.name.toLowerCase().includes(q) || (tag.code ?? '').includes(q);
    return [catalog.filter((tag) => tag.is_system && match(tag)), catalog.filter((tag) => !tag.is_system && match(tag))];
  }, [catalog, search]);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: libraryTagsQueryKey });
    // Listings carry the tags array — bump those so chips refresh immediately.
    void queryClient.invalidateQueries({ queryKey: ['library-files'] });
  };

  const resetRowState = () => {
    setEditingId(null);
    setDraftName('');
    setConfirmingId(null);
    setCreating(false);
  };

  const saveMutation = useMutation({
    mutationFn: async ({ id, name }: { id: number | null; name: string }) => {
      const trimmed = name.trim();
      if (!trimmed) throw new Error(t('fileManager.tags.nameRequired'));
      return id === null ? api.createLibraryTag(trimmed) : api.updateLibraryTag(id, trimmed);
    },
    onSuccess: (_data, variables) => {
      showToast(t(variables.id === null ? 'fileManager.tags.created' : 'fileManager.tags.updated'), 'success');
      resetRowState();
      invalidate();
    },
    onError: (err: Error) => {
      // Deliberately does NOT close the editor: the name the user typed is the
      // only copy of it, and a duplicate is the most likely failure here.
      showToast(err.message || t('fileManager.tags.saveFailed'), 'error');
    },
  });

  const deleteMutation = useMutation({
    mutationFn: async (ids: number[]) => {
      for (const id of ids) await api.deleteLibraryTag(id);
    },
    onSuccess: (_data, ids) => {
      showToast(t('fileManager.tags.deleted'), 'success');
      setConfirmingId(null);
      setConfirmingBulk(false);
      setSelected((prev) => {
        const next = new Set(prev);
        for (const id of ids) next.delete(id);
        return next;
      });
      invalidate();
    },
    onError: (err: Error) => {
      showToast(err.message || t('fileManager.tags.deleteFailed'), 'error');
    },
  });

  useEffect(() => {
    if (editingId !== null || creating) inputRef.current?.select();
  }, [editingId, creating]);

  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      if (saveMutation.isPending || deleteMutation.isPending) return;
      // Escape unwinds one layer at a time — an in-row editor or confirm strip
      // first, the dialog only when nothing is open inside it.
      if (editingId !== null || creating || confirmingId !== null || confirmingBulk) {
        resetRowState();
        setConfirmingBulk(false);
      } else {
        onClose();
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [open, editingId, creating, confirmingId, confirmingBulk, saveMutation.isPending, deleteMutation.isPending, onClose]);

  if (!open) return null;

  const beginEdit = (tag: LibraryTag) => {
    setConfirmingId(null);
    setCreating(false);
    setEditingId(tag.id);
    setDraftName(tag.name);
  };

  const commitEdit = (id: number | null) => {
    const trimmed = draftName.trim();
    const original = id === null ? '' : (catalog.find((tag) => tag.id === id)?.name ?? '');
    if (!trimmed || trimmed === original) {
      resetRowState();
      return;
    }
    saveMutation.mutate({ id, name: trimmed });
  };

  const toggleSelected = (id: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const nameEditor = (id: number | null) => (
    <input
      ref={inputRef}
      type="text"
      maxLength={64}
      value={draftName}
      autoFocus
      onChange={(e) => setDraftName(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          commitEdit(id);
        }
      }}
      // Blur saves when the name actually changed. Escape is handled by the
      // document listener above and resets the draft first, so leaving via
      // Escape cannot save on the way out.
      onBlur={() => commitEdit(id)}
      placeholder={id === null ? t('fileManager.tags.newTagPlaceholder') : undefined}
      className="w-full px-2 py-0.5 bg-bambu-dark border border-bambu-green rounded text-white text-sm focus:outline-none"
    />
  );

  const modalTitleId = 'library-tags-modal-title';

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div
        className="absolute inset-0 bg-black/60"
        onClick={() => {
          if (saveMutation.isPending || deleteMutation.isPending) return;
          onClose();
        }}
      />
      <div
        className="relative w-full max-w-3xl mx-4 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-2xl max-h-[90vh] flex flex-col"
        role="dialog"
        aria-modal="true"
        aria-labelledby={modalTitleId}
      >
        <div className="flex items-center justify-between gap-4 px-4 py-4 border-b border-bambu-dark-tertiary">
          <div className="min-w-0 flex-1">
            <h2 id={modalTitleId} className="text-lg font-semibold text-white flex items-center gap-2">
              <Tag className="w-5 h-5 text-bambu-green" />
              {t('fileManager.tags.title')}
            </h2>
            <p className="text-bambu-gray text-sm mt-0.5">{t('fileManager.tags.subtitle')}</p>
          </div>
          <div className="flex items-center gap-2">
            <div className="relative">
              <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-bambu-gray/50" />
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder={t('fileManager.tags.searchPlaceholder')}
                className="h-9 w-48 pl-8 pr-3 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-sm text-white placeholder:text-bambu-gray/50 focus:outline-none focus:border-bambu-green"
              />
            </div>
            <Button
              onClick={() => {
                resetRowState();
                setCreating(true);
                setDraftName('');
              }}
            >
              <Plus className="w-4 h-4" />
              {t('fileManager.tags.add')}
            </Button>
            <button
              type="button"
              className="p-1.5 text-bambu-gray hover:text-white rounded"
              onClick={onClose}
              aria-label={t('common.close')}
            >
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>

        <div className="overflow-y-auto px-4 py-4">
          {isLoading ? (
            <div className="flex items-center justify-center py-16 text-bambu-gray">
              <Loader2 className="w-6 h-6 animate-spin mr-2" />
              {t('common.loading')}
            </div>
          ) : (
            <>
              {systemTags.length > 0 && (
                <section className="mb-4">
                  <h3 className="text-xs font-medium uppercase tracking-wide text-bambu-gray">
                    {t('fileManager.tags.systemSection')}
                  </h3>
                  <p className="text-xs text-bambu-gray/70 mt-0.5 mb-2">{t('fileManager.tags.systemHint')}</p>
                  {systemTags.map((tag) => {
                    const style = tag.code ? getTagStyle(tag.code) : null;
                    return (
                      <div
                        key={tag.id}
                        data-tag-row
                        className="flex items-center gap-3 py-1.5 border-b border-bambu-dark-tertiary/40 last:border-0"
                      >
                        <span
                          className={`text-xs px-2 py-0.5 rounded font-medium ${style?.bg ?? 'bg-bambu-gray/70'} ${style?.text ?? 'text-white'}`}
                        >
                          {tag.code ? t(`library.tags.${tag.code}`, tag.name) : tag.name}
                        </span>
                        <span className="text-sm text-bambu-gray ml-auto">{tag.file_count}</span>
                      </div>
                    );
                  })}
                </section>
              )}

              <section>
                <h3 className="text-xs font-medium uppercase tracking-wide text-bambu-gray mb-2">
                  {t('fileManager.tags.mineSection')}
                </h3>
                {creating && (
                  <div data-tag-row className="flex items-center gap-3 py-1.5">
                    <span className="w-4" />
                    <div className="flex-1">{nameEditor(null)}</div>
                  </div>
                )}
                {userTags.length === 0 && !creating ? (
                  <p className="py-8 text-center text-bambu-gray">
                    {search.trim() ? t('fileManager.tags.noMatches') : t('fileManager.tags.empty')}
                  </p>
                ) : (
                  userTags.map((tag) => (
                    <div
                      key={tag.id}
                      data-tag-row
                      className="flex items-center gap-3 py-1.5 border-b border-bambu-dark-tertiary/40 last:border-0"
                    >
                      <input
                        type="checkbox"
                        checked={selected.has(tag.id)}
                        onChange={() => toggleSelected(tag.id)}
                        aria-label={tag.name}
                        className="w-4 h-4 accent-bambu-green"
                      />
                      {confirmingId === tag.id ? (
                        <>
                          <span className="text-sm text-white flex-1">
                            {tag.file_count > 0
                              ? t('fileManager.tags.deletePrompt', { name: tag.name, count: tag.file_count })
                              : t('fileManager.tags.deletePromptUnused', { name: tag.name })}
                          </span>
                          <Button
                            variant="danger"
                            size="sm"
                            onClick={() => deleteMutation.mutate([tag.id])}
                            disabled={deleteMutation.isPending}
                          >
                            {t('common.delete')}
                          </Button>
                          <Button variant="secondary" size="sm" onClick={() => setConfirmingId(null)}>
                            {t('common.cancel')}
                          </Button>
                        </>
                      ) : (
                        <>
                          <div className="flex-1 min-w-0">
                            {editingId === tag.id ? (
                              nameEditor(tag.id)
                            ) : (
                              <button
                                type="button"
                                onClick={() => beginEdit(tag)}
                                className="text-sm text-white text-left hover:text-bambu-green transition-colors"
                              >
                                {tag.name}
                              </button>
                            )}
                          </div>
                          <span className="text-sm text-bambu-gray">{tag.file_count}</span>
                          <button
                            type="button"
                            className="p-1.5 text-bambu-gray hover:text-bambu-green rounded"
                            onClick={() => beginEdit(tag)}
                            aria-label={t('fileManager.tags.editAria', { name: tag.name })}
                          >
                            <Pencil className="w-4 h-4" />
                          </button>
                          <button
                            type="button"
                            className="p-1.5 text-bambu-gray hover:text-red-600 dark:hover:text-red-400 rounded"
                            onClick={() => {
                              resetRowState();
                              setConfirmingId(tag.id);
                            }}
                            aria-label={t('fileManager.tags.deleteAria', { name: tag.name })}
                          >
                            <Trash2 className="w-4 h-4" />
                          </button>
                        </>
                      )}
                    </div>
                  ))
                )}
              </section>
            </>
          )}
        </div>

        {selected.size > 0 && (
          <div className="flex items-center gap-3 px-4 py-3 border-t border-bambu-dark-tertiary">
            {confirmingBulk ? (
              <>
                <span className="text-sm text-white flex-1">
                  {t('fileManager.tags.deleteSelectedPrompt', { count: selected.size })}
                </span>
                <Button
                  variant="danger"
                  size="sm"
                  onClick={() => deleteMutation.mutate([...selected])}
                  disabled={deleteMutation.isPending}
                >
                  {t('common.delete')}
                </Button>
                <Button variant="secondary" size="sm" onClick={() => setConfirmingBulk(false)}>
                  {t('common.cancel')}
                </Button>
              </>
            ) : (
              <>
                <span className="text-sm text-bambu-gray flex-1">
                  {t('fileManager.selected', { count: selected.size })}
                </span>
                <Button variant="danger" size="sm" onClick={() => setConfirmingBulk(true)}>
                  {t('fileManager.tags.deleteSelected', { count: selected.size })}
                </Button>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
