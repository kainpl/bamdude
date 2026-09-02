import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Check, FileBox, Layers, Loader2, Search, X } from 'lucide-react';

import { api } from '../api/client';
import type { LibraryFileListItem } from '../api/client';
import { Button } from './Button';
import { FolderTreePicker } from './FolderTreePicker';
import type { SequencedFile } from './QueueSequencer';
import { offerableFiles } from '../lib/offerableFiles';
import { formatDuration, parseUTCDate } from '../utils/date';
import { flattenFolderTree } from '../utils/folderTree';
import { formatFileSize } from '../utils/file';
import { mapModelCode } from '../utils/printer';

/** Newest first, on the field the file list everywhere else sorts by. */
function newestFirst(a: LibraryFileListItem, b: LibraryFileListItem): number {
  const at = parseUTCDate(a.fs_modified_at ?? a.created_at)?.getTime() ?? 0;
  const bt = parseUTCDate(b.fs_modified_at ?? b.created_at)?.getTime() ?? 0;
  return bt - at;
}

/** The row the folder tree has no entry for: everything, everywhere. */
const ALL_FOLDERS = 'all';
/** A folder id no row can carry, so the tree highlights nothing while "all" is on. */
const NOTHING_HIGHLIGHTED = -1;

interface LibraryPickerModalProps {
  /** Only files sliced for this model are offered. Omit for the auto-queue,
   *  which has no one machine to match against. */
  printerModel?: string | null;
  /** Where the selection is headed — the printer's name, or the auto-queue. */
  targetName: string;
  onCancel: () => void;
  /** Never called with an empty selection. */
  onConfirm: (files: SequencedFile[]) => void;
}

/**
 * Pick a batch of sliced files out of the library and hand it to the queue.
 *
 * ⚠️ **It picks files and asks nothing else.** Plates, AMS mapping, quantity,
 * schedule and print options all belong to `PrintModal`, which
 * `QueueSequencer` opens once per GROUP — the files whose answers coincide.
 * That division is the whole reason this dialog is allowed to exist: a bulk
 * *scheduling* dialog was written and rejected in August 2026, and the half of
 * that reason still standing after grouping is that `PrintModal` must remain
 * the only code that builds a queue payload. Carrying one answer onto the next
 * file is no longer the objection — that IS a group, and the answer is carried
 * in full: printer, dispatch mode and auto target, schedule, copies, print
 * options, swap macros, macro selection. What stays per file is what means
 * something different about a different file — filament mapping (a global tray
 * id names another spool on another machine) and anything keyed by plate index
 * (plate 3 of one file need not exist in the next); both are recomputed by the
 * same code the visible dialog uses. A bulk *selection* dialog builds no
 * payload at all, so it cannot make that mistake.
 *
 * Selection is a Map held here, so it survives changing folder and searching —
 * which is the point of picking from a browser rather than from one folder.
 */
