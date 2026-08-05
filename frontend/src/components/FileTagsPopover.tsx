import { useState, useMemo, useEffect } from 'react';
import { createPortal } from 'react-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Loader2, Plus, Search } from 'lucide-react';

import { api, type LibraryFileListItem } from '../api/client';
import { useToast } from '../contexts/ToastContext';
import { libraryTagsQueryKey } from '../utils/libraryTagsQuery';

export interface TagsPopoverAnchor {
  /** The card's or row's box on screen, at the moment the menu entry was hit. */
  rect: { top: number; right: number; bottom: number; height: number };
  placement: 'card' | 'row';
}

interface FileTagsPopoverProps {
  file: LibraryFileListItem;
  anchor: TagsPopoverAnchor;
  onClose: () => void;
}

const PANEL_WIDTH = 240;

/**
 * Anchored to the FILE, not to the pointer.
 *
 * Clicking the ⋮ entry used to open the panel at the cursor, which in list view
 * put it in the middle of nowhere — the menu itself is portal-rendered well
 * away from the row it belongs to.
 *
 * Both placements pin the RIGHT edge to the file's right edge, so the panel
 * always grows leftwards into the page rather than off it. They differ
 * vertically:
 *
 * - **card** — bottom edge on the card's bottom edge, so the panel's
 *   bottom-right corner sits in the card's bottom-right corner and it opens
 *   upwards over the card it belongs to.
 * - **row** — top edge at the row's lower quarter, so it opens downwards from
 *   the row without covering the name being tagged.
 *
 * Expressed with `bottom`/`top` rather than a computed offset because the panel
 * has no fixed height: pinning the edge lets the browser do the arithmetic that
 * would otherwise need the height measured after a first paint.
 */

/**
 * Tags for ONE file — a checkbox list, not the bulk modal.
 *
 * ``BulkTagsPickerModal`` carries an add/remove mode switch, which is the right
 * shape when many files disagree about a tag and pure noise for one, where the
 * honest answer is a checkbox that shows what is already set. There is no OK
 * button either: the tick IS the action.
 *
 * System tags are absent. They are derived from the file, and the backend drops
 * their ids from add/remove silently — a checkbox for one would report success
 * and change nothing.
 */
export function FileTagsPopover({ file, anchor, onClose }: FileTagsPopoverProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const [search, setSearch] = useState('');
  const [newName, setNewName] = useState('');

  const { data: catalog = [], isLoading } = useQuery({
    queryKey: libraryTagsQueryKey,
    queryFn: api.getLibraryTags,
  });

  const tags = useMemo(() => {
    const q = search.trim().toLowerCase();
    return catalog.filter((tag) => !tag.is_system && (!q || tag.name.toLowerCase().includes(q)));
  }, [catalog, search]);

  const current = useMemo(() => new Set((file.tags ?? []).map((tag) => tag.id)), [file.tags]);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['library-files'] });
    void queryClient.invalidateQueries({ queryKey: libraryTagsQueryKey });
  };

  const toggleMutation = useMutation({
    mutationFn: ({ tagId, checked }: { tagId: number; checked: boolean }) =>
      api.bulkAssignLibraryTags([file.id], [tagId], checked ? 'add' : 'remove'),
    onSuccess: invalidate,
    onError: (err: Error) => showToast(err.message || t('fileManager.tags.applyFailed'), 'error'),
  });

  const createMutation = useMutation({
    mutationFn: async (name: string) => {
      const tag = await api.createLibraryTag(name.trim());
      await api.bulkAssignLibraryTags([file.id], [tag.id], 'add');
      return tag;
    },
    onSuccess: () => {
      setNewName('');
      invalidate();
    },
    onError: (err: Error) => showToast(err.message || t('fileManager.tags.saveFailed'), 'error'),
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  return createPortal(
    <>
      <div className="fixed inset-0 z-[65]" onClick={onClose} />
      <div
        role="dialog"
        aria-label={t('fileManager.tags.title')}
        style={{
          position: 'fixed',
          width: PANEL_WIDTH,
          // Never past the left edge on a narrow viewport, where a card can be
          // narrower than the panel itself.
          right: Math.max(8, window.innerWidth - anchor.rect.right),
          ...(anchor.placement === 'card'
            ? { bottom: Math.max(8, window.innerHeight - anchor.rect.bottom) }
            : { top: anchor.rect.top + anchor.rect.height * 0.75 }),
        }}
        className="z-[70] bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl p-2"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="relative mb-2">
          <Search className="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-bambu-gray/50" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t('fileManager.tags.searchPlaceholder')}
            className="w-full h-8 pl-7 pr-2 bg-bambu-dark border border-bambu-dark-tertiary rounded text-xs text-white placeholder:text-bambu-gray/50 focus:outline-none focus:border-bambu-green"
          />
        </div>

        <div className="max-h-48 overflow-y-auto">
          {isLoading ? (
            <div className="flex items-center justify-center py-4 text-bambu-gray">
              <Loader2 className="w-4 h-4 animate-spin" />
            </div>
          ) : tags.length === 0 ? (
            <p className="py-3 text-center text-xs text-bambu-gray">
              {search.trim() ? t('fileManager.tags.noMatches') : t('fileManager.tags.empty')}
            </p>
          ) : (
            tags.map((tag) => (
              <label
                key={tag.id}
                className="flex items-center gap-2 px-1 py-1 rounded hover:bg-bambu-dark cursor-pointer"
              >
                <input
                  type="checkbox"
                  aria-label={tag.name}
                  checked={current.has(tag.id)}
                  disabled={toggleMutation.isPending}
                  onChange={(e) => toggleMutation.mutate({ tagId: tag.id, checked: e.target.checked })}
                  className="w-3.5 h-3.5 accent-bambu-green"
                />
                <span className="text-xs text-white truncate">{tag.name}</span>
              </label>
            ))
          )}
        </div>

        <div className="border-t border-bambu-dark-tertiary mt-2 pt-2 flex items-center gap-1">
          <Plus className="w-3.5 h-3.5 text-bambu-gray" />
          <input
            type="text"
            value={newName}
            maxLength={64}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && newName.trim()) {
                e.preventDefault();
                createMutation.mutate(newName);
              }
            }}
            placeholder={t('fileManager.tags.newTagPlaceholder')}
            className="flex-1 h-7 px-1 bg-transparent text-xs text-white placeholder:text-bambu-gray/50 focus:outline-none"
          />
        </div>
      </div>
    </>,
    document.body,
  );
}
