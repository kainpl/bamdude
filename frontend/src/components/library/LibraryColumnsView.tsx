import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from 'react';
import { useInfiniteQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Briefcase, Check, ChevronLeft, ChevronRight, FileBox, FolderOpen, FolderSymlink, Info, Link2, Loader2, MoveRight, Users, X } from 'lucide-react';
import { api } from '../../api/client';
import type { LibraryFileListItem, LibraryFileListParams, LibraryFolderTree } from '../../api/client';
import type { LibraryDragDrop } from '../../hooks/useLibraryDragDrop';
import { isMovableFolder } from '../../utils/folderMove';

/**
 * The File Manager's columns view (Miller columns): the library root, then one
 * column per folder of the path to the open folder. A column lists the folders
 * of its level and then its files.
 *
 * ⚠️ The server lists, sorts, filters and pages — a column never does. Each
 * column asks the paged list endpoint for the DIRECT files of its own folder,
 * with the page's sort and filters and a page number of its own; folders come
 * from the tree the page already holds, with the server's counters. A search or
 * filter therefore narrows every column to its own level's matches, never into
 * subfolders.
 *
 * The path is not kept here: the columns are the ancestor chain of the page's
 * selected folder (which the address names), so a reload, a shared link and the
 * browser's Back all show the same columns.
 *
 * ARIA: every column is a `listbox` of its own, named after its folder, with
 * one tab stop, and it owns OPTIONS ONLY — a listbox may own nothing else. An
 * `option` is `aria-selected` when it is on the open path (a folder) or shown
 * in the preview (a file). The bulk selection is a different thing and is the
 * file option's `aria-checked`: Space on the option ticks it, and the tick box
 * drawn beside it is the mouse's door to the same state — an `aria-hidden`
 * span, never a button inside the listbox.
 *
 * A folder is moved without a mouse two ways, both through the page's Move
 * dialog and both by the rule its menu in the tree follows: `M` on the focused
 * folder, and the Move… button in the header of an open folder's column — the
 * header IS that folder, and it sits outside the listbox, one Tab from its rows.
 */

/** Files a column asks for at once; "Show more" asks for the next page. */
export const COLUMN_PAGE_SIZE = 50;

/** The page's sort and filters, as every column sends them. */
export type ColumnFilters = Pick<
  LibraryFileListParams,
  'tag_ids' | 'q' | 'file_type' | 'unprinted_only' | 'username' | 'sort_by'
>;

/** `null` is the library root. */
type FolderKey = number | null;

function pathTo(tree: LibraryFolderTree[], folderId: number): LibraryFolderTree[] | null {
  for (const folder of tree) {
    if (folder.id === folderId) return [folder];
    const below = pathTo(folder.children, folderId);
    if (below) return [folder, ...below];
  }
  return null;
}

const optionClass = (selected: boolean) =>
  `flex-1 min-w-0 flex items-center gap-2 px-2 py-1.5 rounded text-sm text-left cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-bambu-green ${
    selected ? 'bg-bambu-green/20 text-bambu-green' : 'text-white hover:bg-bambu-dark'
  }`;

const DROP_CLASS = { legal: 'ring-2 ring-inset ring-bambu-green bg-bambu-green/10', illegal: 'opacity-50 cursor-not-allowed' } as const;