export function LibraryPickerModal({
  printerModel,
  targetName,
  onCancel,
  onConfirm,
}: LibraryPickerModalProps) {
  const { t } = useTranslation();
  const [folder, setFolder] = useState<number | null | typeof ALL_FOLDERS>(ALL_FOLDERS);
  const [search, setSearch] = useState('');
  const [picked, setPicked] = useState<Map<number, SequencedFile>>(new Map());

  const { data: folders } = useQuery({
    queryKey: ['library-folders'],
    queryFn: api.getLibraryFolders,
  });
  // The whole library in one fetch, filtered here. Folder browsing, searching
  // and a selection spanning both are three views of one set; paging by folder
  // would mean the search could only ever see the folder already open.
  // Prefix-shares its key with every `['library-files']` invalidation, so an
  // upload made elsewhere refreshes this list too.
  // Legacy flat call on purpose (task 2, 2026-08-29 server-driven-lists) —
  // this is a small, scoped picker query, not the library-wide, paginated
  // view; the paged surface (`getLibraryFilesPaged`) is FileManagerPage's.
  const { data: files, isLoading } = useQuery({
    queryKey: ['library-files', 'picker'],
    queryFn: () => api.getLibraryFiles(null, false),
  });

  const folderNames = useMemo(() => {
    const names = new Map<number, string>();
    for (const root of folders ?? []) {
      for (const { folder: node } of flattenFolderTree(root)) names.set(node.id, node.name);
    }
    return names;
  }, [folders]);

  const offerable = useMemo(() => offerableFiles(files, printerModel), [files, printerModel]);

  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase();
    // ⚠️ A search spans the library, not the open folder. Searching inside one
    // folder would not answer the question the search box is there for.
    const scoped = needle
      ? offerable.filter(
          (file) =>
            file.filename.toLowerCase().includes(needle) ||
            (file.print_name ?? '').toLowerCase().includes(needle),
        )
      : folder === ALL_FOLDERS
        ? offerable
        : offerable.filter((file) => file.folder_id === folder);
    return [...scoped].sort(newestFirst);
  }, [offerable, search, folder]);

  const toggle = (file: LibraryFileListItem) => {
    setPicked((prev) => {
      const next = new Map(prev);
      if (next.has(file.id)) next.delete(file.id);
      else next.set(file.id, { id: file.id, name: file.print_name || file.filename });
      return next;
    });
  };

  const details = (file: LibraryFileListItem): string => {
    const parts: string[] = [];
    // The model earns its place only where it is not already the answer for
    // every row — on the auto-queue, where it is what the item will target.
    if (!printerModel && file.sliced_for_model) parts.push(file.sliced_for_model);
    if (file.print_time_seconds) parts.push(formatDuration(file.print_time_seconds));
    if (file.filament_used_grams) parts.push(`${Math.round(file.filament_used_grams)} g`);
    parts.push(formatFileSize(file.file_size));
    const where = file.folder_id === null ? null : folderNames.get(file.folder_id);
    if (where && (folder === ALL_FOLDERS || search.trim())) parts.push(where);
    return parts.join(' · ');
  };

  const count = picked.size;

  return (
    <div className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-50 p-4">
      <div className="bg-bambu-dark-secondary rounded-lg w-full max-w-5xl h-[80vh] flex flex-col">
        <div className="flex items-center justify-between gap-3 p-4 border-b border-bambu-dark shrink-0">
          <div className="min-w-0">
            <h2 className="text-lg font-semibold text-white truncate">{t('libraryPicker.title')}</h2>
            <p className="text-xs text-bambu-gray truncate">
              {t('libraryPicker.subtitle', { target: targetName })}
            </p>
          </div>
          <button onClick={onCancel} className="p-1 hover:bg-bambu-dark rounded" aria-label={t('common.close')}>
            <X className="w-5 h-5 text-bambu-gray" />
          </button>
        </div>

        <div className="flex-1 min-h-0 flex">
          <div className="w-56 shrink-0 border-r border-bambu-dark p-2 overflow-y-auto">
            <button
              type="button"
              onClick={() => setFolder(ALL_FOLDERS)}
              className={`w-full text-left px-3 py-2 rounded transition-colors flex items-center gap-2 mb-1 ${
                folder === ALL_FOLDERS
                  ? 'bg-bambu-green/20 text-bambu-green'
                  : 'hover:bg-bambu-dark text-white'
              }`}
            >
              <Layers className="w-4 h-4 shrink-0" />
              <span className="truncate">{t('libraryPicker.allFiles')}</span>
            </button>
            {/* Read-only external mounts are offered here: this picker only
                READS folders, so the write-permission filter does not apply. */}
            <FolderTreePicker
              folders={folders}
              value={folder === ALL_FOLDERS ? NOTHING_HIGHLIGHTED : folder}
              onChange={setFolder}
              rootLabel={t('libraryPicker.libraryRoot')}
              includeReadOnly
              className="max-h-none"
            />
          </div>

          <div className="flex-1 min-w-0 flex flex-col">
            <div className="p-3 border-b border-bambu-dark shrink-0">
              <div className="relative">
                <Search className="w-4 h-4 text-bambu-gray absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" />
                <input
                  type="text"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder={t('libraryPicker.searchPlaceholder')}
                  className="w-full pl-9 pr-3 py-2 rounded bg-bambu-dark text-sm text-white placeholder:text-bambu-gray focus:outline-none focus:ring-1 focus:ring-bambu-green"
                />
              </div>
            </div>

            <div className="flex-1 overflow-y-auto p-3">
              {isLoading ? (
                <div className="h-full flex items-center justify-center">
                  <Loader2 className="w-6 h-6 text-bambu-green animate-spin" />
                </div>
              ) : visible.length === 0 ? (
                <p className="text-sm text-bambu-gray italic p-4 text-center">
                  {offerable.length === 0
                    ? printerModel
                      ? t('libraryPicker.noneForModel', { model: mapModelCode(printerModel) })
                      : t('libraryPicker.noneSliced')
                    : t('libraryPicker.noMatch')}
                </p>
              ) : (
                <div className="grid grid-cols-1 xl:grid-cols-2 gap-2">
                  {visible.map((file) => {
                    const checked = picked.has(file.id);
                    return (
                      <button
                        key={file.id}
                        type="button"
                        aria-pressed={checked}
                        onClick={() => toggle(file)}
                        className={`flex items-center gap-3 p-2 rounded border text-left transition-colors ${
                          checked
                            ? 'border-bambu-green bg-bambu-green/10'
                            : 'border-bambu-dark-tertiary bg-bambu-dark hover:border-bambu-green/50'
                        }`}
                      >
                        <span
                          className={`w-4 h-4 shrink-0 rounded border flex items-center justify-center ${
                            checked ? 'bg-bambu-green border-bambu-green' : 'border-bambu-gray/50'
                          }`}
                        >
                          {checked && <Check className="w-3 h-3 text-black" strokeWidth={3} />}
                        </span>
                        <span className="w-12 h-12 shrink-0 rounded bg-bambu-dark-tertiary overflow-hidden flex items-center justify-center">
                          {file.thumbnail_path ? (
                            <img
                              src={api.getLibraryFileThumbnailUrl(file.id)}
                              alt=""
                              className="w-full h-full object-contain"
                            />
                          ) : (
                            <FileBox className="w-5 h-5 text-bambu-gray/50" />
                          )}
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="block text-sm text-white truncate">
                            {file.print_name || file.filename}
                          </span>
                          <span className="block text-xs text-bambu-gray truncate">{details(file)}</span>
                        </span>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        </div>

        <div className="flex items-center justify-between gap-2 p-4 border-t border-bambu-dark shrink-0">
          <span className="text-xs text-bambu-gray">
            {/* Counted across every folder, which is exactly why it is worth saying. */}
            {t('libraryPicker.selectedCount', { count })}
          </span>
          <div className="flex items-center gap-2">
            <Button variant="ghost" onClick={onCancel}>
              {t('common.cancel')}
            </Button>
            <Button disabled={count === 0} onClick={() => onConfirm([...picked.values()])}>
              {t('libraryPicker.confirm')}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