function Column({
  index,
  folderKey,
  name,
  subfolders,
  openChildId,
  previewFileId,
  filters,
  filtersActive,
  wide,
  hidden,
  selectedFiles,
  thumbnailVersions,
  dnd,
  hintId,
  move,
  onOpenFolder,
  onPreviewFile,
  onOpenFile,
  onToggleFile,
  onFiles,
}: {
  index: number;
  folderKey: FolderKey;
  name: string;
  /** The element that says what Space and M do in a column. */
  hintId: string;
  /** Move… for this column's own folder: absent for the root, `why` when it may not be moved. */
  move: { why?: string; open: () => void } | null;
  subfolders: LibraryFolderTree[];
  /** The folder of this column that the next column shows. */
  openChildId: number | null;
  previewFileId: number | null;
  filters: ColumnFilters;
  filtersActive: boolean;
  /** Fill the view (the phone's single column) instead of a fixed width. */
  wide: boolean;
  /** Kept mounted (its files stay loaded and selectable) but not shown: the phone's preview covers it. */
  hidden: boolean;
  selectedFiles: number[];
  thumbnailVersions: Record<number, number>;
  dnd: LibraryDragDrop;
  onOpenFolder: (folderId: number) => void;
  onPreviewFile: (file: LibraryFileListItem) => void;
  onOpenFile: (file: LibraryFileListItem) => void;
  onToggleFile: (fileId: number) => void;
  onFiles: (folderKey: FolderKey, files: LibraryFileListItem[] | null) => void;
}) {
  const { t } = useTranslation();
  const query = useInfiniteQuery({
    // Under `library-files`, so everything that refreshes the list — a move, an
    // upload, a library event from the socket — refreshes the columns it touches.
    queryKey: ['library-files', 'column', folderKey, filters],
    queryFn: ({ pageParam }) =>
      api.getLibraryFilesPaged({
        ...filters,
        folder_id: folderKey,
        // The root column is the root's own files; a tag filter narrows the
        // level instead of replacing it.
        include_root: true,
        folder_scope: (filters.tag_ids?.length ?? 0) > 0,
        page: pageParam,
        per_page: COLUMN_PAGE_SIZE,
      }),
    initialPageParam: 1,
    getNextPageParam: (last) => (last.meta.current_page < last.meta.last_page ? last.meta.current_page + 1 : undefined),
  });
  const pages = query.data?.pages;
  const files = useMemo(() => pages?.flatMap((page) => page.items) ?? [], [pages]);
  const total = pages?.[pages.length - 1]?.meta.total;

  useEffect(() => {
    onFiles(folderKey, pages ? files : null);
  }, [onFiles, folderKey, pages, files]);
  useEffect(() => () => onFiles(folderKey, null), [onFiles, folderKey]);

  // One tab stop per column: the row last focused, else the selected one, else the first.
  const [focusedKey, setFocusedKey] = useState<string | null>(null);
  const keys = [...subfolders.map((f) => `d${f.id}`), ...files.map((f) => `f${f.id}`)];
  const selectedKey = openChildId != null ? `d${openChildId}` : previewFileId != null ? `f${previewFileId}` : null;
  const tabStop =
    focusedKey && keys.includes(focusedKey)
      ? focusedKey
      : selectedKey && keys.includes(selectedKey)
        ? selectedKey
        : keys[0];

  const loading = query.isPending;
  // With a filter on, "nothing matches here" is worth saying even beside folders;
  // "empty" is a folder with nothing in it at all.
  const noFiles = !loading && !query.isError && files.length === 0;
  const emptyLine = !noFiles
    ? null
    : filtersActive
      ? t('fileManager.columns.noMatches')
      : subfolders.length === 0
        ? t('fileManager.folderIsEmpty')
        : null;
  const headerDrop = dnd.dropState(folderKey);

  return (
    <div
      data-column={index}
      hidden={hidden}
      className={`${hidden ? 'hidden' : 'flex'} ${wide ? 'w-full' : 'w-64'} shrink-0 min-h-0 flex-col border-r border-bambu-dark-tertiary last:border-r-0`}
    >
      {/* The header is the folder itself: a folder or files dropped on it go into it. */}
      <div
        {...dnd.dropProps(folderKey)}
        className={`px-3 py-2 border-b border-bambu-dark-tertiary flex items-center gap-2 text-xs text-bambu-gray ${headerDrop ? DROP_CLASS[headerDrop] : ''}`}
      >
        <span className="font-medium text-white truncate" title={name}>{name}</span>
        {move && (
          <button
            type="button"
            onClick={move.open}
            disabled={!!move.why}
            aria-label={t('fileManager.moveFolder.title', { name })}
            title={move.why ?? t('fileManager.moveFolder.action')}
            className="shrink-0 p-1 rounded text-bambu-gray hover:text-white hover:bg-bambu-dark disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-transparent disabled:hover:text-bambu-gray focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green"
          >
            <MoveRight className="w-3.5 h-3.5" />
          </button>
        )}
        {total !== undefined && (
          <span className="ml-auto shrink-0 tabular-nums">
            {total} {t('fileManager.fileCount', { count: total })}
          </span>
        )}
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto p-1">
        <div role="listbox" aria-label={name} aria-describedby={hintId} aria-busy={loading || undefined} tabIndex={-1} data-column-list={index} className="focus-visible:outline-none">
          {subfolders.map((folder) => {
            const key = `d${folder.id}`;
            const drop = dnd.dropState(folder.id);
            return (
              <div key={key} role="presentation" className="flex items-center gap-1">
                <div
                  role="option"
                  aria-selected={folder.id === openChildId}
                  tabIndex={tabStop === key ? 0 : -1}
                  data-col={index}
                  data-kind="folder"
                  data-id={folder.id}
                  {...dnd.folderDragProps(folder)}
                  {...dnd.dropProps(folder.id)}
                  onFocus={() => setFocusedKey(key)}
                  onClick={() => onOpenFolder(folder.id)}
                  className={`${optionClass(folder.id === openChildId)} ${drop ? DROP_CLASS[drop] : ''}`}
                >
                  {folder.is_external ? (
                    <FolderSymlink className="w-4 h-4 shrink-0 text-purple-600 dark:text-purple-400" />
                  ) : (
                    <FolderOpen className="w-4 h-4 shrink-0 text-bambu-green" />
                  )}
                  <span className="flex-1 min-w-0 truncate" title={folder.name}>{folder.name}</span>
                  {folder.shared && !folder.is_external && (
                    <span className="shrink-0" title={t('fileManager.sharedFolder')}>
                      <Users className="w-3 h-3 text-bambu-gray" />
                    </span>
                  )}
                  {folder.products.length > 0 && (
                    <span
                      className="shrink-0 flex items-center gap-0.5 text-blue-700 dark:text-blue-400"
                      title={folder.products.map((p) => p.name).join(', ')}
                    >
                      <Link2 className="w-3 h-3" />
                      <Briefcase className="w-3 h-3" />
                    </span>
                  )}
                  {folder.file_count > 0 && <span className="shrink-0 text-xs text-bambu-gray">{folder.file_count}</span>}
                  <ChevronRight className="w-3.5 h-3.5 shrink-0 text-bambu-gray" />
                </div>
              </div>
            );
          })}
          {files.map((file) => {
            const key = `f${file.id}`;
            const ticked = selectedFiles.includes(file.id);
            const title = file.print_name || file.filename;
            return (
              <div key={key} role="presentation" data-file-row className="flex items-center gap-1 pl-1">
                {/* The mouse's door to the option's `aria-checked`; the keyboard's is Space on the option. */}
                <span
                  data-select-file
                  aria-hidden="true"
                  onClick={() => onToggleFile(file.id)}
                  title={t('fileManager.selectFile')}
                  className={`w-4 h-4 shrink-0 rounded border-2 flex items-center justify-center cursor-pointer ${
                    ticked ? 'bg-bambu-green border-bambu-green' : 'border-bambu-gray/50'
                  }`}
                >
                  {ticked && <Check className="w-2.5 h-2.5 text-black" strokeWidth={3} />}
                </span>
                <div
                  role="option"
                  aria-selected={file.id === previewFileId}
                  aria-checked={ticked}
                  tabIndex={tabStop === key ? 0 : -1}
                  data-col={index}
                  data-kind="file"
                  data-id={file.id}
                  {...dnd.fileDragProps(file)}
                  onFocus={() => setFocusedKey(key)}
                  onClick={() => onPreviewFile(file)}
                  onDoubleClick={() => onOpenFile(file)}
                  className={`${optionClass(file.id === previewFileId)} ${ticked ? 'bg-bambu-green/10' : ''}`}
                >
                  {file.thumbnail_path ? (
                    <img
                      src={api.getLibraryFileThumbnailUrl(file.id, thumbnailVersions[file.id])}
                      alt=""
                      draggable={false}
                      className="w-5 h-5 shrink-0 rounded object-contain bg-bambu-dark"
                    />
                  ) : (
                    <FileBox className="w-4 h-4 shrink-0 text-bambu-gray/70" />
                  )}
                  <span className="flex-1 min-w-0 truncate" title={title}>{title}</span>
                </div>
              </div>
            );
          })}
        </div>
        {loading && (
          <div className="flex items-center gap-2 px-2 py-3 text-xs text-bambu-gray" role="status">
            <Loader2 className="w-4 h-4 animate-spin text-bambu-green" />
            {t('fileManager.loadingFiles')}
          </div>
        )}
        {query.isError && (
          <p className="px-2 py-3 text-xs text-red-700 dark:text-red-400" role="alert">
            {query.error instanceof Error ? query.error.message : t('fileManager.columns.loadFailed')}
          </p>
        )}
        {emptyLine && (
          <p className="px-2 py-3 text-xs text-bambu-gray" data-column-empty>
            {emptyLine}
          </p>
        )}
        {query.hasNextPage && total !== undefined && (
          <button
            type="button"
            onClick={() => query.fetchNextPage()}
            disabled={query.isFetchingNextPage}
            className="w-full mt-1 px-2 py-1.5 rounded text-xs text-bambu-green hover:bg-bambu-dark disabled:opacity-50 flex items-center justify-center gap-2"
          >
            {query.isFetchingNextPage && <Loader2 className="w-3 h-3 animate-spin" />}
            {t('fileManager.columns.showMore', { shown: files.length, total })}
          </button>
        )}
      </div>
    </div>
  );
}

export function LibraryColumnsView({
  folders,
  selectedFolderId,
  onSelectFolder,
  filters,
  filtersActive,
  selectedFiles,
  onToggleFile,
  onOpenFile,
  onDetails,
  onMoveFolder,
  canMoveFolders,
  onVisibleFilesChange,
  renderPreview,
  thumbnailVersions,
  dnd,
}: {
  /** The folder tree in the order the page shows it. */
  folders: LibraryFolderTree[] | undefined;
  selectedFolderId: number | null;
  onSelectFolder: (folderId: number | null) => void;
  filters: ColumnFilters;
  filtersActive: boolean;
  selectedFiles: number[];
  onToggleFile: (fileId: number) => void;
  /** "Open" — what a double click does in the other views. */
  onOpenFile: (file: LibraryFileListItem) => void;
  onDetails: (file: LibraryFileListItem) => void;
  /** Open the page's Move dialog for a folder. */
  onMoveFolder: (folder: LibraryFolderTree) => void;
  /** May the viewer move folders at all (the page's own rights helper)? */
  canMoveFolders: boolean;
  /** Every file the columns have loaded, in column order — what the bulk bar acts on. */
  onVisibleFilesChange: (files: LibraryFileListItem[]) => void;
  /** The page's own card for a file: its picture, facts, actions and menu. */
  renderPreview: (file: LibraryFileListItem) => ReactNode;
  thumbnailVersions: Record<number, number>;
  dnd: LibraryDragDrop;
}) {
  const { t } = useTranslation();
  const scrollerRef = useRef<HTMLDivElement>(null);
  const rootName = t('fileManager.columns.root');
  const hintId = useId();
  /** Why a folder may not be moved — the sentence its menu in the tree shows — or nothing. */
  const whyNotMovable = (folder: LibraryFolderTree): string | undefined =>
    !canMoveFolders
      ? t('fileManager.moveFolder.noPermission')
      : !isMovableFolder(folder)
        ? t('fileManager.moveFolder.externalFixed')
        : folder.can_rename === false
          ? t('fileManager.moveFolder.onlyOwner')
          : undefined;

  // A folder the tree does not hold (deleted, a stale link) leaves just the root.
  const path = useMemo(
    () => (selectedFolderId != null && folders ? (pathTo(folders, selectedFolderId) ?? []) : []),
    [folders, selectedFolderId],
  );
  const columns = useMemo(
    () => [
      { folderKey: null as FolderKey, name: rootName, subfolders: folders ?? [], folder: null as LibraryFolderTree | null },
      ...path.map((folder) => ({ folderKey: folder.id as FolderKey, name: folder.name, subfolders: folder.children, folder })),
    ],
    [folders, path, rootName],
  );
  const lastKey = columns[columns.length - 1].folderKey;

  // Below `sm` there is room for one column: the open folder (or the preview), with a way back.
  const [narrow, setNarrow] = useState(() => window.matchMedia('(max-width: 39.99rem)').matches);
  useEffect(() => {
    const media = window.matchMedia('(max-width: 39.99rem)');
    const onChange = () => setNarrow(media.matches);
    media.addEventListener('change', onChange);
    return () => media.removeEventListener('change', onChange);
  }, []);

  // The file shown in the preview column — of the LAST column's folder only.
  const [preview, setPreview] = useState<{ folderKey: FolderKey; file: LibraryFileListItem } | null>(null);
  const previewFile = preview && preview.folderKey === lastKey ? preview.file : null;

  // What the columns have loaded, by folder — reported up so the page's bulk
  // actions see the same files the operator does.
  const loadedRef = useRef(new Map<FolderKey, LibraryFileListItem[]>());
  const [loadedVersion, setLoadedVersion] = useState(0);
  const handleFiles = useCallback((folderKey: FolderKey, files: LibraryFileListItem[] | null) => {
    if (files) loadedRef.current.set(folderKey, files);
    else loadedRef.current.delete(folderKey);
    setLoadedVersion((version) => version + 1);
    // A previewed file follows its row (a rename shows through; a deleted file closes the preview).
    if (files) {
      setPreview((prev) => {
        if (!prev || prev.folderKey !== folderKey) return prev;
        const fresh = files.find((file) => file.id === prev.file.id);
        return fresh ? (fresh === prev.file ? prev : { folderKey, file: fresh }) : null;
      });
    }
  }, []);
  const reportedRef = useRef<LibraryFileListItem[]>([]);
  useEffect(() => {
    const next = columns.flatMap((column) => loadedRef.current.get(column.folderKey) ?? []);
    // Only a real change is reported: the page re-renders on every report.
    const last = reportedRef.current;
    if (next.length === last.length && next.every((file, i) => file === last[i])) return;
    reportedRef.current = next;
    onVisibleFilesChange(next);
  }, [loadedVersion, columns, onVisibleFilesChange]);
  useEffect(() => () => onVisibleFilesChange([]), [onVisibleFilesChange]);

  // Where the focus goes once the columns have followed a key: into column N, or to the preview.
  const [focusRequest, setFocusRequest] = useState<number | 'preview' | null>(null);
  const previewRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (focusRequest === null) return;
    if (focusRequest === 'preview') {
      if (!previewRef.current) return;
      previewRef.current.focus();
      setFocusRequest(null);
      return;
    }
    const list = scrollerRef.current?.querySelector<HTMLElement>(`[data-column-list="${focusRequest}"]`);
    if (!list) return;
    (list.querySelector<HTMLElement>('[role="option"][tabindex="0"]') ?? list).focus();
    setFocusRequest(null);
  }, [focusRequest, columns.length, previewFile]);

  // The newest column is the one being looked at: bring it into view.
  useEffect(() => {
    const scroller = scrollerRef.current;
    if (scroller) scroller.scrollLeft = scroller.scrollWidth;
  }, [columns.length, previewFile?.id]);

  const previewIn = (columnIndex: number, file: LibraryFileListItem) => {
    const folderKey = columns[columnIndex].folderKey;
    setPreview({ folderKey, file });
    // A file of an earlier column closes the columns to its right.
    if (folderKey !== lastKey) onSelectFolder(folderKey);
  };

  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    // Only a key pressed ON a row: a dialog or a menu opened from the preview
    // bubbles its keys through here too.
    const row = e.target as HTMLElement;
    if (!e.currentTarget.contains(row)) return;
    // A column without a row to stand on holds the focus itself.
    const onList = row.dataset.columnList !== undefined;
    if (row.getAttribute('role') !== 'option' && !onList) return;
    const columnIndex = Number(onList ? row.dataset.columnList : row.dataset.col);
    const id = Number(row.dataset.id);
    const isFolder = row.dataset.kind === 'folder';
    const rows = Array.from(e.currentTarget.querySelectorAll<HTMLElement>(`[role="option"][data-col="${columnIndex}"]`));
    const at = rows.indexOf(row);
    if (onList && !['ArrowDown', 'ArrowUp', 'Home', 'End', 'ArrowLeft'].includes(e.key)) return;
    const file = () => loadedRef.current.get(columns[columnIndex]?.folderKey ?? null)?.find((f) => f.id === id);
    let handled = true;
    switch (e.key) {
      case 'ArrowDown':
        rows[Math.min(rows.length - 1, at + 1)]?.focus();
        break;
      case 'ArrowUp':
        rows[Math.max(0, at - 1)]?.focus();
        break;
      case 'Home':
        rows[0]?.focus();
        break;
      case 'End':
        rows[rows.length - 1]?.focus();
        break;
      case 'ArrowRight':
      case 'Enter': {
        const target = file();
        if (isFolder) {
          onSelectFolder(id);
          setFocusRequest(columnIndex + 1);
        } else if (!target) {
          handled = false;
        } else if (e.key === 'Enter') {
          onOpenFile(target);
        } else {
          previewIn(columnIndex, target);
          setFocusRequest('preview');
        }
        break;
      }
      case 'ArrowLeft':
        if (columnIndex > 0) {
          const parent = e.currentTarget.querySelector<HTMLElement>(`[data-column-list="${columnIndex - 1}"]`);
          (parent?.querySelector<HTMLElement>('[role="option"][aria-selected="true"]') ??
            parent?.querySelector<HTMLElement>('[role="option"]'))?.focus();
        }
        break;
      case ' ':
        if (isFolder) handled = false;
        else onToggleFile(id);
        break;
      case 'm':
      case 'M': {
        // Move… for the focused folder — the keyboard's door to what a drag does.
        const folder = isFolder ? columns[columnIndex]?.subfolders.find((f) => f.id === id) : undefined;
        if (!folder || e.ctrlKey || e.metaKey || e.altKey || whyNotMovable(folder)) handled = false;
        else onMoveFolder(folder);
        break;
      }
      default:
        handled = false;
    }
    if (handled) e.preventDefault();
  };

  const shown = columns.map((column, index) => ({ column, index }));
  const visible = narrow ? shown.slice(-1) : shown;
  // The way back on a phone: out of the preview, else up one folder.
  const back = previewFile
    ? { label: columns[columns.length - 1].name, go: () => setPreview(null) }
    : columns.length > 1
      ? {
          label: columns[columns.length - 2].name,
          go: () => onSelectFolder(columns[columns.length - 2].folderKey),
        }
      : null;

  return (
    <div className="flex-1 min-w-0 min-h-0 flex flex-col gap-2">
      {filtersActive && (
        <p className="flex items-start gap-2 text-xs text-bambu-gray" data-testid="columns-filter-note">
          <Info className="w-3.5 h-3.5 shrink-0 mt-0.5" />
          {t('fileManager.columns.filterNote')}
        </p>
      )}
      <p id={hintId} className="sr-only">
        {t('fileManager.columns.keysHint')}
        {canMoveFolders ? ` ${t('fileManager.columns.keysHintMove')}` : ''}
      </p>
      {narrow && back && (
        <button
          type="button"
          onClick={back.go}
          aria-label={t('fileManager.columns.backTo', { name: back.label })}
          className="self-start min-h-11 px-2 flex items-center gap-1 text-sm text-bambu-green"
        >
          <ChevronLeft className="w-4 h-4" />
          <span className="truncate max-w-[70vw]">{back.label}</span>
        </button>
      )}
      {/* The columns scroll sideways inside this box — never the page. */}
      <div
        ref={scrollerRef}
        role="group"
        aria-label={t('fileManager.columns.label')}
        data-testid="library-columns"
        onKeyDown={onKeyDown}
        className="h-[70vh] lg:h-auto lg:flex-1 min-h-0 min-w-0 flex overflow-x-auto overflow-y-hidden bg-bambu-dark-secondary rounded-lg border border-bambu-dark-tertiary"
      >
        {visible.map(({ column, index }) => (
          <Column
            key={column.folderKey ?? 'root'}
            index={index}
            folderKey={column.folderKey}
            name={column.name}
            subfolders={column.subfolders}
            openChildId={columns[index + 1]?.folderKey ?? null}
            previewFileId={index === columns.length - 1 ? (previewFile?.id ?? null) : null}
            filters={filters}
            filtersActive={filtersActive}
            wide={narrow}
            hidden={narrow && previewFile !== null}
            selectedFiles={selectedFiles}
            thumbnailVersions={thumbnailVersions}
            dnd={dnd}
            hintId={hintId}
            move={
              column.folder
                ? { why: whyNotMovable(column.folder), open: () => onMoveFolder(column.folder as LibraryFolderTree) }
                : null
            }
            onOpenFolder={onSelectFolder}
            onPreviewFile={(file) => previewIn(index, file)}
            onOpenFile={onOpenFile}
            onToggleFile={onToggleFile}
            onFiles={handleFiles}
          />
        ))}
        {previewFile && (
          <div
            ref={previewRef}
            role="region"
            aria-label={t('fileManager.columns.preview', { name: previewFile.print_name || previewFile.filename })}
            tabIndex={-1}
            data-column-preview
            className={`${narrow ? 'w-full' : 'w-72'} shrink-0 min-h-0 overflow-y-auto p-3 space-y-3 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-bambu-green`}
          >
            <div className="flex items-center justify-between gap-2">
              <button
                type="button"
                onClick={() => onDetails(previewFile)}
                className="px-2 py-1 rounded text-xs text-bambu-green hover:bg-bambu-dark flex items-center gap-1"
              >
                <Info className="w-3.5 h-3.5" />
                {t('fileDetails.menu')}
              </button>
              <button
                type="button"
                onClick={() => setPreview(null)}
                aria-label={t('fileManager.columns.closePreview')}
                title={t('fileManager.columns.closePreview')}
                className="p-1 rounded text-bambu-gray hover:text-white hover:bg-bambu-dark"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            {renderPreview(previewFile)}
          </div>
        )}
      </div>
    </div>
  );
}
