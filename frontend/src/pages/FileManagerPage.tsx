import { useState, useRef, useCallback, useMemo, useEffect, type DragEvent } from 'react';
import { createPortal } from 'react-dom';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  FolderOpen,
  Loader2,
  Plus,
  Upload,
  Trash2,
  Download,
  MoreVertical,
  ChevronRight,
  FolderPlus,
  FileBox,
  Clock,
  HardDrive,
  Package,
  File,
  MoveRight,
  CheckSquare,
  Square,
  LayoutGrid,
  List,
  Search,
  ArrowUpNarrowWide,
  ArrowDownWideNarrow,
  AlertTriangle,
  X,
  Link2,
  Briefcase,
  Printer,
  Pencil,
  Play,
  Image,
  User,
  Box,
  History,
  RefreshCw,
  Lock,
  FolderSymlink,
  WrapText,
  ListCollapse,
  Layers,
  Cog,
  ExternalLink,
  Tag as TagIcon,
} from 'lucide-react';
import { api, ApiError } from '../api/client';
import type {
  LibraryFolderTree,
  LibraryFileListItem,
  LibraryFileListParams,
  LibraryFileUpdate,
  LibraryFolderCreate,
  LibraryFolderUpdate,
  ExternalFolderCreate,
  AppSettings,
  Permission,
} from '../api/client';
import { useLibraryScanProgress, type LibraryScanState } from '../hooks/useLibraryScanProgress';
import { Button } from '../components/Button';
import { PaginationBar } from '../components/PaginationBar';
import { ConfirmModal } from '../components/ConfirmModal';
import { LibraryPlateGalleryModal } from '../components/LibraryPlateGallery';
import { PrintModal } from '../components/PrintModal';
import { SliceModal } from '../components/SliceModal';
import { ModelViewerModal } from '../components/ModelViewerModal';
import { FileUploadModal } from '../components/FileUploadModal';
import { FolderReadmePanel } from '../components/FolderReadmePanel';
import { FolderTreePicker } from '../components/FolderTreePicker';
import { LibraryFileNotesButton } from '../components/LibraryFileNotesButton';
import { PurgeOldFilesModal } from '../components/PurgeOldFilesModal';
import { TrashSplitButton } from '../components/TrashSplitButton';
import { MakerWorldIcon } from '../components/BrandIcons';
import { useToast } from '../contexts/ToastContext';
import { useIsMobile } from '../hooks/useIsMobile';
import { useAuth } from '../contexts/AuthContext';
import { formatDateTime, formatDuration, type TimeFormat, type DateFormat } from '../utils/date';
import { fileActivityAt, formatFileSize } from '../utils/file';
import { FileTagBadges } from '../components/FileTagBadges';
import { PlateObjectsPreviewModal } from '../components/PlateObjectsPreviewModal';
import { SkipObjectsIcon } from '../components/SkipObjectsModal';
import { getTagStyle, isPrintable, isSliceable, isMultiPlate } from '../lib/fileTags';
import { openInSlicer, type SlicerType } from '../utils/slicer';
import { LibraryTagsModal } from '../components/LibraryTagsModal';
import { BulkTagsPickerModal } from '../components/BulkTagsPickerModal';
import { FileTagsPopover, type TagsPopoverAnchor } from '../components/FileTagsPopover';
import { QueueSequencer } from '../components/QueueSequencer';
import { libraryTagsQueryKey } from '../utils/libraryTagsQuery';
import { selectableProjects } from '../utils/projects';

type SortField = 'name' | 'date' | 'size' | 'type';
type SortDirection = 'asc' | 'desc';
type TFunction = (key: string, options?: Record<string, unknown>) => string;

/**
 * Debounces a rapidly-changing value (300ms, same as ArchivesPage's search
 * box) — both the search box and the username filter below are free-text
 * fields that would otherwise fire one server round-trip per keystroke.
 */
function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}

// Closed set of `LibraryFile.file_type` values the backend can ever produce
// — `detect_file_type` in backend/app/services/library_helpers.py, driven by
// `_SCANNABLE_EXTENSIONS` in backend/app/api/routes/library.py. Static on
// purpose (task 2, 2026-08-29 server-driven-lists fix round): deriving this
// from the fetched files (`files.map(f => f.file_type)`) only ever offered
// the types present on the CURRENT server-filtered/paginated page, so a type
// filtered or paged out of view silently vanished from its own dropdown.
const LIBRARY_FILE_TYPES = [
  '3mf', 'gcode', 'stl', 'obj', 'step', 'stp',
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'md',
] as const;

// New Folder Modal
interface NewFolderModalProps {
  parentId: number | null;
  /** Where the folder will land, for the destination line. */
  parentName: string | null;
  /** True when the current selection is an external folder — the new folder
   *  then goes to the ROOT (virtual folders cannot live inside a mirrored
   *  filesystem), and the modal says so instead of landing there silently. */
  externalRedirected: boolean;
  onClose: () => void;
  onSave: (data: LibraryFolderCreate) => void;
  isLoading: boolean;
  t: TFunction;
}

function NewFolderModal({ parentId, parentName, externalRedirected, onClose, onSave, isLoading, t }: NewFolderModalProps) {
  const [name, setName] = useState('');

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    onSave({ name: name.trim(), parent_id: parentId });
  };

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4">
      <div className="bg-bambu-dark-secondary rounded-lg w-full max-w-sm border border-bambu-dark-tertiary">
        <div className="p-4 border-b border-bambu-dark-tertiary">
          <h2 className="text-lg font-semibold text-white">{t('fileManager.newFolder')}</h2>
          <p className="text-xs text-bambu-gray mt-1">
            {t('fileManager.newFolderDestination', {
              destination: parentId !== null && parentName ? parentName : t('fileManager.allFiles'),
            })}
          </p>
          {externalRedirected && (
            <p className="text-xs text-amber-600 dark:text-amber-400 mt-1">
              {t('fileManager.newFolderExternalRedirect')}
            </p>
          )}
        </div>
        <form onSubmit={handleSubmit} className="p-4 space-y-4">
          <div>
            <label className="block text-sm font-medium text-white mb-1">
              {t('fileManager.folderName')}
            </label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-3 py-2 text-white placeholder-bambu-gray focus:outline-none focus:border-bambu-green"
              placeholder={t('fileManager.folderNamePlaceholder')}
              autoFocus
              required
            />
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" variant="secondary" onClick={onClose}>
              {t('common.cancel')}
            </Button>
            <Button type="submit" disabled={!name.trim() || isLoading}>
              {isLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : t('common.create')}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}

// External Folder Modal
interface ExternalFolderModalProps {
  onClose: () => void;
  onSave: (data: ExternalFolderCreate) => void;
  isLoading: boolean;
  t: TFunction;
}

function ExternalFolderModal({ onClose, onSave, isLoading, t }: ExternalFolderModalProps) {
  const [name, setName] = useState('');
  const [path, setPath] = useState('');
  const [readonly, setReadonly] = useState(true);
  const [showHidden, setShowHidden] = useState(false);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    onSave({
      name: name.trim(),
      external_path: path.trim(),
      readonly,
      show_hidden: showHidden,
    });
  };

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4">
      <div className="bg-bambu-dark-secondary rounded-lg w-full max-w-md border border-bambu-dark-tertiary">
        <div className="p-4 border-b border-bambu-dark-tertiary">
          <h2 className="text-lg font-semibold text-white flex items-center gap-2">
            <FolderSymlink className="w-5 h-5 text-bambu-green" />
            {t('fileManager.linkExternalFolder')}
          </h2>
          <p className="text-sm text-bambu-gray mt-1">{t('fileManager.linkExternalFolderDescription')}</p>
        </div>
        <form onSubmit={handleSubmit} className="p-4 space-y-4">
          <div>
            <label className="block text-sm font-medium text-white mb-1">
              {t('fileManager.folderName')}
            </label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-3 py-2 text-white placeholder-bambu-gray focus:outline-none focus:border-bambu-green"
              placeholder={t('fileManager.externalFolderNamePlaceholder')}
              autoFocus
              required
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-white mb-1">
              {t('fileManager.externalPath')}
            </label>
            <input
              type="text"
              value={path}
              onChange={(e) => setPath(e.target.value)}
              className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-3 py-2 text-white placeholder-bambu-gray focus:outline-none focus:border-bambu-green font-mono text-sm"
              placeholder="/mnt/nas/3d-prints"
              required
            />
            <p className="text-xs text-bambu-gray mt-1">{t('fileManager.externalPathHelp')}</p>
          </div>
          <div className="space-y-2">
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={readonly}
                onChange={(e) => setReadonly(e.target.checked)}
                className="rounded border-bambu-dark-tertiary bg-bambu-dark text-bambu-green focus:ring-bambu-green"
              />
              <span className="text-sm text-white">{t('fileManager.readOnly')}</span>
              <span className="text-xs text-bambu-gray">({t('fileManager.readOnlyHelp')})</span>
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={showHidden}
                onChange={(e) => setShowHidden(e.target.checked)}
                className="rounded border-bambu-dark-tertiary bg-bambu-dark text-bambu-green focus:ring-bambu-green"
              />
              <span className="text-sm text-white">{t('fileManager.showHiddenFiles')}</span>
            </label>
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" variant="secondary" onClick={onClose}>
              {t('common.cancel')}
            </Button>
            <Button type="submit" disabled={!name.trim() || !path.trim() || isLoading}>
              {isLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : t('fileManager.linkFolder')}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}

// Rename Modal
interface RenameModalProps {
  type: 'file' | 'folder';
  currentName: string;
  onClose: () => void;
  onSave: (newName: string) => void;
  isLoading: boolean;
  t: TFunction;
}

// FAT32/exFAT-illegal characters — mirrors backend validate_print_filename so
// the user gets instant feedback (Bambu Studio parity) instead of a round-trip
// 400 or an obscure FTP 553 at print time (#1540).
const INVALID_FILENAME_CHARS = '<>:"/\\|?*';

function RenameModal({ type, currentName, onClose, onSave, isLoading, t }: RenameModalProps) {
  // For files, separate the extension so users can only edit the base name
  // Handle compound extensions like .gcode.3mf
  const fileExtension = type === 'file' ? (currentName.match(/(\.gcode\.3mf|\.3mf|\.gcode)$/i)?.[1] ?? '') : '';
  const baseName = type === 'file' && fileExtension ? currentName.slice(0, -fileExtension.length) : currentName;
  const [name, setName] = useState(baseName);

  // First offending character (null when clean). Trailing space/dot are also
  // illegal on the SD card but the extension is re-appended for files, so we
  // only surface the character-set violation inline here.
  const invalidChar = [...name].find((ch) => INVALID_FILENAME_CHARS.includes(ch) || ch.charCodeAt(0) < 0x20) ?? null;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (invalidChar) return;
    const fullName = type === 'file' ? name.trim() + fileExtension : name.trim();
    if (name.trim() && fullName !== currentName) {
      onSave(fullName);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4">
      <div className="bg-bambu-dark-secondary rounded-lg w-full max-w-sm border border-bambu-dark-tertiary">
        <div className="p-4 border-b border-bambu-dark-tertiary">
          <h2 className="text-lg font-semibold text-white">{type === 'file' ? t('fileManager.renameFile') : t('fileManager.renameFolder')}</h2>
        </div>
        <form onSubmit={handleSubmit} className="p-4 space-y-4">
          <div>
            <label className="block text-sm font-medium text-white mb-1">
              {t('common.name')}
            </label>
            <div className="flex items-center bg-bambu-dark border border-bambu-dark-tertiary rounded focus-within:border-bambu-green">
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                className="flex-1 bg-transparent px-3 py-2 text-white placeholder-bambu-gray focus:outline-none min-w-0"
                autoFocus
                required
              />
              {fileExtension && (
                <span className="pr-3 text-bambu-gray text-sm select-none whitespace-nowrap">{fileExtension}</span>
              )}
            </div>
            {invalidChar && (
              <p className="mt-1 text-sm text-red-700 dark:text-red-400">
                {t('fileManager.invalidFilenameChar', { char: invalidChar })}
              </p>
            )}
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" variant="secondary" onClick={onClose}>
              {t('common.cancel')}
            </Button>
            <Button type="submit" disabled={!name.trim() || name.trim() === baseName || isLoading || !!invalidChar}>
              {isLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : t('common.rename')}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}

// Move Files Modal
interface MoveFilesModalProps {
  folders: LibraryFolderTree[];
  selectedFiles: number[];
  currentFolderId: number | null;
  onClose: () => void;
  onMove: (folderId: number | null) => void;
  isLoading: boolean;
  t: TFunction;
}

function MoveFilesModal({ folders, selectedFiles, currentFolderId, onClose, onMove, isLoading, t }: MoveFilesModalProps) {
  const [targetFolder, setTargetFolder] = useState<number | null>(null);

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4">
      <div className="bg-bambu-dark-secondary rounded-lg w-full max-w-sm border border-bambu-dark-tertiary">
        <div className="p-4 border-b border-bambu-dark-tertiary">
          <h2 className="text-lg font-semibold text-white">{t('fileManager.moveFiles', { count: selectedFiles.length })}</h2>
        </div>
        <div className="p-4 space-y-4">
          <FolderTreePicker
            folders={folders}
            value={targetFolder}
            onChange={setTargetFolder}
            rootLabel={t('fileManager.rootNoFolder')}
            disabledId={currentFolderId}
            disabledLabel={t('fileManager.current')}
          />
          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" variant="secondary" onClick={onClose}>
              {t('common.cancel')}
            </Button>
            <Button onClick={() => onMove(targetFolder)} disabled={isLoading}>
              {isLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : t('common.move')}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

// Link Folder Modal
interface LinkFolderModalProps {
  folder: LibraryFolderTree;
  onClose: () => void;
  onLink: (update: LibraryFolderUpdate) => void;
  isLoading: boolean;
  t: TFunction;
}

function LinkFolderModal({ folder, onClose, onLink, isLoading, t }: LinkFolderModalProps) {
  // m044: folder ↔ projects is M2M.
  const [selectedProjectIds, setSelectedProjectIds] = useState<Set<number>>(
    () => new Set(folder.projects.map((p) => p.id)),
  );

  const { data: allProjects } = useQuery({
    queryKey: ['projects'],
    queryFn: () => api.getProjects(),
  });

  // Whatever this folder is already in stays offered, archived or not.
  const projects = useMemo(
    () => selectableProjects(allProjects, selectedProjectIds),
    [allProjects, selectedProjectIds],
  );

  const toggleProject = (projectId: number) => {
    setSelectedProjectIds((prev) => {
      const next = new Set(prev);
      if (next.has(projectId)) next.delete(projectId);
      else next.add(projectId);
      return next;
    });
  };

  const handleSave = () => {
    // Replace the project list. Per-project unlink happens by deselecting
    // individual chips above; the legacy "wipe everything" red button is gone.
    onLink({ project_ids: Array.from(selectedProjectIds) });
  };

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4">
      <div className="bg-bambu-dark-secondary rounded-lg w-full max-w-md border border-bambu-dark-tertiary">
        <div className="p-4 border-b border-bambu-dark-tertiary flex items-center justify-between">
          <h2 className="text-lg font-semibold text-white flex items-center gap-2">
            <Link2 className="w-5 h-5 text-bambu-green" />
            {t('fileManager.linkFolder')}
          </h2>
          <button onClick={onClose} className="p-1 hover:bg-bambu-dark rounded">
            <X className="w-5 h-5 text-bambu-gray" />
          </button>
        </div>

        <div className="p-4 space-y-4">
          <p className="text-sm text-bambu-gray">
            {t('fileManager.linkFolderDescription', { name: folder.name })}
          </p>

          {/* Chip multi-select. Each project is a clickable colored chip;
              selected = full color + check, unselected = outline only. */}
          <div className="bg-bambu-dark rounded-lg p-3">
            {projects && projects.length > 0 ? (
              <div className="flex flex-wrap gap-1.5">
                {projects.map((project) => {
                  const selected = selectedProjectIds.has(project.id);
                  return (
                    <button
                      key={project.id}
                      type="button"
                      onClick={() => toggleProject(project.id)}
                      // m044 (post-feedback): selected chips show an
                      // inline × so the per-project unlink affordance
                      // is visually obvious — replaces the legacy
                      // "wipe all" red button.
                      title={
                        selected
                          ? t('fileManager.removeFromProject', { name: project.name })
                          : project.name
                      }
                      className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium border transition-colors ${
                        selected
                          ? 'border-transparent text-white'
                          : 'border-bambu-dark-tertiary text-bambu-gray hover:text-white hover:border-bambu-gray'
                      }`}
                      style={
                        selected
                          ? { backgroundColor: project.color || '#00ae42' }
                          : undefined
                      }
                    >
                      <div
                        className="w-2 h-2 rounded-full"
                        style={{ backgroundColor: project.color || '#00ae42' }}
                      />
                      {project.name}
                      {selected && <X className="w-3 h-3 ml-0.5 opacity-80" />}
                    </button>
                  );
                })}
              </div>
            ) : (
              <p className="text-sm text-bambu-gray text-center py-4">
                {t('fileManager.noProjectsFound')}
              </p>
            )}
            {selectedProjectIds.size === 0 && (
              <p className="text-xs text-bambu-gray italic mt-2">
                {t('fileManager.noProjectsSelected')}
              </p>
            )}
          </div>
        </div>

        <div className="p-4 border-t border-bambu-dark-tertiary flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button onClick={handleSave} disabled={isLoading}>
            {isLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : t('common.save')}
          </Button>
        </div>
      </div>
    </div>
  );
}

// Link File Modal — per-file project link
interface LinkFileModalProps {
  file: LibraryFileListItem;
  onClose: () => void;
  onLink: (update: LibraryFileUpdate) => void;
  isLoading: boolean;
  t: TFunction;
}

function LinkFileModal({ file, onClose, onLink, isLoading, t }: LinkFileModalProps) {
  // m044: file ↔ projects is M2M. Chip multi-select.
  const [selectedProjectIds, setSelectedProjectIds] = useState<Set<number>>(
    () => new Set(file.project_ids ?? []),
  );

  const { data: allProjects } = useQuery({
    queryKey: ['projects'],
    queryFn: () => api.getProjects(),
  });

  // Whatever this folder is already in stays offered, archived or not.
  const projects = useMemo(
    () => selectableProjects(allProjects, selectedProjectIds),
    [allProjects, selectedProjectIds],
  );

  const toggleProject = (projectId: number) => {
    setSelectedProjectIds((prev) => {
      const next = new Set(prev);
      if (next.has(projectId)) next.delete(projectId);
      else next.add(projectId);
      return next;
    });
  };

  const handleSave = () => {
    // Per-project unlink lives on the chips (deselect = remove from
    // file's project list). Saving without any selected chip is the
    // explicit "unlink from everything" path.
    onLink({ project_ids: Array.from(selectedProjectIds) });
  };

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4">
      <div className="bg-bambu-dark-secondary rounded-lg w-full max-w-md border border-bambu-dark-tertiary">
        <div className="p-4 border-b border-bambu-dark-tertiary flex items-center justify-between">
          <h2 className="text-lg font-semibold text-white flex items-center gap-2">
            <Link2 className="w-5 h-5 text-bambu-green" />
            {t('fileManager.linkFile')}
          </h2>
          <button onClick={onClose} className="p-1 hover:bg-bambu-dark rounded">
            <X className="w-5 h-5 text-bambu-gray" />
          </button>
        </div>

        <div className="p-4 space-y-4">
          <p className="text-sm text-bambu-gray">
            {t('fileManager.linkFileDescription', { name: file.print_name || file.filename })}
          </p>

          <div className="bg-bambu-dark rounded-lg p-3">
            {projects && projects.length > 0 ? (
              <div className="flex flex-wrap gap-1.5">
                {projects.map((project) => {
                  const selected = selectedProjectIds.has(project.id);
                  return (
                    <button
                      key={project.id}
                      type="button"
                      onClick={() => toggleProject(project.id)}
                      title={
                        selected
                          ? t('fileManager.removeFromProject', { name: project.name })
                          : project.name
                      }
                      className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium border transition-colors ${
                        selected
                          ? 'border-transparent text-white'
                          : 'border-bambu-dark-tertiary text-bambu-gray hover:text-white hover:border-bambu-gray'
                      }`}
                      style={
                        selected
                          ? { backgroundColor: project.color || '#00ae42' }
                          : undefined
                      }
                    >
                      <div
                        className="w-2 h-2 rounded-full"
                        style={{ backgroundColor: project.color || '#00ae42' }}
                      />
                      {project.name}
                      {selected && <X className="w-3 h-3 ml-0.5 opacity-80" />}
                    </button>
                  );
                })}
              </div>
            ) : (
              <p className="text-sm text-bambu-gray text-center py-4">{t('fileManager.noProjectsFound')}</p>
            )}
            {selectedProjectIds.size === 0 && (
              <p className="text-xs text-bambu-gray italic mt-2">
                {t('fileManager.noProjectsSelected')}
              </p>
            )}
          </div>
        </div>

        <div className="p-4 border-t border-bambu-dark-tertiary flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button onClick={handleSave} disabled={isLoading}>
            {isLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : t('common.save')}
          </Button>
        </div>
      </div>
    </div>
  );
}

// Folder Tree Item
interface FolderTreeItemProps {
  folder: LibraryFolderTree;
  selectedFolderId: number | null;
  onSelect: (id: number | null) => void;
  onDelete: (id: number) => void;
  onLink: (folder: LibraryFolderTree) => void;
  onRename: (folder: LibraryFolderTree) => void;
  depth?: number;
  wrapNames?: boolean;
  defaultExpanded?: boolean;
  hasPermission: (permission: Permission) => boolean;
  t: TFunction;
  timeFormat: TimeFormat;
  dateFormat: DateFormat;
}

function FolderTreeItem({ folder, selectedFolderId, onSelect, onDelete, onLink, onRename, depth = 0, wrapNames = false, defaultExpanded = true, hasPermission, t, timeFormat, dateFormat }: FolderTreeItemProps) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const [showActions, setShowActions] = useState(false);
  const hasChildren = folder.children.length > 0;
  // m044: M2M projects.
  const isLinked = folder.projects.length > 0;
  const isExternal = folder.is_external;
  // The row has no room for a date column — the order icon → name → lock →
  // link → count → menu is deliberate and keeps every row's right edge aligned.
  // So the sort key lives in the name's tooltip, where it explains why a folder
  // sits where it does under "sort by activity" (#2680). Omitted entirely when
  // the server sent nothing rather than shown as an empty line.
  const nameTitle = folder.latest_activity_at
    ? `${folder.name}\n${t('fileManager.lastActivity')}: ${formatDateTime(folder.latest_activity_at, timeFormat, dateFormat)}`
    : folder.name;

  return (
    <div>
      <div
        className={`group flex items-center gap-1 px-2 py-1.5 rounded cursor-pointer transition-colors ${
          selectedFolderId === folder.id
            ? 'bg-bambu-green/20 text-bambu-green'
            : 'hover:bg-bambu-dark text-white'
        }`}
        style={{ paddingLeft: `${8 + depth * 12}px` }}
        onClick={() => onSelect(folder.id)}
      >
        {hasChildren ? (
          <button
            onClick={(e) => {
              e.stopPropagation();
              setExpanded(!expanded);
            }}
            className="p-0.5 hover:bg-bambu-dark-tertiary rounded"
          >
            <ChevronRight className={`w-3.5 h-3.5 transition-transform ${expanded ? 'rotate-90' : ''}`} />
          </button>
        ) : (
          <div className="w-4.5" />
        )}
        {isExternal ? (
          <FolderSymlink className="w-4 h-4 text-purple-600 dark:text-purple-400 flex-shrink-0" />
        ) : (
          <FolderOpen className="w-4 h-4 text-bambu-green flex-shrink-0" />
        )}
        <span className={`text-sm flex-1 min-w-0 ${wrapNames ? 'break-all' : 'truncate'}`} title={nameTitle}>{folder.name}</span>
        {/* Read-only indicator for external folders — non-interactive
            metadata, kept adjacent to the name. */}
        {isExternal && folder.external_readonly && (
          <span title={t('fileManager.readOnly')}>
            <Lock className="w-3 h-3 text-amber-600 dark:text-amber-400 flex-shrink-0" />
          </span>
        )}
        {/* Order across all rows is strictly: link/unlink → count → menu,
            so the count sits right next to the three-dots trigger and the
            row's right edge stays vertically aligned regardless of whether
            the folder is linked, external, or empty. */}
        {isLinked ? (
          <button
            onClick={(e) => { e.stopPropagation(); onLink(folder); }}
            className="flex-shrink-0 flex items-center gap-1 text-xs px-1.5 py-0.5 rounded bg-blue-100 dark:bg-blue-500/20 text-blue-700 dark:text-blue-400 hover:bg-blue-200 dark:hover:bg-blue-500/30 transition-colors"
            title={folder.projects.map(p => p.name).join(', ')}
          >
            <Link2 className="w-3 h-3" />
            <Briefcase className="w-3 h-3" />
            {folder.projects.length > 1 && (
              <span className="text-[10px] font-semibold">×{folder.projects.length}</span>
            )}
          </button>
        ) : !isExternal ? (
          <button
            onClick={(e) => { e.stopPropagation(); onLink(folder); }}
            className="flex-shrink-0 p-1 rounded hover:bg-bambu-dark-tertiary"
            title={t('fileManager.linkToProject')}
          >
            <Link2 className="w-3.5 h-3.5 text-bambu-gray hover:text-bambu-green" />
          </button>
        ) : null}
        {folder.file_count > 0 && (
          <span className="flex-shrink-0 text-xs text-bambu-gray">{folder.file_count}</span>
        )}
        <div className={`flex-shrink-0 flex items-center gap-0.5 transition-opacity ${wrapNames ? '' : 'opacity-0 group-hover:opacity-100'}`} onClick={(e) => e.stopPropagation()}>
          <div className="relative">
            <button
              onClick={() => setShowActions(!showActions)}
              className="p-1 rounded hover:bg-bambu-dark-tertiary"
            >
              <MoreVertical className="w-3.5 h-3.5 text-bambu-gray" />
            </button>
            {showActions && (
              <>
                <div className="fixed inset-0 z-10" onClick={() => setShowActions(false)} />
                <div className="absolute right-0 top-full mt-1 z-20 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl py-1 min-w-[120px]">
                <button
                  className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${
                    hasPermission('library:update_all') ? 'text-white hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'
                  }`}
                  onClick={() => { if (hasPermission('library:update_all')) { onRename(folder); setShowActions(false); } }}
                  disabled={!hasPermission('library:update_all')}
                  title={!hasPermission('library:update_all') ? t('fileManager.noPermissionRenameFolder') : undefined}
                >
                  <Pencil className="w-3.5 h-3.5" />
                  {t('common.rename')}
                </button>
                <button
                  className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${
                    hasPermission('library:update_all') ? 'text-white hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'
                  }`}
                  onClick={() => { if (hasPermission('library:update_all')) { onLink(folder); setShowActions(false); } }}
                  disabled={!hasPermission('library:update_all')}
                  title={!hasPermission('library:update_all') ? t('fileManager.noPermissionLinkFolder') : undefined}
                >
                  <Link2 className="w-3.5 h-3.5" />
                  {isLinked ? t('fileManager.changeLink') : t('fileManager.linkTo')}
                </button>
                {/* A folder holds nobody's ownership, so clearing one that has
                    contents needs delete_all. delete_own is enough for an empty
                    one (#1781) — but only the BACKEND can say "empty", because a
                    folder holding somebody's trashed file looks empty here and
                    still refuses. This gate is the hint, not the rule. */}
                {(() => {
                  const looksEmpty = folder.file_count === 0 && folder.children.length === 0;
                  const canDelete =
                    hasPermission('library:delete_all') ||
                    (hasPermission('library:delete_own') && looksEmpty && !folder.is_external);
                  const why = !canDelete
                    ? hasPermission('library:delete_own') && !looksEmpty
                      ? t('fileManager.onlyEmptyFolders')
                      : t('fileManager.noPermissionDeleteFolder')
                    : undefined;
                  return (
                <button
                  className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${
                    canDelete ? 'text-red-700 dark:text-red-400 hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'
                  }`}
                  onClick={() => { if (canDelete) { onDelete(folder.id); setShowActions(false); } }}
                  disabled={!canDelete}
                  title={why}
                >
                  <Trash2 className="w-3.5 h-3.5" />
                  {t('common.delete')}
                </button>
                  );
                })()}
              </div>
              </>
            )}
          </div>
        </div>
      </div>
      {hasChildren && expanded && (
        <div>
          {folder.children.map((child) => (
            <FolderTreeItem
              key={child.id}
              folder={child}
              selectedFolderId={selectedFolderId}
              onSelect={onSelect}
              onDelete={onDelete}
              onLink={onLink}
              onRename={onRename}
              depth={depth + 1}
              wrapNames={wrapNames}
              defaultExpanded={defaultExpanded}
              hasPermission={hasPermission}
              t={t}
              timeFormat={timeFormat}
              dateFormat={dateFormat}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// Slice-related predicates moved to ``lib/fileTags`` so FileCard /
// FileListActions / ProjectDetailPage / bulk-action handlers all read
// from the same ``file_tags`` source. ``isPrintable(file)`` /
// ``isSliceable(file)`` / ``isMultiPlate(file)`` replace the two
// filename-suffix helpers that used to live here.

// File Card
interface FileCardProps {
  file: LibraryFileListItem;
  isSelected: boolean;
  isMobile: boolean;
  onSelect: (id: number) => void;
  /** Open the archive (print history) filtered to this file's prints. */
  onOpenArchives: (file: LibraryFileListItem) => void;
  onDelete: (id: number) => void;
  onDownload: (id: number) => void;
  onAddToQueue?: (id: number) => void;
  onPrint?: (file: LibraryFileListItem) => void;
  onSlice?: (file: LibraryFileListItem) => void;
  onOpenInSlicer?: (file: LibraryFileListItem) => void;
  useSlicerApi?: boolean;
  onPreview3d?: (file: LibraryFileListItem) => void;
  onRename?: (file: LibraryFileListItem) => void;
  onLink?: (file: LibraryFileListItem) => void;
  onGenerateThumbnail?: (file: LibraryFileListItem) => void;
  onPlateGallery?: (file: LibraryFileListItem) => void;
  /** Move this one file — the toolbar's Move, without the checkbox dance. */
  onMove?: (file: LibraryFileListItem) => void;
  /** Per-file tag popover; the anchor is where the entry was clicked. */
  onTags?: (file: LibraryFileListItem, anchor: TagsPopoverAnchor) => void;
  /** #1268 — click a user-tag chip to toggle it in the filter rail. */
  onTagClick?: (tagId: number) => void;
  thumbnailVersion?: number;
  /** True while a thumbnail-regeneration mutation is in flight for THIS
   *  file. Drives the loading overlay on the card thumbnail so the
   *  operator sees the action took effect (otherwise it ran fully in
   *  the background with no visual feedback). */
  isRegeneratingThumbnail?: boolean;
  hasPermission: (permission: Permission) => boolean;
  canModify: (resource: 'queue' | 'archives' | 'library', action: 'update' | 'delete' | 'reprint', createdById: number | null | undefined) => boolean;
  authEnabled: boolean;
  timeFormat?: TimeFormat;
  dateFormat?: DateFormat;
  t: TFunction;
}

/**
 * The box of the card or row that owns `trigger`, for anchoring the tag
 * popover.
 *
 * Measured from the ⋮ TRIGGER rather than from the clicked menu entry: the menu
 * is portal-rendered at the document root, so a menu item has no DOM ancestry
 * back to its file. Falls back to the trigger's own box if the marker is
 * missing, which puts the panel beside the button instead of nowhere.
 */
function anchorFrom(
  trigger: HTMLElement | null,
  selector: string,
  placement: 'card' | 'row',
): TagsPopoverAnchor {
  const el = trigger?.closest(selector) ?? trigger;
  const r = el?.getBoundingClientRect();
  return {
    rect: r
      ? { top: r.top, right: r.right, bottom: r.bottom, height: r.height }
      : { top: 0, right: 0, bottom: 0, height: 0 },
    placement,
  };
}

function FileListActions({ file, t, hasPermission, canModify, onPrint, onSchedule, onSlice, onOpenInSlicer, useSlicerApi, onPreview3d, onDownload, onRename, onGenerateThumbnail, onMove, onTags, onDelete }: {
  file: LibraryFileListItem;
  t: TFunction;
  hasPermission: (permission: Permission) => boolean;
  canModify: (resource: 'queue' | 'archives' | 'library', action: 'update' | 'delete' | 'reprint', createdById: number | null | undefined) => boolean;
  onPrint: (f: LibraryFileListItem) => void;
  onSchedule: (f: LibraryFileListItem) => void;
  onSlice?: (f: LibraryFileListItem) => void;
  onOpenInSlicer?: (f: LibraryFileListItem) => void;
  useSlicerApi?: boolean;
  onPreview3d: (f: LibraryFileListItem) => void;
  onDownload: (id: number) => void;
  onRename: (f: LibraryFileListItem) => void;
  onGenerateThumbnail: (f: LibraryFileListItem) => void;
  onMove?: (f: LibraryFileListItem) => void;
  onTags?: (f: LibraryFileListItem, anchor: TagsPopoverAnchor) => void;
  onDelete: (id: number) => void;
}) {
  // ⚠️ The two modes need different permissions: slicing through the sidecar
  // writes a new library file, while opening in a desktop slicer is a download.
  const sliceDisabled = useSlicerApi ? !hasPermission('library:upload') : !hasPermission('library:read');
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  // Portal-rendered dropdown escapes the list container's `overflow-hidden`,
  // so the menu isn't clipped inside the row. Coords are computed from the
  // trigger button and recalculated on scroll/resize.
  const [coords, setCoords] = useState<{ top: number; right: number } | null>(null);
  const MENU_WIDTH = 240;

  useEffect(() => {
    if (!open) return;
    const update = () => {
      const btn = triggerRef.current;
      if (!btn) return;
      const rect = btn.getBoundingClientRect();
      // Align menu's right edge to the trigger's right edge, hang below.
      const right = Math.max(8, window.innerWidth - rect.right);
      let top = rect.bottom + 4;
      // Flip above when there isn't enough room below.
      const estimatedHeight = 280;
      if (top + estimatedHeight > window.innerHeight - 8 && rect.top > estimatedHeight) {
        top = rect.top - estimatedHeight - 4;
      }
      setCoords({ top, right });
    };
    update();
    window.addEventListener('resize', update);
    window.addEventListener('scroll', update, true);
    return () => {
      window.removeEventListener('resize', update);
      window.removeEventListener('scroll', update, true);
    };
  }, [open]);

  return (
    <div onClick={(e) => e.stopPropagation()}>
      <button ref={triggerRef} onClick={() => setOpen(!open)} className="p-1.5 rounded hover:bg-bambu-dark transition-colors">
        <MoreVertical className="w-4 h-4 text-bambu-gray" />
      </button>
      {open && createPortal(
        <>
          <div className="fixed inset-0 z-[55]" onClick={() => setOpen(false)} />
          <div
            style={{
              position: 'fixed',
              top: coords?.top ?? 0,
              right: coords?.right ?? 0,
              width: MENU_WIDTH,
              visibility: coords ? 'visible' : 'hidden',
            }}
            className="z-[60] bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl py-1 whitespace-nowrap"
          >
            {isPrintable(file) && (
              <>
                <button
                  className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${hasPermission('printers:control') ? 'text-bambu-green hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'}`}
                  onClick={() => { if (hasPermission('printers:control')) { onPrint(file); setOpen(false); } }}
                  disabled={!hasPermission('printers:control')}
                >
                  <Printer className="w-3.5 h-3.5" />
                  {t('common.print')}
                </button>
                <button
                  className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${hasPermission('queue:create') ? 'text-white hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'}`}
                  onClick={() => { if (hasPermission('queue:create')) { onSchedule(file); setOpen(false); } }}
                  disabled={!hasPermission('queue:create')}
                >
                  <Clock className="w-3.5 h-3.5" />
                  {t('fileManager.schedulePrint')}
                </button>
              </>
            )}
            {/* ⚠️ The entry is no longer gated on the sidecar being enabled.
                With it off this is the desktop hand-off, which has always
                worked and simply had no way in from here. The two also need
                DIFFERENT permissions: slicing on the server writes a new
                library file (library:upload), while opening in a desktop app
                is a download (library:read). */}
            {isSliceable(file) && (onSlice || onOpenInSlicer) && (
              <button
                className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${!sliceDisabled ? 'text-white hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'}`}
                onClick={() => {
                  if (sliceDisabled) return;
                  if (useSlicerApi) onSlice?.(file);
                  else onOpenInSlicer?.(file);
                  setOpen(false);
                }}
                disabled={sliceDisabled}
                title={sliceDisabled ? (useSlicerApi ? t('fileManager.noPermissionSlice') : t('fileManager.noPermissionDownload')) : undefined}
              >
                {useSlicerApi ? <Cog className="w-3.5 h-3.5" /> : <ExternalLink className="w-3.5 h-3.5" />}
                {useSlicerApi ? t('slice.action') : t('modelViewer.openInSlicer')}
              </button>
            )}
            {(file.file_type === '3mf' || file.file_type === 'gcode' || file.file_type === 'stl' || file.file_type === 'obj') && (
              <button
                className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${hasPermission('library:read') ? 'text-white hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'}`}
                onClick={() => { if (hasPermission('library:read')) { onPreview3d(file); setOpen(false); } }}
                disabled={!hasPermission('library:read')}
              >
                <Box className="w-3.5 h-3.5" />
                {t('fileManagerModal.threeView')}
              </button>
            )}
            {file.source_type === 'makerworld' && file.source_url && (
              <a
                href={file.source_url}
                target="_blank"
                rel="noopener noreferrer"
                onClick={() => setOpen(false)}
                className="w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 text-white hover:bg-bambu-dark"
              >
                <MakerWorldIcon className="w-3.5 h-3.5 text-white" />
                {t('fileManager.source.openOriginal', { defaultValue: 'Open on MakerWorld' })}
              </a>
            )}
            <button
              className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${hasPermission('library:read') ? 'text-white hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'}`}
              onClick={() => { if (hasPermission('library:read')) { onDownload(file.id); setOpen(false); } }}
              disabled={!hasPermission('library:read')}
            >
              <Download className="w-3.5 h-3.5" />
              {t('common.download')}
            </button>
            <button
              className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${canModify('library', 'update', file.created_by_id) ? 'text-white hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'}`}
              onClick={() => { if (canModify('library', 'update', file.created_by_id)) { onRename(file); setOpen(false); } }}
              disabled={!canModify('library', 'update', file.created_by_id)}
            >
              <Pencil className="w-3.5 h-3.5" />
              {t('common.rename')}
            </button>
            {/* Move and Tags — the two actions that used to exist only in the
                multi-select toolbar, so moving one file meant ticking its
                checkbox first. Same permission gate as Rename above, so a
                *_own user sees them on their own files only. */}
            {onMove && (
              <button
                className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${canModify('library', 'update', file.created_by_id) ? 'text-white hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'}`}
                onClick={() => { if (canModify('library', 'update', file.created_by_id)) { onMove(file); setOpen(false); } }}
                disabled={!canModify('library', 'update', file.created_by_id)}
              >
                <MoveRight className="w-3.5 h-3.5" />
                {t('common.move')}
              </button>
            )}
            {onTags && (
              <button
                className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${canModify('library', 'update', file.created_by_id) ? 'text-white hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'}`}
                onClick={() => {
                  if (!canModify('library', 'update', file.created_by_id)) return;
                  // Anchored to the ROW, not the pointer: this menu is
                  // portal-rendered away from the row it belongs to, so the
                  // cursor is nowhere near the file being tagged.
                  onTags(file, anchorFrom(triggerRef.current, '[data-file-row]', 'row'));
                  setOpen(false);
                }}
                disabled={!canModify('library', 'update', file.created_by_id)}
              >
                <TagIcon className="w-3.5 h-3.5" />
                {t('fileManager.tags.tagAction')}
              </button>
            )}
            {(file.file_type === 'stl' || file.file_type === 'obj') && (
              <button
                className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${canModify('library', 'update', file.created_by_id) ? 'text-white hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'}`}
                onClick={() => { if (canModify('library', 'update', file.created_by_id)) { onGenerateThumbnail(file); setOpen(false); } }}
                disabled={!canModify('library', 'update', file.created_by_id)}
              >
                <Image className="w-3.5 h-3.5" />
                {t('fileManager.generateThumbnail')}
              </button>
            )}
            <button
              className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${canModify('library', 'delete', file.created_by_id) ? 'text-red-700 dark:text-red-400 hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'}`}
              onClick={() => { if (canModify('library', 'delete', file.created_by_id)) { onDelete(file.id); setOpen(false); } }}
              disabled={!canModify('library', 'delete', file.created_by_id)}
            >
              <Trash2 className="w-3.5 h-3.5" />
              {t('common.delete')}
            </button>
          </div>
        </>,
        document.body
      )}
    </div>
  );
}

function FileCard({ file, isSelected, isMobile, onSelect, onOpenArchives, onDelete, onDownload, onAddToQueue, onPrint, onSlice, onOpenInSlicer, useSlicerApi, onPreview3d, onRename, onLink, onGenerateThumbnail, onPlateGallery, onMove, onTags, onTagClick, thumbnailVersion, isRegeneratingThumbnail, hasPermission, canModify, authEnabled, timeFormat, dateFormat, t }: FileCardProps) {
  // ⚠️ The two modes need different permissions: slicing through the sidecar
  // writes a new library file, while opening in a desktop slicer is a download.
  const sliceDisabled = useSlicerApi ? !hasPermission('library:upload') : !hasPermission('library:read');
  const [showActions, setShowActions] = useState(false);
  // Portal-rendered dropdown — the card root has `overflow-hidden` for the
  // thumbnail crop, which clips an absolute-positioned menu against the card
  // edge on narrow viewports. Coords are computed from the trigger button
  // and recalculated on scroll/resize to track the card's position.
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  // Anchor the menu's bottom edge to the trigger's top (default) so the gap
  // stays a fixed 4 px regardless of menu height. Flip to top-anchor when
  // there isn't enough room above (e.g. trigger near top of viewport).
  const [coords, setCoords] = useState<{ top?: number; bottom?: number; right: number } | null>(null);
  const [showPlateObjects, setShowPlateObjects] = useState(false);

  useEffect(() => {
    if (!showActions) return;
    const update = () => {
      const btn = triggerRef.current;
      if (!btn) return;
      const rect = btn.getBoundingClientRect();
      const right = Math.max(8, window.innerWidth - rect.right);
      // Default: anchor menu's bottom 4 px above the trigger — flush layout,
      // exact gap. Flip below when the trigger is near the top of the viewport.
      const minOpenAboveHeight = 120;
      if (rect.top > minOpenAboveHeight + 8) {
        setCoords({ bottom: window.innerHeight - rect.top + 4, right });
      } else {
        setCoords({ top: rect.bottom + 4, right });
      }
    };
    update();
    window.addEventListener('resize', update);
    window.addEventListener('scroll', update, true);
    return () => {
      window.removeEventListener('resize', update);
      window.removeEventListener('scroll', update, true);
    };
  }, [showActions]);

  return (
    <div
      data-file-card
      className={`group relative bg-bambu-dark-secondary rounded-lg border transition-all overflow-hidden ${
        isSelected
          ? 'border-bambu-green ring-1 ring-bambu-green'
          : 'border-bambu-dark-tertiary hover:border-bambu-green/50'
      }`}
    >
      {/* Thumbnail */}
      <div className="relative aspect-square bg-bambu-dark flex items-center justify-center overflow-hidden">
        {file.thumbnail_path ? (
          <img
            src={`${api.getLibraryFileThumbnailUrl(file.id)}${thumbnailVersion ? `?v=${thumbnailVersion}` : ''}`}
            alt={file.filename}
            className="w-full h-full object-contain"
          />
        ) : (
          <FileBox className="w-12 h-12 text-bambu-gray/30" />
        )}
        {/* Regen overlay — covers the thumbnail with a translucent backdrop
            + spinner so the operator gets visible feedback that the menu
            action took effect (without it the regen ran silently in the
            background). Render takes precedence over badges/buttons via
            z-30 so they're not click-target-able mid-regen. */}
        {isRegeneratingThumbnail && (
          <div className="absolute inset-0 z-30 flex flex-col items-center justify-center gap-2 bg-bambu-dark/70 backdrop-blur-sm pointer-events-none">
            <Loader2 className="w-6 h-6 text-bambu-green animate-spin" />
            <span className="text-xs text-white font-medium">
              {t('fileManager.regeneratingThumbnail', { defaultValue: 'Regenerating…' })}
            </span>
          </div>
        )}
        {/* Composite badge row — driven by ``file_tags`` (m036). The
            backend computes the list at every write site so this just
            renders. Provenance (MakerWorld) ships as the orange ``MW``
            chip inside FileTagBadges; the click-to-open-original action
            lives in the three-dots menu. */}
        <div className="absolute top-2 right-2 flex items-center gap-1">
          <FileTagBadges tags={file.file_tags} compact />
        </div>
        {/* Plate-gallery overlay — sits directly above the notes button.
            Only multi-plate sliced 3MFs render it; opens the modal handled
            by FileManagerPage so the same dialog instance is shared with
            the list-mode action button. */}
        {isMultiPlate(file) && onPlateGallery && (
          <div className="absolute bottom-8 left-2" onClick={(e) => e.stopPropagation()}>
            <div className="relative inline-block">
              <button
                onClick={() => onPlateGallery(file)}
                className="rounded-md bg-bambu-dark/80 backdrop-blur text-bambu-gray hover:text-bambu-green hover:bg-bambu-dark transition-colors flex items-center"
                title={t('fileManager.plateGallery')}
              >
                <Layers className="w-5 h-5" />
              </button>
            </div>
          </div>
        )}
        {/* Notes overlay - bottom-left corner */}
        <div className="absolute bottom-1 left-2" onClick={(e) => e.stopPropagation()}>
          <LibraryFileNotesButton fileId={file.id} initialCount={file.notes_count} variant="overlay" />
        </div>
        {/* Project link overlay - bottom-right, same height as notes */}
        {onLink && (
          <div className="absolute bottom-2 right-2" onClick={(e) => e.stopPropagation()}>
            {(file.project_ids ?? []).length > 0 ? (
              <button
                onClick={() => onLink(file)}
                className="rounded-md bg-blue-500/85 backdrop-blur text-white hover:bg-blue-500 transition-colors flex items-center gap-1 px-1.5 py-1"
                title={t('fileManager.linkedToNProjects', { count: file.project_ids.length })}
              >
                <Link2 className="w-5 h-5" />
                <Briefcase className="w-4 h-4" />
                {file.project_ids.length > 1 && (
                  <span className="text-[10px] font-semibold">×{file.project_ids.length}</span>
                )}
              </button>
            ) : canModify('library', 'update', file.created_by_id) ? (
              <button
                onClick={() => onLink(file)}
                className="rounded-md bg-bambu-dark/80 backdrop-blur text-bambu-gray hover:text-bambu-green hover:bg-bambu-dark transition-colors flex items-center p-1 opacity-0 group-hover:opacity-100"
                title={t('fileManager.linkToProject')}
              >
                <Link2 className="w-5 h-5" />
              </button>
            ) : null}
          </div>
        )}
      </div>

      {/* Info */}
      <div className="p-3">
        <h3 className="text-sm font-medium text-white truncate">
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); onOpenArchives(file); }}
            title={t('fileManager.viewPrintsOf', { name: file.print_name || file.filename })}
            className="block w-full truncate text-left hover:text-bambu-green hover:underline transition-colors cursor-pointer"
          >
            {file.print_name || file.filename}
          </button>
        </h3>
        <div className="flex items-center gap-3 mt-1 text-xs text-bambu-gray">
          <span>{formatFileSize(file.file_size)}</span>
          {file.print_time_seconds && (
            <span className="flex items-center gap-1">
              <Clock className="w-3 h-3" />
              {formatDuration(file.print_time_seconds)}
            </span>
          )}
          {file.filament_used_grams != null && file.filament_used_grams > 0 && (
            <span className="flex items-center gap-1">
              <Package className="w-3 h-3" />
              {file.filament_used_grams.toFixed(1)}g
            </span>
          )}
          {file.object_count != null && file.object_count > 0 && (
            <span className="flex items-center gap-1">
              {/* The count itself opens the preview — no extra icon button to
                  crowd the card. stopPropagation is load-bearing: the card
                  itself is a selection target. */}
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); setShowPlateObjects(true); }}
                className="flex items-center gap-1 hover:text-bambu-green transition-colors"
                title={t('library.plateObjects.open')}
              >
                <Box className="w-3 h-3" />
                {file.object_count}
              </button>
              {/* Icon-only: most sliced files support skipping, so a text badge
                  on every card would be noise. Absence is the signal. */}
              {file.skip_objects_supported && (
                <SkipObjectsIcon className="w-3 h-3 text-bambu-green/70" />
              )}
            </span>
          )}
          {/* How many times this file actually finished a print. The count
              itself is the button, exactly like the object count beside it,
              and it opens the same archive view the filename above already
              opens — this is the label that affordance never had. Nothing is
              drawn at zero: most of a library is unprinted, and a badge on
              nearly every card would be noise. */}
          {file.print_count > 0 && (
            <button
              type="button"
              onClick={(e) => { e.stopPropagation(); onOpenArchives(file); }}
              aria-label={t('fileManager.printedTimes', { count: file.print_count })}
              title={t('fileManager.printedTimes', { count: file.print_count })}
              className="flex items-center gap-1 hover:text-bambu-green transition-colors"
            >
              <History className="w-3 h-3" />
              {file.print_count}
            </button>
          )}
        </div>
        {file.sliced_for_model && (
          <div className="mt-1 text-xs text-bambu-gray flex items-center gap-1">
            <Printer className="w-3 h-3" />
            {file.sliced_for_model}
          </div>
        )}
        <div className="mt-1 text-xs text-bambu-gray truncate">
          {formatDateTime(fileActivityAt(file), timeFormat, dateFormat)}
        </div>
        {authEnabled && file.created_by_username && (
          <div className="mt-0.5 text-xs text-bambu-gray flex items-center gap-1">
            <User className="w-3 h-3" />
            {file.created_by_username}
          </div>
        )}
        {/* #1268 — user-authored tag chips (green). DISTINCT from the
            computed FileTagBadges overlay on the thumbnail. Click toggles
            the tag in the cross-cutting filter rail; stopPropagation keeps
            the click off card selection. */}
        {(file.tags ?? []).length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1" onClick={(e) => e.stopPropagation()}>
            {(file.tags ?? []).map((tag) => (
              <button
                key={tag.id}
                type="button"
                onClick={() => onTagClick?.(tag.id)}
                className="text-[11px] px-1.5 py-0.5 rounded-full bg-bambu-green/15 text-bambu-green border border-bambu-green/30 hover:bg-bambu-green/25 transition-colors max-w-full truncate"
                title={tag.name}
              >
                {tag.name}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Actions - always visible on mobile, hover on desktop */}
      <div className={`absolute bottom-2 right-2 transition-opacity ${isMobile ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'}`} onClick={(e) => e.stopPropagation()}>
        <button
          ref={triggerRef}
          onClick={() => setShowActions(!showActions)}
          aria-label={t('fileManager.fileActions')}
          className="p-1.5 rounded bg-bambu-dark-secondary/90 hover:bg-bambu-dark-tertiary"
        >
          <MoreVertical className="w-4 h-4 text-bambu-gray" />
        </button>
        {showActions && createPortal(
          <>
            <div className="fixed inset-0 z-[55]" onClick={() => setShowActions(false)} />
            <div
              style={{
                position: 'fixed',
                top: coords?.top,
                bottom: coords?.bottom,
                right: coords?.right ?? 0,
                visibility: coords ? 'visible' : 'hidden',
              }}
              className="z-[60] bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl py-1 whitespace-nowrap w-max max-w-[calc(100vw-16px)]"
              onClick={(e) => e.stopPropagation()}
            >
              {onPrint && isPrintable(file) && (
                <button
                  className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${
                    hasPermission('printers:control') ? 'text-bambu-green hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'
                  }`}
                  onClick={() => { if (hasPermission('printers:control')) { onPrint(file); setShowActions(false); } }}
                  disabled={!hasPermission('printers:control')}
                  title={!hasPermission('printers:control') ? t('fileManager.noPermissionPrint') : undefined}
                >
                  <Printer className="w-3.5 h-3.5" />
                  {t('common.print')}
                </button>
              )}
              {onAddToQueue && isPrintable(file) && (
                <button
                  className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${
                    hasPermission('queue:create') ? 'text-white hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'
                  }`}
                  onClick={() => { if (hasPermission('queue:create')) { onAddToQueue(file.id); setShowActions(false); } }}
                  disabled={!hasPermission('queue:create')}
                  title={!hasPermission('queue:create') ? t('fileManager.noPermissionAddToQueue') : undefined}
                >
                  <Clock className="w-3.5 h-3.5" />
                  {t('fileManager.schedulePrint')}
                </button>
              )}
              {/* See the note on the sibling menu above: not gated on the
                  sidecar, and the two modes need different permissions. */}
              {isSliceable(file) && (onSlice || onOpenInSlicer) && (
                <button
                  className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${
                    !sliceDisabled ? 'text-white hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'
                  }`}
                  onClick={() => {
                    if (sliceDisabled) return;
                    if (useSlicerApi) onSlice?.(file);
                    else onOpenInSlicer?.(file);
                    setShowActions(false);
                  }}
                  disabled={sliceDisabled}
                  title={sliceDisabled ? (useSlicerApi ? t('fileManager.noPermissionSlice') : t('fileManager.noPermissionDownload')) : undefined}
                >
                  {useSlicerApi ? <Cog className="w-3.5 h-3.5" /> : <ExternalLink className="w-3.5 h-3.5" />}
                  {useSlicerApi ? t('slice.action') : t('modelViewer.openInSlicer')}
                </button>
              )}
              {onPreview3d && (file.file_type === '3mf' || file.file_type === 'gcode' || file.file_type === 'stl' || file.file_type === 'obj') && (
                <button
                  className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${
                    hasPermission('library:read') ? 'text-white hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'
                  }`}
                  onClick={() => { if (hasPermission('library:read')) { onPreview3d(file); setShowActions(false); } }}
                  disabled={!hasPermission('library:read')}
                  title={!hasPermission('library:read') ? t('fileManager.noPermissionPreview', { defaultValue: 'You do not have permission to preview files' }) : undefined}
                >
                  <Box className="w-3.5 h-3.5" />
                  {t('fileManagerModal.threeView')}
                </button>
              )}
              {file.source_type === 'makerworld' && file.source_url && (
                <a
                  href={file.source_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={() => setShowActions(false)}
                  className="w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 text-white hover:bg-bambu-dark"
                >
                  <MakerWorldIcon className="w-3.5 h-3.5 text-white" />
                  {t('fileManager.source.openOriginal', { defaultValue: 'Open on MakerWorld' })}
                </a>
              )}
              <button
                className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${
                  hasPermission('library:read') ? 'text-white hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'
                }`}
                onClick={() => { if (hasPermission('library:read')) { onDownload(file.id); setShowActions(false); } }}
                disabled={!hasPermission('library:read')}
                title={!hasPermission('library:read') ? t('fileManager.noPermissionDownload') : undefined}
              >
                <Download className="w-3.5 h-3.5" />
                {t('common.download')}
              </button>
              {onRename && (
                <button
                  className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${
                    canModify('library', 'update', file.created_by_id) ? 'text-white hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'
                  }`}
                  onClick={() => { if (canModify('library', 'update', file.created_by_id)) { onRename(file); setShowActions(false); } }}
                  disabled={!canModify('library', 'update', file.created_by_id)}
                  title={!canModify('library', 'update', file.created_by_id) ? t('fileManager.noPermissionRenameFile') : undefined}
                >
                  <Pencil className="w-3.5 h-3.5" />
                  {t('common.rename')}
                </button>
              )}
              {/* Move and Tags — same pair as the list menu, same gate. */}
              {onMove && (
                <button
                  className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${
                    canModify('library', 'update', file.created_by_id) ? 'text-white hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'
                  }`}
                  onClick={() => { if (canModify('library', 'update', file.created_by_id)) { onMove(file); setShowActions(false); } }}
                  disabled={!canModify('library', 'update', file.created_by_id)}
                >
                  <MoveRight className="w-3.5 h-3.5" />
                  {t('common.move')}
                </button>
              )}
              {onTags && (
                <button
                  className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${
                    canModify('library', 'update', file.created_by_id) ? 'text-white hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'
                  }`}
                  onClick={() => {
                    if (!canModify('library', 'update', file.created_by_id)) return;
                    onTags(file, anchorFrom(triggerRef.current, '[data-file-card]', 'card'));
                    setShowActions(false);
                  }}
                  disabled={!canModify('library', 'update', file.created_by_id)}
                >
                  <TagIcon className="w-3.5 h-3.5" />
                  {t('fileManager.tags.tagAction')}
                </button>
              )}
              {onGenerateThumbnail && (file.file_type === 'stl' || file.file_type === 'obj') && (
                <button
                  className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${
                    canModify('library', 'update', file.created_by_id) ? 'text-white hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'
                  }`}
                  onClick={() => { if (canModify('library', 'update', file.created_by_id)) { onGenerateThumbnail(file); setShowActions(false); } }}
                  disabled={!canModify('library', 'update', file.created_by_id)}
                  title={!canModify('library', 'update', file.created_by_id) ? t('fileManager.noPermissionGenerateThumbnail') : undefined}
                >
                  <Image className="w-3.5 h-3.5" />
                  {t('fileManager.generateThumbnail')}
                </button>
              )}
              <button
                className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${
                  canModify('library', 'delete', file.created_by_id) ? 'text-red-700 dark:text-red-400 hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'
                }`}
                onClick={() => { if (canModify('library', 'delete', file.created_by_id)) { onDelete(file.id); setShowActions(false); } }}
                disabled={!canModify('library', 'delete', file.created_by_id)}
                title={!canModify('library', 'delete', file.created_by_id) ? t('fileManager.noPermissionDeleteFile') : undefined}
              >
                <Trash2 className="w-3.5 h-3.5" />
                {t('common.delete')}
              </button>
            </div>
          </>,
          document.body
        )}
      </div>

      {/* Selection checkbox - the only select affordance (a plain card click
          no longer toggles selection). Always visible on mobile, hover on
          desktop. stopPropagation keeps the click off the card body. */}
      <button
        type="button"
        data-select-file
        onClick={(e) => { e.stopPropagation(); onSelect(file.id); }}
        aria-pressed={isSelected}
        aria-label={t('fileManager.selectFile', { defaultValue: 'Select file' })}
        className={`absolute top-2 left-2 w-5 h-5 rounded border-2 flex items-center justify-center transition-all cursor-pointer ${
          isSelected
            ? 'bg-bambu-green border-bambu-green'
            : `border-white/30 bg-black/30 ${isMobile ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'}`
        }`}
      >
        {isSelected && <div className="w-2 h-2 bg-white rounded-sm" />}
      </button>
      {/* Sibling of the card body, NOT of the hover-revealed action cluster:
          that wrapper is `opacity-0 group-hover:opacity-100`, so a modal nested
          inside it would vanish the moment the pointer left the card. */}
      {showPlateObjects && (
        <PlateObjectsPreviewModal
          source="library"
          id={file.id}
          isOpen
          onClose={() => setShowPlateObjects(false)}
        />
      )}
    </div>
  );
}

export function FileManagerPage() {
  const [previewFileId, setPreviewFileId] = useState<number | null>(null);
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { hasPermission, hasAnyPermission, canModify, authEnabled } = useAuth();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();

  // Read folder ID from URL query parameter
  const folderIdFromUrl = searchParams.get('folder');
  const initialFolderId = folderIdFromUrl ? parseInt(folderIdFromUrl, 10) : null;

  // State
  const [selectedFolderId, setSelectedFolderId] = useState<number | null>(initialFolderId);
  // Which top-level pseudo-view the sidebar shows when no specific folder is
  // selected: "internal" = files in BamDude's managed storage, "external" =
  // combined view across every linked external folder (#1621). Per-folder
  // selection bypasses this (selectedFolderId !== null disables the filter).
  const [topLevelView, setTopLevelView] = useState<'internal' | 'external'>('internal');
  const [selectedFiles, setSelectedFiles] = useState<number[]>([]);
  const [showNewFolderModal, setShowNewFolderModal] = useState(false);
  const [showExternalFolderModal, setShowExternalFolderModal] = useState(false);
  const [showMoveModal, setShowMoveModal] = useState(false);
  // Single-file Move and Tags, reachable from the ⋮ menu without first ticking
  // a checkbox. `moveFile` reuses MoveFilesModal with a one-id list.
  const [moveFile, setMoveFile] = useState<LibraryFileListItem | null>(null);
  const [tagsPopover, setTagsPopover] = useState<{ file: LibraryFileListItem; anchor: TagsPopoverAnchor } | null>(null);
  const [showUploadModal, setShowUploadModal] = useState(false);
  const [droppedFiles, setDroppedFiles] = useState<File[]>([]);
  const [isPageDragging, setIsPageDragging] = useState(false);
  const dragCounterRef = useRef(0);
  const [showPurgeModal, setShowPurgeModal] = useState(false);
  const [linkFolder, setLinkFolder] = useState<LibraryFolderTree | null>(null);
  const [linkFile, setLinkFile] = useState<LibraryFileListItem | null>(null);
  const [deleteConfirm, setDeleteConfirm] = useState<{ type: 'file' | 'folder' | 'bulk'; id: number; count?: number } | null>(null);
  const [printFile, setPrintFile] = useState<LibraryFileListItem | null>(null);
  const [printMultiFile, setPrintMultiFile] = useState<LibraryFileListItem | null>(null);
  // The files still to be scheduled, in the order they were selected. One entry
  // is an ordinary Schedule-print open; several is a run through the same
  // dialog, one file at a time (QueueSequencer). `fromSelection` says whether
  // the run may write back to the selection when it ends — a run started from
  // one file's ⋮ menu must not touch what happens to be ticked.
  const [queueSequence, setQueueSequence] = useState<
    { files: LibraryFileListItem[]; fromSelection: boolean } | null
  >(null);
  const [sliceFile, setSliceFile] = useState<LibraryFileListItem | null>(null);
  const [renameItem, setRenameItem] = useState<{ type: 'file' | 'folder'; id: number; name: string } | null>(null);
  const [thumbnailVersions, setThumbnailVersions] = useState<Record<number, number>>({});
  const [viewerFile, setViewerFile] = useState<LibraryFileListItem | null>(null);
  // Per-plate gallery modal — opened from list-mode "plates" button. Null when closed.
  const [galleryFile, setGalleryFile] = useState<LibraryFileListItem | null>(null);

  // #1268 — user-authored tags (SYSTEM C). Completely separate from the
  // computed-tag chip-row `filterTags` (SYSTEM B) above: this drives a
  // server-side cross-cutting AND filter, a catalog CRUD modal, and a
  // bulk-tag picker. `selectedTagIds` are tag row ids, not tag strings.
  const [showTagsModal, setShowTagsModal] = useState(false);
  const [showBulkTagsModal, setShowBulkTagsModal] = useState(false);
  const [selectedTagIds, setSelectedTagIds] = useState<number[]>([]);

  const [viewMode, setViewMode] = useState<'grid' | 'list'>(() => {
    return (localStorage.getItem('library-view-mode') as 'grid' | 'list') || 'grid';
  });
  const [wrapFolderNames, setWrapFolderNames] = useState(() => {
    return localStorage.getItem('library-wrap-folders') === 'true';
  });
  const [collapseFoldersByDefault, setCollapseFoldersByDefault] = useState(() => {
    return localStorage.getItem('library-collapse-folders') === 'true';
  });
  // Folder tree sort (#1770). 'activity' = most recent file activity inside
  // the folder first. Persisted independently from the file-side sort so each
  // can be tuned to taste.
  const [folderSortField, setFolderSortField] = useState<'name' | 'activity'>(() =>
    localStorage.getItem('library-folder-sort-field') === 'activity' ? 'activity' : 'name',
  );
  const [folderSortDirection, setFolderSortDirection] = useState<'asc' | 'desc'>(() =>
    localStorage.getItem('library-folder-sort-direction') === 'desc' ? 'desc' : 'asc',
  );

  // Resizable sidebar state
  const [sidebarWidth, setSidebarWidth] = useState(() => {
    const saved = localStorage.getItem('library-sidebar-width');
    return saved ? parseInt(saved, 10) : 256; // Default w-64 = 256px
  });
  const [isResizing, setIsResizing] = useState(false);
  const sidebarRef = useRef<HTMLDivElement>(null);

  // Handle sidebar resize
  useEffect(() => {
    if (!isResizing) return;

    // Prevent text selection during resize
    document.body.style.userSelect = 'none';
    document.body.style.cursor = 'col-resize';

    const handleMouseMove = (e: MouseEvent) => {
      if (!sidebarRef.current) return;
      const containerRect = sidebarRef.current.parentElement?.getBoundingClientRect();
      if (!containerRect) return;
      // Calculate new width based on mouse position relative to container
      const newWidth = e.clientX - containerRect.left;
      // Clamp between 200px and 500px
      const clampedWidth = Math.min(500, Math.max(200, newWidth));
      setSidebarWidth(clampedWidth);
    };

    const handleMouseUp = () => {
      setIsResizing(false);
      document.body.style.userSelect = '';
      document.body.style.cursor = '';
      // Save to localStorage
      localStorage.setItem('library-sidebar-width', String(sidebarWidth));
    };

    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);

    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
      document.body.style.userSelect = '';
      document.body.style.cursor = '';
    };
  }, [isResizing, sidebarWidth]);

  // Filter and sort state (persist sort preferences to localStorage)
  const [searchQuery, setSearchQuery] = useState('');
  // The search value now goes to the server as `q` (task 1) — debounced so
  // an undebounced value doesn't fire one request per keystroke.
  const debouncedSearchQuery = useDebouncedValue(searchQuery, 300);
  const [filterType, setFilterType] = useState<string>('all');
  // Deliberately NOT persisted, unlike the grid/list view mode: this is a
  // question, not a preference. Restored silently it would show a partial
  // library on the next visit with nothing on screen explaining why.
  const [unprintedOnly, setUnprintedOnly] = useState(false);
  // `filterTags` used to live here: a client-side AND-predicate over the
  // computed badges, persisted to localStorage. Both kinds of tag are rows in
  // one catalog since m128, so `selectedTagIds` below now carries them all and
  // the server does the filtering. The stale `library-filter-tags` key is left
  // alone rather than deleted on startup — it is inert once nothing reads it,
  // and reaching into a user's browser storage to tidy up is a bigger action
  // than the tidiness is worth.
  const [filterUsername, setFilterUsername] = useState('');
  // Free-text, same reasoning (and the same 300ms) as the search box above.
  const debouncedFilterUsername = useDebouncedValue(filterUsername, 300);
  const [sortField, setSortField] = useState<SortField>(() => {
    const saved = localStorage.getItem('library-sort-field');
    return (saved as SortField) || 'name';
  });
  const [sortDirection, setSortDirection] = useState<SortDirection>(() => {
    const saved = localStorage.getItem('library-sort-direction');
    return (saved as SortDirection) || 'asc';
  });
  // Paging (task 2, 2026-08-29 server-driven-lists) — same PaginationBar the
  // Archives list uses. `-1` means "all" (PaginationBar's own convention).
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(() => {
    const saved = localStorage.getItem('library-per-page');
    return saved ? Number(saved) : 50;
  });
  useEffect(() => {
    localStorage.setItem('library-per-page', String(perPage));
  }, [perPage]);

  // Mobile detection for touch-friendly UI
  const isMobile = useIsMobile();

  // Update selectedFolderId when URL parameter changes (e.g., navigating from Project or Archive page)
  useEffect(() => {
    const folderParam = searchParams.get('folder');
    if (folderParam) {
      const newFolderId = parseInt(folderParam, 10);
      setSelectedFolderId(newFolderId);
    }
  }, [searchParams]);

  // Queries
  const { data: settings } = useQuery({
    queryKey: ['settings'],
    queryFn: () => api.getSettings() as Promise<AppSettings>,
  });
  const timeFormat: TimeFormat = settings?.time_format || 'system';
  const dateFormat: DateFormat = settings?.date_format || 'system';

  // Hand a library file to the operator's own desktop slicer. ⚠️ The token URL
  // first so the file is reachable with auth on; the plain download URL is the
  // fallback for an install with auth disabled, where minting one would 404.
  const preferredSlicer: SlicerType = settings?.open_in_slicer || settings?.preferred_slicer || 'bambu_studio';
  const handleOpenInSlicer = useCallback(
    async (file: LibraryFileListItem) => {
      try {
        const { token } = await api.createLibrarySlicerToken(file.id);
        const path = api.getLibrarySlicerDownloadUrl(file.id, token, file.filename);
        openInSlicer(`${window.location.origin}${path}`, preferredSlicer);
      } catch {
        const path = api.getLibraryFileDownloadUrl(file.id);
        openInSlicer(`${window.location.origin}${path}`, preferredSlicer);
      }
    },
    [preferredSlicer],
  );
  const { data: folders, isLoading: foldersLoading } = useQuery({
    queryKey: ['library-folders'],
    queryFn: () => api.getLibraryFolders(),
  });

  // Recursive folder tree sort (#1770). Applies the same comparator to the
  // top-level list AND to each level of `children`, so sort order is uniform
  // at every depth of nesting. When sorting by activity, folders with no file
  // activity (`latest_activity_at` is null) fall back to name and are pushed
  // to the end so an empty folder doesn't elbow a recently-used one.
  const sortedFolders = useMemo(() => {
    if (!folders) return folders;
    const sortLevel = (items: LibraryFolderTree[]): LibraryFolderTree[] => {
      const sorted = [...items].sort((a, b) => {
        let comparison = 0;
        if (folderSortField === 'name') {
          comparison = a.name.localeCompare(b.name);
        } else {
          const aTs = a.latest_activity_at ? new Date(a.latest_activity_at).getTime() : null;
          const bTs = b.latest_activity_at ? new Date(b.latest_activity_at).getTime() : null;
          if (aTs === null && bTs === null) {
            comparison = a.name.localeCompare(b.name);
          } else if (aTs === null) {
            return 1;
          } else if (bTs === null) {
            return -1;
          } else {
            comparison = aTs - bTs;
          }
        }
        return folderSortDirection === 'asc' ? comparison : -comparison;
      });
      return sorted.map((f) => ({ ...f, children: sortLevel(f.children) }));
    };
    return sortLevel(folders);
  }, [folders, folderSortField, folderSortDirection]);

  // #1621 promised "external folders are surfaced separately below" but the
  // tree rendered one interleaved alphabetical list, so a linked NAS root sat
  // in the middle of the user's own folders — and everything after the
  // "External" header read as its child. Split at ROOT level only: an
  // external living deeper in the tree belongs to its parent and stays put.
  const ownRootFolders = useMemo(() => sortedFolders?.filter((f) => !f.is_external), [sortedFolders]);
  const externalRootFolders = useMemo(() => sortedFolders?.filter((f) => f.is_external), [sortedFolders]);

  // Trash count for the header badge (#1008). Empty/error treated as zero so a
  // broken trash endpoint doesn't break the File Manager.
  const { data: trashCount } = useQuery({
    queryKey: ['library-trash-count'],
    queryFn: async () => {
      try {
        const res = await api.listLibraryTrash(1, 0);
        return res.total;
      } catch {
        return 0;
      }
    },
    staleTime: 30_000,
  });

  // #1268 — user-tag catalog. Loaded once; drives the filter rail, the
  // per-file chip labels, and the two tag modals. `tagFilterKey` is the
  // sorted id list so the library-files query cache is stable regardless
  // of the order tags were toggled in.
  const tagFilterKey = useMemo(() => [...selectedTagIds].sort((a, b) => a - b), [selectedTagIds]);
  const { data: tagCatalog = [] } = useQuery({
    queryKey: libraryTagsQueryKey,
    queryFn: api.getLibraryTags,
  });
  // The catalog carries BOTH kinds since m128, and the filter row shows both.
  //
  // A system tag nobody's library uses is noise, so it is offered only when
  // something carries it. The count is GLOBAL, from the catalog — the row this
  // replaced derived it from the loaded page, so pills vanished as you
  // narrowed, which made switching from one to another impossible: the second
  // was already off the screen.
  const systemTagPills = useMemo(
    () => tagCatalog.filter((tag) => tag.is_system && tag.file_count > 0),
    [tagCatalog],
  );
  // User tags are offered always. Asymmetric on purpose: somebody created that
  // one deliberately, and an empty tag is precisely the one worth seeing.
  const userTagPills = useMemo(() => tagCatalog.filter((tag) => !tag.is_system), [tagCatalog]);
  const tagsById = useMemo(() => {
    const m = new Map<number, string>();
    for (const tag of tagCatalog) m.set(tag.id, tag.name);
    return m;
  }, [tagCatalog]);
  // Prune selected tag ids that no longer exist (a tag was deleted from the
  // catalog) so the filter never strands on a phantom id.
  useEffect(() => {
    if (tagCatalog.length === 0) return;
    setSelectedTagIds((prev) => {
      const next = prev.filter((id) => tagsById.has(id));
      return next.length === prev.length ? prev : next;
    });
  }, [tagCatalog.length, tagsById]);
  const toggleTagFilter = useCallback((tagId: number) => {
    setSelectedTagIds((prev) =>
      prev.includes(tagId) ? prev.filter((id) => id !== tagId) : [...prev, tagId],
    );
  }, []);

  // Is anything narrowing the library right now? Five independent filters, two
  // of which are easy to forget: the computed-tag chip row survives reloads in
  // localStorage, and the user-tag filter is applied SERVER-side, so it empties
  // the listing itself rather than the client-side view of it.
  const anyFilterActive =
    searchQuery.trim() !== '' ||
    filterType !== 'all' ||
    unprintedOnly ||
    filterUsername.trim() !== '' ||
    selectedTagIds.length > 0;

  /**
   * One definition of what "clear filters" means, because the button used to
   * carry its own partial one inline — search and type only — and quietly left
   * three filters running. Four filters now rather than five: the computed-tag
   * predicate folded into `selectedTagIds` when both kinds of tag became rows
   * in one catalog.
   */
  const clearAllFilters = useCallback(() => {
    setSearchQuery('');
    setFilterType('all');
    setUnprintedOnly(false);
    setFilterUsername('');
    setSelectedTagIds([]);
  }, []);

  const allFilesRecursive = settings?.library_all_files_recursive ?? false;
  // #1268: when a folder is selected and the user has typed a search query,
  // ask the server to expand the result to every descendant folder so the
  // search can match files in subfolders too. Without this the listing is
  // just the immediate children and "robot.3mf" two levels deep is invisible
  // from the parent. Only kicks in for folder-scoped views — root and the
  // internal/external pseudo-nodes already return the union. Keyed off the
  // DEBOUNCED query (not the raw keystroke value): both this flag and `q`
  // below drive the same request, and letting one fire on every keystroke
  // while the other waits out the debounce would refetch twice for one type.
  const searchExpandsSubfolders = selectedFolderId !== null && debouncedSearchQuery.trim().length > 0;

  // Any filter/sort/scope change invalidates the current page — staying on
  // page 4 of a narrower result is a page nobody asked for. Resetting this in
  // a `useEffect` lands one render late: the query would fire once with
  // {newFilter, oldPage} on the render the filter itself changed, then a
  // SECOND time after the effect's `setPage(1)` commits. Adjusting `page`
  // right here during render (React's documented "you might not need an
  // effect" idiom) means the mismatched render is thrown away before it
  // commits — its `useQuery` never gets a chance to fetch — so the query
  // only ever sees the correct, already-reset combination.
  const pageResetSignature = JSON.stringify([
    selectedFolderId,
    topLevelView,
    tagFilterKey,
    debouncedSearchQuery,
    filterType,
    unprintedOnly,
    debouncedFilterUsername,
    sortField,
    sortDirection,
  ]);
  const [prevPageResetSignature, setPrevPageResetSignature] = useState(pageResetSignature);
  let effectivePage = page;
  if (pageResetSignature !== prevPageResetSignature) {
    setPrevPageResetSignature(pageResetSignature);
    setPage(1);
    effectivePage = 1;
  }

  // Server-driven (task 2, 2026-08-29 server-driven-lists) — every filter,
  // the sort and the page all become request params; task 1's envelope
  // ({items, meta}) replaces the flat array this used to fetch, so there is
  // no client-side filter/sort pass left to run over the result.
  const libraryFileParams: LibraryFileListParams = {
    folder_id: selectedFolderId,
    // "All Files" (selectedFolderId === null): include_root=false lists every
    // file across all subfolders recursively (#1499), include_root=true
    // scopes to root-level files only. Gated on the library_all_files_recursive
    // setting (default off → root-only, the pre-#1499 behaviour). When a
    // specific folder is selected the backend ignores include_root.
    include_root: selectedFolderId === null ? !allFilesRecursive : true,
    // At the top level, topLevelView scopes the result to internal managed
    // storage vs the union of every external folder (#1621); per-folder
    // selection passes no scope.
    scope: selectedFolderId === null ? topLevelView : undefined,
    // #1268: a non-empty tag_ids makes the backend bypass folder/root scoping
    // entirely (tags are cross-cutting).
    tag_ids: tagFilterKey,
    recursive: searchExpandsSubfolders,
    q: debouncedSearchQuery.trim() || undefined,
    file_type: filterType !== 'all' ? filterType : undefined,
    unprinted_only: unprintedOnly,
    username: debouncedFilterUsername.trim() || undefined,
    sort_by: `${sortField}_${sortDirection}`,
    page: effectivePage,
    per_page: perPage === -1 ? undefined : perPage,
    all: perPage === -1 ? true : undefined,
  };
  const { data: filesPage, isLoading: filesLoading } = useQuery({
    queryKey: ['library-files', libraryFileParams],
    queryFn: () => api.getLibraryFilesPaged(libraryFileParams),
    placeholderData: (prev) => prev,
  });
  const files = filesPage?.items;
  const meta = filesPage?.meta;

  const { data: stats } = useQuery({
    queryKey: ['library-stats'],
    queryFn: () => api.getLibraryStats(),
  });

  // Get users for the username filter autocomplete. The slim listing, not the
  // full one: only the name is rendered, and the full listing is admin-gated —
  // an operator who may filter by user but not administer them got an empty
  // datalist with no indication why.
  const { data: users } = useQuery({
    queryKey: ['users-slim'],
    queryFn: api.getUsersSlim,
  });

  // File types for the filter dropdown — the static closed set (see
  // LIBRARY_FILE_TYPES above), not derived from `files`. A per-page
  // derivation only ever offered the types present on the current
  // server-filtered/paginated result, so a type filtered or paged out of
  // view silently vanished from its own dropdown.
  const fileTypes = LIBRARY_FILE_TYPES;

  // Filtering, sorting and paging all happen server-side now (task 1 + this
  // task's params above) — this is a pass-through so every render call site
  // below keeps reading `filteredAndSortedFiles` unmodified.
  const filteredAndSortedFiles = useMemo(() => files ?? [], [files]);

  // Check if disk space is low
  const isDiskSpaceLow = useMemo(() => {
    if (!stats || !settings) return false;
    const thresholdBytes = (settings.library_disk_warning_gb || 5) * 1024 * 1024 * 1024;
    return stats.disk_free_bytes < thresholdBytes;
  }, [stats, settings]);

  // An external scan is a background job; these are its numbers as they land.
  const handleScanFinished = useCallback(
    (_folderId: number, state: LibraryScanState) => {
      if (state.status === 'failed') {
        showToast(t('fileManager.toast.scanFailed', { error: state.error || '' }), 'error');
        return;
      }
      if (state.skippedDeletions) {
        // Deliberately not a success toast. Nothing was deleted, and the reason
        // is one the operator has to act on — the strip keeps saying it.
        showToast(t('fileManager.toast.scanSkippedDeletions'), 'warning');
        return;
      }
      showToast(t('fileManager.toast.folderScanned', { added: state.added, removed: state.removed }), 'success');
    },
    [showToast, t]
  );
  const { scans, markStarted: markScanStarted, dismiss: dismissScan } = useLibraryScanProgress(handleScanFinished);
  const activeScan = selectedFolderId ? scans[selectedFolderId] : undefined;

  // Mutations
  const createFolderMutation = useMutation({
    mutationFn: (data: LibraryFolderCreate) => api.createLibraryFolder(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['library-folders'] });
      setShowNewFolderModal(false);
      showToast(t('fileManager.toast.folderCreated'), 'success');
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const createExternalFolderMutation = useMutation({
    mutationFn: async (data: ExternalFolderCreate) => {
      const folder = await api.createExternalFolder(data);
      // Auto-scan after creation. Returns as soon as the job exists — a share
      // with thousands of files no longer holds this dialog open.
      const job = await api.scanExternalFolder(folder.id);
      return { folder, job };
    },
    onSuccess: ({ folder, job }) => {
      queryClient.invalidateQueries({ queryKey: ['library-folders'] });
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      queryClient.invalidateQueries({ queryKey: ['library-stats'] });
      setShowExternalFolderModal(false);
      setSelectedFolderId(folder.id);
      markScanStarted(folder.id, job.job_id);
      showToast(t('fileManager.toast.externalFolderLinked'), 'success');
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  // ⚠️ Starting a scan is all this does now. It used to wait for the counts,
  // which is why a NAS share could hold the request — and SQLite's write lock —
  // for minutes. The counts arrive on the socket; see useLibraryScanProgress.
  const scanExternalFolderMutation = useMutation({
    mutationFn: (folderId: number) => api.scanExternalFolder(folderId),
    onSuccess: (result, folderId) => {
      markScanStarted(folderId, result.job_id);
      showToast(t('fileManager.toast.scanStarted'), 'info');
    },
    onError: (error: Error) => {
      // A scan of this folder is already running — a fact, not a failure. The
      // usual way to see one is reloading the tab mid-walk, when the strip that
      // was following it is gone but the walk is not.
      const already = error instanceof ApiError && error.status === 409;
      showToast(already ? t('fileManager.toast.scanAlreadyRunning') : error.message, already ? 'info' : 'error');
    },
  });

  const deleteFolderMutation = useMutation({
    mutationFn: (id: number) => api.deleteLibraryFolder(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['library-folders'] });
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      queryClient.invalidateQueries({ queryKey: ['library-stats'] });
      if (selectedFolderId === deleteConfirm?.id) {
        setSelectedFolderId(null);
      }
      setDeleteConfirm(null);
      showToast(t('fileManager.toast.folderDeleted'), 'success');
    },
    onError: (error: Error) => {
      setDeleteConfirm(null);
      showToast(error.message, 'error');
    },
  });

  const deleteFileMutation = useMutation({
    mutationFn: (id: number) => api.deleteLibraryFile(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      queryClient.invalidateQueries({ queryKey: ['library-folders'] });
      queryClient.invalidateQueries({ queryKey: ['library-stats'] });
      // Soft-delete moves the row into the trash table — refresh both the
      // header badge counter and the trash list so navigating to the
      // trash page picks the new row up immediately. Without this the
      // global 60s staleTime on QueryClient (App.tsx) keeps the trash
      // queries on a stale snapshot that pre-dates this delete.
      queryClient.invalidateQueries({ queryKey: ['library-trash'] });
      queryClient.invalidateQueries({ queryKey: ['library-trash-count'] });
      setSelectedFiles((prev) => prev.filter((id) => id !== deleteConfirm?.id));
      setDeleteConfirm(null);
      showToast(t('fileManager.toast.fileDeleted'), 'success');
    },
    onError: (error: Error) => {
      setDeleteConfirm(null);
      showToast(error.message, 'error');
    },
  });

  const bulkDeleteMutation = useMutation({
    mutationFn: (fileIds: number[]) => api.bulkDeleteLibrary(fileIds, []),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      queryClient.invalidateQueries({ queryKey: ['library-folders'] });
      queryClient.invalidateQueries({ queryKey: ['library-stats'] });
      queryClient.invalidateQueries({ queryKey: ['library-trash'] });
      queryClient.invalidateQueries({ queryKey: ['library-trash-count'] });
      // What happened, not what was asked for. A file whose queue item is
      // mid-print is skipped by the backend, and counting the request reported
      // it as deleted.
      showToast(
        result.skipped_files
          ? t('fileManager.toast.filesDeletedWithSkipped', {
              count: result.deleted_files,
              skipped: result.skipped_files,
            })
          : t('fileManager.toast.filesDeleted', { count: result.deleted_files }),
        'success',
      );
      setSelectedFiles([]);
      setDeleteConfirm(null);
    },
    onError: (error: Error) => {
      setDeleteConfirm(null);
      showToast(error.message, 'error');
    },
  });

  const moveFilesMutation = useMutation({
    mutationFn: ({ fileIds, folderId }: { fileIds: number[]; folderId: number | null }) =>
      api.moveLibraryFiles(fileIds, folderId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      queryClient.invalidateQueries({ queryKey: ['library-folders'] });
      setSelectedFiles([]);
      setShowMoveModal(false);
      showToast(t('fileManager.toast.filesMoved'), 'success');
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const updateFolderMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: LibraryFolderUpdate }) =>
      api.updateLibraryFolder(id, data),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['library-folders'] });
      // Invalidate project folder queries so other pages see the update
      queryClient.invalidateQueries({ queryKey: ['project-folders'] });
      // Folder→project link rewires every child file's project list AND the
      // affected projects' print plans (server-side `sync_plan_for_folder`
      // plants/drops plan rows for every eligible file in the folder). The
      // file browser pulls `library-files` to render the file column and
      // each project view pulls `project-print-plan`; both must be refreshed
      // so the linked files/projects show the new state without a manual
      // reload. `library-stats` carries the per-project file count too.
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      queryClient.invalidateQueries({ queryKey: ['library-stats'] });
      queryClient.invalidateQueries({ queryKey: ['project-print-plan'] });
      setLinkFolder(null);
      // m044: project_ids is an array; an empty list is a full unlink,
      // otherwise it's a link/update.
      const isUnlink =
        Array.isArray(variables.data.project_ids) && variables.data.project_ids.length === 0;
      showToast(isUnlink ? t('fileManager.toast.folderUnlinked') : t('fileManager.toast.folderLinked'), 'success');
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const linkFileMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: LibraryFileUpdate }) =>
      api.updateLibraryFile(id, data),
    onSuccess: (_, variables) => {
      // File's project list change rewires plan rows for every affected
      // project, so invalidate both library-files and project-* queries.
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      queryClient.invalidateQueries({ queryKey: ['project-print-plan'] });
      queryClient.invalidateQueries({ queryKey: ['project-files'] });
      setLinkFile(null);
      const isUnlink =
        Array.isArray(variables.data.project_ids) && variables.data.project_ids.length === 0;
      showToast(isUnlink ? t('fileManager.toast.fileUnlinked') : t('fileManager.toast.fileLinked'), 'success');
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const renameFileMutation = useMutation({
    mutationFn: ({ id, filename }: { id: number; filename: string }) =>
      api.updateLibraryFile(id, { filename }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      setRenameItem(null);
      showToast(t('fileManager.toast.fileRenamed'), 'success');
    },
    onError: (error: Error) => {
      setRenameItem(null);
      showToast(error.message, 'error');
    },
  });

  const renameFolderMutation = useMutation({
    mutationFn: ({ id, name }: { id: number; name: string }) =>
      api.updateLibraryFolder(id, { name }),
    onSuccess: () => {
      // Invalidate both folders and files - files may display folder info
      queryClient.invalidateQueries({ queryKey: ['library-folders'] });
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      setRenameItem(null);
      showToast(t('fileManager.toast.folderRenamed'), 'success');
    },
    onError: (error: Error) => {
      setRenameItem(null);
      showToast(error.message, 'error');
    },
  });

  const batchThumbnailMutation = useMutation({
    mutationFn: () => api.batchGenerateStlThumbnails({ all_missing: true }),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      // Update thumbnail versions for cache busting
      if (result.succeeded > 0) {
        const now = Date.now();
        const newVersions: Record<number, number> = {};
        result.results.forEach((r) => {
          if (r.success) {
            newVersions[r.file_id] = now;
          }
        });
        setThumbnailVersions((prev) => ({ ...prev, ...newVersions }));
      }
      if (result.succeeded > 0 && result.failed === 0) {
        showToast(t('fileManager.toast.thumbnailsGenerated', { count: result.succeeded }), 'success');
      } else if (result.succeeded > 0 && result.failed > 0) {
        showToast(t('fileManager.toast.thumbnailsGeneratedPartial', { succeeded: result.succeeded, failed: result.failed }), 'success');
      } else if (result.processed === 0) {
        showToast(t('fileManager.toast.noStlMissingThumbnails'), 'info');
      } else {
        showToast(t('fileManager.toast.failedToGenerateThumbnails', { error: result.results[0]?.error || 'Unknown error' }), 'error');
      }
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const singleThumbnailMutation = useMutation({
    mutationFn: (fileId: number) => api.batchGenerateStlThumbnails({ file_ids: [fileId] }),
    // Track which file is mid-regen so the cards can show an overlay
    // spinner. ``mutation.variables`` IS the file id while pending, but
    // mirroring it into a state keeps the prop-drilling shape simple
    // (one number/null instead of poking at the mutation object).
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      // Update thumbnail version for cache busting
      if (result.succeeded > 0) {
        const fileId = result.results[0]?.file_id;
        if (fileId) {
          setThumbnailVersions((prev) => ({ ...prev, [fileId]: Date.now() }));
        }
        showToast(t('fileManager.toast.thumbnailGenerated'), 'success');
      } else {
        showToast(t('fileManager.toast.failedToGenerateThumbnail', { error: result.results[0]?.error || 'Unknown error' }), 'error');
      }
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  // Derive the in-flight file id from the mutation directly. While
  // pending, ``variables`` is the file id passed to ``mutate(id)``;
  // when settled it falls back to null and the overlay clears.
  const regeneratingFileId = singleThumbnailMutation.isPending
    ? (singleThumbnailMutation.variables ?? null)
    : null;

  // Get sliced files from selection — predicate now reads from
  // ``file_tags`` via the shared ``isPrintable`` helper instead of a
  // local filename-suffix scan, so bulk-print actions agree with the
  // per-row Print button on what counts as "printable".
  const selectedSlicedFiles = useMemo(() => {
    if (!files) return [];
    return files.filter((f) => selectedFiles.includes(f.id) && isPrintable(f));
  }, [files, selectedFiles]);

  // Schedule one file from its own ⋮ menu — a run of length 1, which renders
  // exactly as the dialog always did (the counter only appears for several
  // files) and leaves the selection alone, like Move and Tags do from there.
  const scheduleOne = useCallback(
    (file: LibraryFileListItem) => setQueueSequence({ files: [file], fromSelection: false }),
    [],
  );

  // Handlers
  const handleFileSelect = useCallback((id: number) => {
    // Always toggle selection (multi-select by default)
    setSelectedFiles((prev) => {
      return prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id];
    });
  }, []);

  // Open the archive page filtered to this file's print history. The file
  // name rides along so the archive page can show a "prints of <name>" chip
  // without a second lookup.
  const handleOpenArchives = useCallback((file: LibraryFileListItem) => {
    const params = new URLSearchParams({
      file: String(file.id),
      fileName: file.print_name || file.filename,
    });
    navigate(`/archives?${params.toString()}`);
  }, [navigate]);

  const handleSelectAll = useCallback(() => {
    if (filteredAndSortedFiles.length > 0) {
      setSelectedFiles(filteredAndSortedFiles.map((f) => f.id));
    }
  }, [filteredAndSortedFiles]);

  const handleDeselectAll = useCallback(() => {
    setSelectedFiles([]);
  }, []);

  const handleUploadComplete = () => {
    queryClient.invalidateQueries({ queryKey: ['library-files'] });
    queryClient.invalidateQueries({ queryKey: ['library-folders'] });
    queryClient.invalidateQueries({ queryKey: ['library-stats'] });
  };

  // Page-level drag-drop: drop anywhere over the files area opens
  // FileUploadModal with the files preloaded. dragenter/dragleave fire for
  // every child element, so the counter avoids the overlay flickering as the
  // pointer moves between nested nodes.
  const handlePageDragEnter = (e: DragEvent<HTMLDivElement>) => {
    if (!hasPermission('library:upload')) return;
    if (!e.dataTransfer.types.includes('Files')) return;
    e.preventDefault();
    dragCounterRef.current += 1;
    setIsPageDragging(true);
  };
  const handlePageDragOver = (e: DragEvent<HTMLDivElement>) => {
    if (!hasPermission('library:upload')) return;
    if (!e.dataTransfer.types.includes('Files')) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  };
  const handlePageDragLeave = (e: DragEvent<HTMLDivElement>) => {
    if (!hasPermission('library:upload')) return;
    e.preventDefault();
    dragCounterRef.current = Math.max(0, dragCounterRef.current - 1);
    if (dragCounterRef.current === 0) setIsPageDragging(false);
  };
  const handlePageDrop = (e: DragEvent<HTMLDivElement>) => {
    if (!hasPermission('library:upload')) return;
    e.preventDefault();
    dragCounterRef.current = 0;
    setIsPageDragging(false);
    const files = Array.from(e.dataTransfer.files);
    if (files.length === 0) return;
    setDroppedFiles(files);
    setShowUploadModal(true);
  };

  // Harden the drag overlay against the cancel paths the counter alone misses:
  // Escape mid-drag, or a drop/dragend that lands outside the drop area (drag
  // back out of the window, release off-page). Without these the overlay stays
  // stuck until a refresh (upstream Bambuddy #1510). Listeners only live while
  // the overlay is showing, so they never interfere with normal interaction.
  useEffect(() => {
    if (!isPageDragging) return;
    const reset = () => {
      dragCounterRef.current = 0;
      setIsPageDragging(false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') reset();
    };
    document.addEventListener('drop', reset);
    document.addEventListener('dragend', reset);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('drop', reset);
      document.removeEventListener('dragend', reset);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [isPageDragging]);

  const handleDownload = (id: number) => {
    api.downloadLibraryFile(id).catch((err) => {
      console.error('Library file download failed:', err);
    });
  };

  const handleDeleteConfirm = () => {
    if (!deleteConfirm) return;
    if (deleteConfirm.type === 'file') {
      deleteFileMutation.mutate(deleteConfirm.id);
    } else if (deleteConfirm.type === 'folder') {
      deleteFolderMutation.mutate(deleteConfirm.id);
    } else if (deleteConfirm.type === 'bulk') {
      bulkDeleteMutation.mutate(selectedFiles);
    }
  };

  const isDeleting = deleteFolderMutation.isPending || deleteFileMutation.isPending || bulkDeleteMutation.isPending;

  const handleViewModeChange = (mode: 'grid' | 'list') => {
    setViewMode(mode);
    localStorage.setItem('library-view-mode', mode);
  };

  const isLoading = foldersLoading || filesLoading;

  // Find the selected folder in the tree to check external status
  const selectedFolder = useMemo(() => {
    if (!selectedFolderId || !folders) return null;
    const findFolder = (items: LibraryFolderTree[]): LibraryFolderTree | null => {
      for (const item of items) {
        if (item.id === selectedFolderId) return item;
        const found = findFolder(item.children);
        if (found) return found;
      }
      return null;
    };
    return findFolder(folders);
  }, [selectedFolderId, folders]);

  // The "External" root is a virtual node whose children are linked shares —
  // "New folder" has nothing meaningful to create there (it used to silently
  // create at the library root), so the button disables and points at Link.
  const atExternalRoot = selectedFolderId === null && topLevelView === 'external';

  // One renderer for both root groups of the sidebar tree (own folders under
  // "All files", external roots under the "External" header) so the split
  // can't let their props drift apart.
  const renderRootFolder = (folder: LibraryFolderTree) => (
    <FolderTreeItem
      key={`${folder.id}-${collapseFoldersByDefault ? 'c' : 'e'}`}
      folder={folder}
      depth={1}
      selectedFolderId={selectedFolderId}
      onSelect={setSelectedFolderId}
      onDelete={(id) => setDeleteConfirm({ type: 'folder', id })}
      onLink={setLinkFolder}
      onRename={(f) => setRenameItem({ type: 'folder', id: f.id, name: f.name })}
      wrapNames={wrapFolderNames}
      defaultExpanded={!collapseFoldersByDefault}
      hasPermission={hasPermission}
      t={t}
      timeFormat={timeFormat}
      dateFormat={dateFormat}
    />
  );

  return (
    <div className="p-4 md:p-6 min-h-[calc(100vh)] lg:h-[calc(100vh)] flex flex-col">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-4">
        <div className="flex items-center gap-3">
          <div>
            <h1 className="text-2xl font-bold text-white flex items-center gap-3"><FolderOpen className="w-6 h-6 text-bambu-green" />{t('fileManager.title')}</h1>
            <p className="text-sm text-bambu-gray">{t('fileManager.subtitle')}</p>
          </div>
        </div>
        <div className="flex items-center gap-2 sm:gap-3 flex-wrap">
          {/* View mode toggle - style matches PrintersPage card-size selector */}
          <div className="flex items-center bg-bambu-dark rounded-lg border border-bambu-dark-tertiary">
            <button
              onClick={() => handleViewModeChange('grid')}
              className={`px-2 py-1.5 transition-colors rounded-l-lg ${
                viewMode === 'grid'
                  ? 'bg-bambu-green text-white'
                  : 'text-bambu-gray hover:bg-bambu-dark-tertiary hover:text-white'
              }`}
              title={t('fileManager.gridView')}
            >
              <LayoutGrid className="w-4 h-4" />
            </button>
            <button
              onClick={() => handleViewModeChange('list')}
              className={`px-2 py-1.5 transition-colors rounded-r-lg ${
                viewMode === 'list'
                  ? 'bg-bambu-green text-white'
                  : 'text-bambu-gray hover:bg-bambu-dark-tertiary hover:text-white'
              }`}
              title={t('fileManager.listView')}
            >
              <List className="w-4 h-4" />
            </button>
          </div>

          <div className="w-px h-6 bg-bambu-dark-tertiary" />

          <Button
            variant="outline"
            size="sm"
            onClick={() => batchThumbnailMutation.mutate()}
            disabled={batchThumbnailMutation.isPending || !hasAnyPermission('library:update_own', 'library:update_all')}
            title={!hasAnyPermission('library:update_own', 'library:update_all') ? t('fileManager.noPermissionGenerateThumbnail') : t('fileManager.generateThumbnailsForMissing')}
          >
            {batchThumbnailMutation.isPending ? (
              <Loader2 className="w-4 h-4 mr-2 animate-spin" />
            ) : (
              <Image className="w-4 h-4 mr-2" />
            )}
            {t('fileManager.generateThumbnails')}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setShowExternalFolderModal(true)}
            disabled={!hasPermission('library:upload')}
            title={!hasPermission('library:upload') ? t('fileManager.noPermissionCreateFolder') : t('fileManager.linkExternalFolder')}
          >
            <FolderSymlink className="w-4 h-4 mr-2" />
            {t('fileManager.linkExternal')}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setShowNewFolderModal(true)}
            disabled={!hasPermission('library:upload') || atExternalRoot}
            title={
              !hasPermission('library:upload')
                ? t('fileManager.noPermissionCreateFolder')
                : atExternalRoot
                  ? t('fileManager.newFolderExternalRootHint')
                  : undefined
            }
          >
            <FolderPlus className="w-4 h-4 mr-2" />
            {t('fileManager.newFolder')}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setShowTagsModal(true)}
            title={t('fileManager.tags.manageTitle')}
          >
            <TagIcon className="w-4 h-4 mr-2" />
            {t('fileManager.tags.manage')}
          </Button>
          <Button
            onClick={() => setShowUploadModal(true)}
            disabled={!hasPermission('library:upload')}
            title={!hasPermission('library:upload') ? t('fileManager.noPermissionUpload') : undefined}
          >
            <Upload className="w-4 h-4 mr-2" />
            {t('common.upload')}
          </Button>
          {hasAnyPermission('library:delete_own', 'library:delete_all') && (
            <TrashSplitButton
              trashHref="/files/trash"
              trashLabel={t('libraryTrash.headerButton')}
              trashTooltip={t('libraryTrash.headerTooltip')}
              count={trashCount}
              onPurgeClick={hasPermission('library:purge') ? () => setShowPurgeModal(true) : undefined}
              purgeLabel={t('libraryPurge.headerButton')}
              purgeTooltip={t('libraryPurge.headerTooltip')}
            />
          )}
        </div>
      </div>

      {/* Disk space warning */}
      {isDiskSpaceLow && stats && settings && (
        <div className="flex items-center gap-3 mb-4 p-3 bg-amber-500/10 border border-amber-500/30 rounded-lg">
          <AlertTriangle className="w-5 h-5 text-amber-500 flex-shrink-0" />
          <div className="flex-1">
            <p className="text-sm text-amber-500 font-medium">{t('fileManager.lowDiskSpaceWarning')}</p>
            <p className="text-xs text-amber-500/80">
              {t('fileManager.lowDiskSpaceDetails', { free: formatFileSize(stats.disk_free_bytes), total: formatFileSize(stats.disk_total_bytes), threshold: settings.library_disk_warning_gb })}
            </p>
          </div>
        </div>
      )}

      {/* Stats bar */}
      {stats && (
        <div className="flex flex-wrap items-center gap-3 sm:gap-6 mb-4 p-3 bg-bambu-dark-secondary rounded-lg border border-bambu-dark-tertiary">
          <div className="flex items-center gap-2 text-sm">
            <File className="w-4 h-4 text-bambu-green" />
            <span className="text-bambu-gray">{t('fileManager.files')}:</span>
            <span className="text-white font-medium">{stats.total_files}</span>
          </div>
          <div className="flex items-center gap-2 text-sm">
            <FolderOpen className="w-4 h-4 text-blue-600 dark:text-blue-400" />
            <span className="text-bambu-gray">{t('fileManager.folders')}:</span>
            <span className="text-white font-medium">{stats.total_folders}</span>
          </div>
          <div className="flex items-center gap-2 text-sm">
            <HardDrive className="w-4 h-4 text-amber-600 dark:text-amber-400" />
            <span className="text-bambu-gray">{t('fileManager.size')}:</span>
            <span className="text-white font-medium">{formatFileSize(stats.total_size_bytes)}</span>
          </div>
          <div className="flex items-center gap-2 text-sm sm:ml-auto">
            <span className="text-bambu-gray">{t('fileManager.free')}:</span>
            <span className={`font-medium ${isDiskSpaceLow ? 'text-amber-500' : 'text-white'}`}>
              {formatFileSize(stats.disk_free_bytes)}
            </span>
          </div>
        </div>
      )}

      {/* Main content */}
      <div className="flex-1 flex flex-col lg:flex-row gap-4 min-h-0">
        {/* Mobile folder selector */}
        <div className="lg:hidden">
          <select
            value={selectedFolderId !== null ? String(selectedFolderId) : `__top:${topLevelView}`}
            onChange={(e) => {
              const v = e.target.value;
              if (v.startsWith('__top:')) {
                setSelectedFolderId(null);
                setTopLevelView(v.slice('__top:'.length) as 'internal' | 'external');
              } else {
                setSelectedFolderId(parseInt(v, 10));
              }
            }}
            className="w-full bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg px-3 py-2.5 text-white focus:outline-none focus:border-bambu-green"
          >
            {/* Same grouping as the desktop sidebar: own folders under
                "All files", external roots after the "External" entry. */}
            <option value="__top:internal">📁 {t('fileManager.allFiles')}</option>
            {(() => {
              // Flatten folder tree for mobile selector
              const flattenFolders = (items: LibraryFolderTree[], depth = 0): { id: number; name: string; fileCount: number; depth: number }[] => {
                const result: { id: number; name: string; fileCount: number; depth: number }[] = [];
                for (const item of items) {
                  result.push({ id: item.id, name: item.name, fileCount: item.file_count, depth });
                  if (item.children.length > 0) {
                    result.push(...flattenFolders(item.children, depth + 1));
                  }
                }
                return result;
              };
              const toOptions = (items: LibraryFolderTree[] | undefined) =>
                flattenFolders(items ?? [], 1).map((folder) => (
                  <option key={folder.id} value={folder.id}>
                    {'│ '.repeat(folder.depth)}📂 {folder.name} {folder.fileCount > 0 ? `(${folder.fileCount})` : ''}
                  </option>
                ));
              return (
                <>
                  {toOptions(ownRootFolders)}
                  {folders?.some((f) => f.is_external) && (
                    <option value="__top:external">🔗 {t('fileManager.allExternal')}</option>
                  )}
                  {toOptions(externalRootFolders)}
                </>
              );
            })()}
          </select>
        </div>

        {/* Folder sidebar - resizable, hidden on mobile */}
        <div
          ref={sidebarRef}
          className="hidden lg:flex flex-shrink-0 bg-bambu-dark-secondary rounded-lg border border-bambu-dark-tertiary overflow-hidden flex-col relative lg:sticky lg:top-4 lg:self-start lg:min-h-[calc(100vh-14rem)] lg:max-h-[calc(100vh-6rem)]"
          style={{ width: `${sidebarWidth}px` }}
        >
          {/* Resize handle - drag to resize, double-click to reset */}
          <div
            className={`absolute right-0 top-0 bottom-0 w-1.5 cursor-col-resize z-10 group/resize flex items-center justify-center transition-colors ${
              isResizing ? 'bg-bambu-green' : 'hover:bg-bambu-green/50'
            }`}
            onMouseDown={(e) => {
              e.preventDefault();
              setIsResizing(true);
            }}
            onDoubleClick={() => {
              setSidebarWidth(256); // Reset to default w-64
              localStorage.setItem('library-sidebar-width', '256');
            }}
            title={t('fileManager.dragToResizeTooltip')}
          >
            {/* Grip dots */}
            <div className={`flex flex-col gap-1 opacity-0 group-hover/resize:opacity-100 transition-opacity ${isResizing ? 'opacity-100' : ''}`}>
              <div className="w-0.5 h-0.5 rounded-full bg-white/70" />
              <div className="w-0.5 h-0.5 rounded-full bg-white/70" />
              <div className="w-0.5 h-0.5 rounded-full bg-white/70" />
            </div>
          </div>
          <div className="p-3 border-b border-bambu-dark-tertiary flex items-center justify-between">
            <h2 className="text-sm font-medium text-white">{t('fileManager.folders')}</h2>
            <div className="flex items-center gap-1">
              {/* Folder tree sort (#1770). Dropdown drives the comparator;
                  direction button flips asc/desc. Both persist to localStorage
                  on change so the choice survives reloads. */}
              <select
                value={folderSortField}
                onChange={(e) => {
                  const v = e.target.value === 'activity' ? 'activity' : 'name';
                  setFolderSortField(v);
                  localStorage.setItem('library-folder-sort-field', v);
                }}
                className="text-xs px-1 py-0.5 rounded bg-bambu-dark border border-bambu-dark-tertiary text-bambu-gray focus:outline-none focus:border-bambu-green"
                title={t('fileManager.folderSort')}
                aria-label={t('fileManager.folderSort')}
              >
                <option value="name">{t('fileManager.folderSortByName')}</option>
                <option value="activity">{t('fileManager.folderSortByActivity')}</option>
              </select>
              <button
                onClick={() => {
                  const newValue = folderSortDirection === 'asc' ? 'desc' : 'asc';
                  setFolderSortDirection(newValue);
                  localStorage.setItem('library-folder-sort-direction', newValue);
                }}
                className="text-bambu-gray hover:text-white hover:bg-bambu-dark p-1.5 rounded transition-colors"
                title={folderSortDirection === 'asc' ? t('fileManager.ascending') : t('fileManager.descending')}
                aria-label={folderSortDirection === 'asc' ? t('fileManager.ascending') : t('fileManager.descending')}
              >
                {folderSortDirection === 'asc' ? (
                  <ArrowUpNarrowWide className="w-4 h-4" />
                ) : (
                  <ArrowDownWideNarrow className="w-4 h-4" />
                )}
              </button>
              <button
                onClick={() => {
                  const newValue = !collapseFoldersByDefault;
                  setCollapseFoldersByDefault(newValue);
                  localStorage.setItem('library-collapse-folders', String(newValue));
                }}
                className={`p-1.5 rounded transition-colors ${
                  collapseFoldersByDefault
                    ? 'bg-bambu-green/20 text-bambu-green'
                    : 'text-bambu-gray hover:text-white hover:bg-bambu-dark'
                }`}
                title={collapseFoldersByDefault ? t('fileManager.expandFoldersByDefault') : t('fileManager.collapseFoldersByDefault')}
                aria-label={collapseFoldersByDefault ? t('fileManager.expandFoldersByDefault') : t('fileManager.collapseFoldersByDefault')}
              >
                <ListCollapse className="w-4 h-4" />
              </button>
              <button
                onClick={() => {
                  const newValue = !wrapFolderNames;
                  setWrapFolderNames(newValue);
                  localStorage.setItem('library-wrap-folders', String(newValue));
                }}
                className={`p-1.5 rounded transition-colors ${
                  wrapFolderNames
                    ? 'bg-bambu-green/20 text-bambu-green'
                    : 'text-bambu-gray hover:text-white hover:bg-bambu-dark'
                }`}
                title={wrapFolderNames ? t('fileManager.disableTextWrapping') : t('fileManager.enableTextWrapping')}
                aria-label={wrapFolderNames ? t('fileManager.disableTextWrapping') : t('fileManager.enableTextWrapping')}
              >
                <WrapText className="w-4 h-4" />
              </button>
            </div>
          </div>
          <div className="flex-1 overflow-y-auto p-2">
            {/* "Internal" root = the user's own uploaded / managed-storage
                files only; its folders render indented beneath it. External
                roots live under the "External" header below, so a linked NAS
                can't drown the user's own uploads (#1621). */}
            <div
              className={`flex items-center gap-2 px-2 py-1.5 rounded cursor-pointer transition-colors ${
                selectedFolderId === null && topLevelView === 'internal'
                  ? 'bg-bambu-green/20 text-bambu-green'
                  : 'hover:bg-bambu-dark text-white'
              }`}
              onClick={() => {
                setSelectedFolderId(null);
                setTopLevelView('internal');
              }}
            >
              <FileBox className="w-4 h-4" />
              <span className="text-sm">{t('fileManager.allFiles')}</span>
            </div>

            {/* Folder tree — re-key on the collapse toggle so flipping it
                remounts every FolderTreeItem, which re-reads defaultExpanded
                and makes the preference take effect immediately. Own folders
                render here, under "All files"; external roots render below,
                under the "External" header — each header heads its group. */}
            {ownRootFolders?.map(renderRootFolder)}

            {/* External (combined) — only shown when at least one external
                folder is linked. Single-folder users don't need a combined
                view; clicking the individual folder is just as fast. */}
            {folders?.some((f) => f.is_external) && (
              <div
                className={`flex items-center gap-2 px-2 py-1.5 rounded cursor-pointer transition-colors ${
                  selectedFolderId === null && topLevelView === 'external'
                    ? 'bg-bambu-green/20 text-bambu-green'
                    : 'hover:bg-bambu-dark text-white'
                }`}
                onClick={() => {
                  setSelectedFolderId(null);
                  setTopLevelView('external');
                }}
              >
                <FolderSymlink className="w-4 h-4 text-purple-600 dark:text-purple-400" />
                <span className="text-sm">{t('fileManager.allExternal')}</span>
              </div>
            )}
            {externalRootFolders?.map(renderRootFolder)}
          </div>
        </div>

        {/* Files area */}
        <div
          className="flex-1 flex flex-col min-w-0 min-h-0 relative"
          onDragEnter={handlePageDragEnter}
          onDragOver={handlePageDragOver}
          onDragLeave={handlePageDragLeave}
          onDrop={handlePageDrop}
        >
          {isPageDragging && (
            <div className="absolute inset-0 z-30 pointer-events-none flex items-center justify-center rounded-lg border-2 border-dashed border-bambu-green bg-bambu-green/10 backdrop-blur-sm">
              <div className="flex flex-col items-center gap-3 text-center px-6">
                <Upload className="w-12 h-12 text-bambu-green" />
                <p className="text-lg font-medium text-white">{t('fileManager.dropFilesToUpload')}</p>
                <p className="text-sm text-bambu-green">{t('fileManager.dropFilesToUploadHint')}</p>
              </div>
            </div>
          )}
          {/* External folder info bar */}
          {selectedFolder?.is_external && (
            <div className="flex items-center gap-3 mb-4 p-3 bg-purple-50 dark:bg-purple-500/10 border border-purple-300 dark:border-purple-500/30 rounded-lg">
              <FolderSymlink className="w-5 h-5 text-purple-600 dark:text-purple-400 flex-shrink-0" />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-purple-700 dark:text-purple-300">{t('fileManager.externalFolder')}</span>
                  {selectedFolder.external_readonly && (
                    <span className="text-xs px-1.5 py-0.5 rounded bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-400 flex items-center gap-1">
                      <Lock className="w-3 h-3" />
                      {t('fileManager.readOnly')}
                    </span>
                  )}
                </div>
                <p className="text-xs text-bambu-gray truncate font-mono" title={selectedFolder.external_path || ''}>
                  {selectedFolder.external_path}
                </p>
              </div>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => selectedFolderId && scanExternalFolderMutation.mutate(selectedFolderId)}
                disabled={scanExternalFolderMutation.isPending || activeScan?.status === 'running'}
                title={t('fileManager.scanFolder')}
              >
                {scanExternalFolderMutation.isPending || activeScan?.status === 'running' ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <RefreshCw className="w-4 h-4" />
                )}
                <span className="ml-1.5">{t('fileManager.scanFolder')}</span>
              </Button>
            </div>
          )}
          {/* Scan progress. The walk runs in the background now, so this strip
              is the only place its numbers appear — and the only place an
              unreachable mount is explained. */}
          {activeScan && activeScan.status === 'running' && (
            <div className="mb-4 p-3 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg">
              <div className="flex items-center gap-3">
                <Loader2 className="w-4 h-4 text-bambu-green animate-spin flex-shrink-0" />
                <span className="text-sm text-white">
                  {activeScan.total > 0
                    ? t('fileManager.scanProgress.counted', { seen: activeScan.seen, total: activeScan.total })
                    : t('fileManager.scanProgress.counting')}
                </span>
                <span className="text-xs text-bambu-gray ml-auto tabular-nums">
                  {t('fileManager.scanProgress.counters', {
                    added: activeScan.added,
                    updated: activeScan.updated,
                    removed: activeScan.removed,
                  })}
                </span>
              </div>
              {activeScan.total > 0 && (
                <div className="mt-2 h-1 bg-bambu-dark rounded-full overflow-hidden">
                  <div
                    className="h-full bg-bambu-green transition-all duration-300"
                    style={{ width: `${Math.min(100, Math.round((activeScan.seen / activeScan.total) * 100))}%` }}
                  />
                </div>
              )}
            </div>
          )}
          {activeScan && activeScan.status === 'failed' && (
            <div className="flex items-start gap-3 mb-4 p-3 bg-red-500/10 border border-red-500/30 rounded-lg">
              <AlertTriangle className="w-5 h-5 text-red-400 flex-shrink-0 mt-0.5" />
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-red-300">{t('fileManager.scanProgress.failed')}</p>
                {activeScan.error && (
                  <p className="text-xs text-bambu-gray mt-0.5 break-words">{activeScan.error}</p>
                )}
              </div>
              <button
                onClick={() => selectedFolderId && dismissScan(selectedFolderId)}
                className="text-bambu-gray hover:text-white flex-shrink-0"
                title={t('common.dismiss')}
              >
                <X className="w-4 h-4" />
              </button>
            </div>
          )}
          {activeScan && activeScan.status === 'finished' && activeScan.skippedDeletions && (
            <div className="flex items-start gap-3 mb-4 p-3 bg-amber-500/10 border border-amber-500/30 rounded-lg">
              <AlertTriangle className="w-5 h-5 text-amber-400 flex-shrink-0 mt-0.5" />
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-amber-300">
                  {t('fileManager.scanProgress.skippedDeletionsTitle')}
                </p>
                <p className="text-xs text-bambu-gray mt-0.5">
                  {t('fileManager.scanProgress.skippedDeletionsBody')}
                </p>
              </div>
              <button
                onClick={() => selectedFolderId && dismissScan(selectedFolderId)}
                className="text-bambu-gray hover:text-white flex-shrink-0"
                title={t('common.dismiss')}
              >
                <X className="w-4 h-4" />
              </button>
            </div>
          )}
          {/* Combined toolbar: search/filters/sort (row 1) + selection actions (row 2).
              ⚠️ `files` is now the server-FILTERED page, not the raw
              folder/tag-scoped fetch — a filter that matches zero rows would
              otherwise hide this toolbar exactly when it's needed to clear or
              adjust the filter, so `anyFilterActive` keeps it up regardless. */}
          {files && (files.length > 0 || anyFilterActive) && (
            <div className="flex flex-col gap-2 mb-4 p-3 bg-bambu-dark-secondary rounded-lg border border-bambu-dark-tertiary sticky top-0 z-10 lg:static">
            <div className="flex flex-wrap items-stretch gap-2">
              {/* Search */}
              <div className="relative w-full sm:w-[28rem] h-9">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-bambu-gray/50" />
                <input
                  type="text"
                  placeholder={t('fileManager.searchFiles')}
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className="w-full h-9 pl-10 pr-3 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-sm text-white placeholder:text-bambu-gray/50 focus:outline-none focus:border-bambu-green"
                />
              </div>

              {/* Type filter */}
              <select
                value={filterType}
                onChange={(e) => setFilterType(e.target.value)}
                className="h-9 min-w-[9rem] text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded-lg px-3 text-white focus:border-bambu-green focus:outline-none"
              >
                <option value="all">{t('fileManager.allTypes')}</option>
                {fileTypes.map((type) => (
                  <option key={type} value={type}>
                    {type.toUpperCase()}
                  </option>
                ))}
              </select>

              {/* A toggle, not a fourth select: the question is binary, and a
                  two-option dropdown is heavier than its answer. */}
              <button
                type="button"
                onClick={() => setUnprintedOnly((on) => !on)}
                aria-pressed={unprintedOnly}
                className={`h-9 px-3 text-sm rounded-lg border transition-colors ${
                  unprintedOnly
                    ? 'bg-bambu-green/20 border-bambu-green text-bambu-green'
                    : 'bg-bambu-dark border-bambu-dark-tertiary text-bambu-gray hover:text-white'
                }`}
              >
                {t('fileManager.unprintedOnly')}
              </button>

              {/* Username filter with autocomplete - only when auth is enabled */}
              {authEnabled && (
                <div className="relative h-9">
                  <input
                    type="text"
                    placeholder={t('fileManager.filterByUser', { defaultValue: 'Filter by user' })}
                    value={filterUsername}
                    onChange={(e) => setFilterUsername(e.target.value)}
                    list="usernames-list"
                    className={`w-40 h-9 px-3 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-sm text-white placeholder:text-bambu-gray/50 focus:outline-none focus:border-bambu-green ${filterUsername ? 'pr-8' : ''}`}
                    style={filterUsername ? { WebkitAppearance: 'none', MozAppearance: 'textfield' } : undefined}
                  />
                  {filterUsername && (
                    <button
                      onClick={() => setFilterUsername('')}
                      className="absolute right-2 top-1/2 -translate-y-1/2 text-bambu-gray hover:text-white z-10"
                    >
                      <X className="w-3 h-3" />
                    </button>
                  )}
                  <datalist id="usernames-list">
                    {users?.map((user) => (
                      <option key={user.id} value={user.username} />
                    ))}
                  </datalist>
                </div>
              )}

              {/* Results count — `total` is now the server's grand total across
                  every page (`meta.total`), not the size of one client-side
                  fetch; `showing` is this page's own item count. */}
              {anyFilterActive && (
                <span className="h-9 flex items-center text-sm text-bambu-gray hidden sm:inline-flex">
                  {t('fileManager.resultsCount', { showing: filteredAndSortedFiles.length, total: meta?.total ?? 0 })}
                </span>
              )}

              {/* Sort - pushed to far right via ml-auto */}
              <div className="flex items-center gap-1 ml-auto">
                <select
                  value={sortField}
                  onChange={(e) => {
                    const newField = e.target.value as SortField;
                    setSortField(newField);
                    localStorage.setItem('library-sort-field', newField);
                  }}
                  className="h-9 min-w-[9rem] text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded-lg px-3 text-white focus:border-bambu-green focus:outline-none"
                >
                  <option value="name">{t('common.name')}</option>
                  <option value="date">{t('common.date')}</option>
                  <option value="size">{t('fileManager.size')}</option>
                  <option value="type">{t('common.type')}</option>
                </select>
                <button
                  onClick={() => setSortDirection((d) => {
                    const newDir = d === 'asc' ? 'desc' : 'asc';
                    localStorage.setItem('library-sort-direction', newDir);
                    return newDir;
                  })}
                  className="h-9 w-9 flex items-center justify-center bg-bambu-dark border border-bambu-dark-tertiary rounded-lg hover:border-bambu-green transition-colors"
                  title={sortDirection === 'asc' ? t('fileManager.ascending') : t('fileManager.descending')}
                >
                  {sortDirection === 'asc' ? (
                    <ArrowUpNarrowWide className="w-4 h-4 text-bambu-gray" />
                  ) : (
                    <ArrowDownWideNarrow className="w-4 h-4 text-bambu-gray" />
                  )}
                </button>
              </div>
            </div>

            {/* #1268: recursive-search hint — placed below the toolbar row
                (not floating under the input) so it never overlaps wrapped
                filters on narrow breakpoints. */}
            {searchExpandsSubfolders && (
              <span className="text-[10px] text-bambu-gray whitespace-nowrap">
                {t('fileManager.searchSubfoldersHint')}
              </span>
            )}

            {/* THE tag filter row — both kinds, one mechanism. System pills
                first in their own colours, then a divider, then the user's own
                in green. Every one of them toggles the same `selectedTagIds`,
                which the server AND-filters through `tag_ids`; there is no
                client-side tag predicate any more. Active = filled with an X. */}
            {(systemTagPills.length > 0 || userTagPills.length > 0) && (
              // A row OF the filter panel, like the chip row it replaces —
              // same top border as the selection row below it. A tag filter
              // floating under the panel is a filter outside the filters.
              <div className="flex items-center gap-1.5 flex-wrap pt-2 border-t border-bambu-dark-tertiary">
                <span className="text-xs text-bambu-gray mr-1 inline-flex items-center gap-1">
                  <TagIcon className="w-3.5 h-3.5" />
                  {t('fileManager.tags.filterLabel')}
                </span>
                {systemTagPills.map((tag) => {
                  const active = selectedTagIds.includes(tag.id);
                  const style = tag.code ? getTagStyle(tag.code) : null;
                  // Second argument matters: a code this frontend has no
                  // translation for falls back to the backend's English name
                  // instead of rendering the key at the user.
                  const label = tag.code ? t(`library.tags.${tag.code}`, tag.name) : tag.name;
                  return (
                    <button
                      key={tag.id}
                      type="button"
                      onClick={() => toggleTagFilter(tag.id)}
                      className={`text-xs px-2 py-0.5 rounded font-medium transition-colors inline-flex items-center gap-1 ${
                        active
                          ? `${style?.bg ?? 'bg-bambu-gray/70'} ${style?.text ?? 'text-white'}`
                          : 'bg-bambu-dark border border-bambu-dark-tertiary text-bambu-gray hover:text-white hover:border-bambu-gray'
                      }`}
                    >
                      {label}
                      {active && <X className="w-3 h-3" />}
                    </button>
                  );
                })}
                {systemTagPills.length > 0 && userTagPills.length > 0 && (
                  // The two groups answer differently shaped questions — what a
                  // file IS versus what somebody called it — and without a break
                  // they read as one undifferentiated wall.
                  <span aria-hidden className="text-bambu-gray/30 px-1 select-none">
                    |
                  </span>
                )}
                {userTagPills.map((tag) => {
                  const active = selectedTagIds.includes(tag.id);
                  return (
                    <button
                      key={tag.id}
                      type="button"
                      onClick={() => toggleTagFilter(tag.id)}
                      className={`text-xs px-2 py-0.5 rounded-full font-medium transition-colors inline-flex items-center gap-1 ${
                        active
                          ? 'bg-bambu-green text-white'
                          : 'bg-bambu-dark border border-bambu-dark-tertiary text-bambu-gray hover:text-white hover:border-bambu-green/60'
                      }`}
                    >
                      {tag.name}
                      {active && <X className="w-3 h-3" />}
                    </button>
                  );
                })}
                {selectedTagIds.length > 0 && (
                  <button
                    type="button"
                    onClick={() => setSelectedTagIds([])}
                    className="text-xs px-2 py-0.5 rounded text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary transition-colors"
                  >
                    {t('fileManager.tags.clearAll')}
                  </button>
                )}
              </div>
            )}

            {/* Selection row - rendered inside the same panel as a second row. */}
            {filteredAndSortedFiles.length > 0 && (
              <div className="flex flex-wrap items-center gap-2 pt-2 border-t border-bambu-dark-tertiary">
              {/* Select all / Deselect all */}
              {selectedFiles.length === filteredAndSortedFiles.length && selectedFiles.length > 0 ? (
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={handleDeselectAll}
                >
                  <Square className="w-4 h-4 sm:mr-1" />
                  <span className="hidden sm:inline">{t('fileManager.deselectAll')}</span>
                </Button>
              ) : (
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={handleSelectAll}
                >
                  <CheckSquare className="w-4 h-4 sm:mr-1" />
                  <span className="hidden sm:inline">{t('fileManager.selectAll')}</span>
                </Button>
              )}

              {selectedFiles.length > 0 && (
                <>
                  <span className="text-sm text-bambu-gray ml-2">
                    {t('fileManager.selected', { count: selectedFiles.length })}
                  </span>
                  <div className="hidden sm:block flex-1" />
                  <div className="w-full sm:w-auto flex flex-wrap items-center gap-2 mt-2 sm:mt-0">
                    {selectedSlicedFiles.length === 1 && (
                      <Button
                        variant="primary"
                        size="sm"
                        onClick={() => setPrintMultiFile(selectedSlicedFiles[0])}
                        disabled={!hasPermission('printers:control')}
                        title={!hasPermission('printers:control') ? t('fileManager.noPermissionPrint') : undefined}
                      >
                        <Play className="w-4 h-4 sm:mr-1" />
                        <span className="hidden sm:inline">{t('common.print')}</span>
                      </Button>
                    )}
                    {/* Gated on > 0, unlike Print above: the Schedule dialog
                        takes one file, so several files are the same dialog
                        several times over (QueueSequencer). Hidden rather than
                        disabled when nothing selected is sliced — a button that
                        opens a window saying "nothing here can be queued"
                        spends two clicks on what its absence says for free. */}
                    {selectedSlicedFiles.length > 0 && (
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() => setQueueSequence({ files: selectedSlicedFiles, fromSelection: true })}
                        disabled={!hasPermission('queue:create')}
                        title={!hasPermission('queue:create') ? t('fileManager.noPermissionAddToQueue') : undefined}
                      >
                        <Clock className="w-4 h-4 sm:mr-1" />
                        <span className="hidden sm:inline">{t('fileManager.schedulePrint')}</span>
                      </Button>
                    )}
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => setShowMoveModal(true)}
                      disabled={!hasAnyPermission('library:update_own', 'library:update_all')}
                      title={!hasAnyPermission('library:update_own', 'library:update_all') ? t('fileManager.noPermissionMoveFiles') : undefined}
                    >
                      <MoveRight className="w-4 h-4 sm:mr-1" />
                      <span className="hidden sm:inline">{t('common.move')}</span>
                    </Button>
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => setShowBulkTagsModal(true)}
                      disabled={!hasAnyPermission('library:update_own', 'library:update_all')}
                      title={!hasAnyPermission('library:update_own', 'library:update_all') ? t('fileManager.tags.noPermission') : t('fileManager.tags.bulkTooltip')}
                    >
                      <TagIcon className="w-4 h-4 sm:mr-1" />
                      <span className="hidden sm:inline">{t('fileManager.tags.tagAction')}</span>
                    </Button>
                    <Button
                      variant="danger"
                      size="sm"
                      onClick={() => {
                        if (selectedFiles.length === 1) {
                          setDeleteConfirm({ type: 'file', id: selectedFiles[0] });
                        } else {
                          setDeleteConfirm({ type: 'bulk', id: 0, count: selectedFiles.length });
                        }
                      }}
                      disabled={!hasAnyPermission('library:delete_own', 'library:delete_all')}
                      title={!hasAnyPermission('library:delete_own', 'library:delete_all') ? t('fileManager.noPermissionDeleteFiles') : undefined}
                    >
                      <Trash2 className="w-4 h-4 sm:mr-1" />
                      <span className="hidden sm:inline">{t('common.delete')}</span>
                    </Button>
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={handleDeselectAll}
                    >
                      <X className="w-4 h-4 sm:mr-1" />
                      <span className="hidden sm:inline">{t('common.clear')}</span>
                    </Button>
                  </div>
                </>
              )}
              </div>
            )}
            </div>
          )}

          {/* File grid/list */}
          {isLoading ? (
            <div className="flex-1 flex items-center justify-center">
              <div className="flex flex-col items-center gap-3">
                <Loader2 className="w-8 h-8 animate-spin text-bambu-green" />
                <p className="text-sm text-bambu-gray">{t('fileManager.loadingFiles')}</p>
              </div>
            </div>
          ) : /* An EMPTY LISTING is not the same as an empty library. The
                 user-tag filter is applied server-side, so a filter that
                 matches nothing comes back as zero rows — and this branch used
                 to answer that with "No files yet" and an Upload button, which
                 is both wrong and a dead end: the reset lives in the branch
                 below, so the only way out was to know about the tag pill. */
          files?.length === 0 && !anyFilterActive ? (
            <div className="flex-1 flex flex-col items-center justify-center">
              <div className="p-4 bg-bambu-dark rounded-2xl mb-4">
                <FileBox className="w-12 h-12 text-bambu-gray/50" />
              </div>
              <h3 className="text-lg font-medium text-white mb-2">
                {selectedFolderId !== null
                  ? t('fileManager.folderIsEmpty')
                  : topLevelView === 'external'
                    ? t('fileManager.externalIsEmpty')
                    : t('fileManager.noFilesYet')}
              </h3>
              <p className="text-bambu-gray text-center max-w-md mb-6">
                {selectedFolderId !== null
                  ? t('fileManager.folderEmptyDescription')
                  : topLevelView === 'external'
                    ? t('fileManager.externalEmptyDescription')
                    : t('fileManager.noFilesDescription')}
              </p>
              <Button
                onClick={() => setShowUploadModal(true)}
                disabled={!hasPermission('library:upload')}
                title={!hasPermission('library:upload') ? t('fileManager.noPermissionUpload') : undefined}
              >
                <Plus className="w-4 h-4 mr-2" />
                {t('fileManager.uploadFiles')}
              </Button>
            </div>
          ) : filteredAndSortedFiles.length === 0 ? (
            <div className="flex-1 flex flex-col items-center justify-center">
              <div className="p-4 bg-bambu-dark rounded-2xl mb-4">
                <Search className="w-12 h-12 text-bambu-gray/50" />
              </div>
              <h3 className="text-lg font-medium text-white mb-2">{t('fileManager.noMatchingFiles')}</h3>
              <p className="text-bambu-gray text-center max-w-md mb-6">
                {t('fileManager.noMatchingFilesDescription')}
              </p>
              <Button variant="secondary" onClick={clearAllFilters}>
                {t('fileManager.clearFilters')}
              </Button>
            </div>
          ) : viewMode === 'grid' ? (
            <div className="flex-1 lg:overflow-y-auto">
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6 gap-4">
                {filteredAndSortedFiles.map((file) => (
                  <FileCard
                    key={file.id}
                    file={file}
                    isSelected={selectedFiles.includes(file.id)}
                    isMobile={isMobile}
                    t={t}
                    onSelect={handleFileSelect}
                    onOpenArchives={handleOpenArchives}
                    onDelete={(id) => setDeleteConfirm({ type: 'file', id })}
                    onDownload={handleDownload}
                    onAddToQueue={(id) => {
                      const file = files?.find(f => f.id === id);
                      if (file) scheduleOne(file);
                    }}
                    onPrint={setPrintFile}
                    onSlice={setSliceFile}
                    onOpenInSlicer={handleOpenInSlicer}
                    useSlicerApi={settings?.use_slicer_api ?? false}
                    onPreview3d={setViewerFile}
                    onRename={(f) => setRenameItem({ type: 'file', id: f.id, name: f.filename })}
                    onLink={setLinkFile}
                    onGenerateThumbnail={(f) => singleThumbnailMutation.mutate(f.id)}
                    onPlateGallery={setGalleryFile}
                    onMove={setMoveFile}
                    onTags={(f, anchor) => setTagsPopover({ file: f, anchor })}
                    onTagClick={toggleTagFilter}
                    thumbnailVersion={thumbnailVersions[file.id]}
                    isRegeneratingThumbnail={regeneratingFileId === file.id}
                    hasPermission={hasPermission}
                    canModify={canModify}
                    authEnabled={authEnabled}
                    timeFormat={timeFormat}
                    dateFormat={dateFormat}
                  />
                ))}
              </div>
            </div>
          ) : (
            <div className="flex-1 lg:overflow-y-auto">
              {/* Outer grid carries the column-template; header + every row
                  use ``grid-cols-subgrid`` so they share track widths. The
                  Actions column is therefore sized to the WIDEST row's
                  buttons (e.g. a sliced .gcode.3mf with all of: project,
                  notes, print, schedule, plate gallery, 3D, menu) — all
                  other rows then use that same width and align cleanly. */}
              <div
                className={`grid ${
                  authEnabled
                    ? 'grid-cols-[auto_minmax(0,1fr)_max-content_max-content_max-content_max-content_max-content]'
                    : 'grid-cols-[auto_minmax(0,1fr)_max-content_max-content_max-content_max-content]'
                } bg-bambu-dark-secondary rounded-lg border border-bambu-dark-tertiary`}
              >
                {/* List header - hidden on mobile, show simplified on small screens */}
                <div className="hidden sm:grid grid-cols-subgrid col-span-full gap-4 px-4 py-2 bg-bambu-dark-secondary border-b border-bambu-dark-tertiary text-xs text-bambu-gray font-medium">
                  <div className="w-6" />
                  <div className="text-center">{t('common.name')}</div>
                  {authEnabled && <div className="text-center">{t('fileManager.uploadedBy', { defaultValue: 'Uploaded By' })}</div>}
                  <div className="text-center">{t('common.type')}</div>
                  <div className="text-center">{t('fileManager.size')}</div>
                  <div className="text-center">{t('common.date')}</div>
                  <div className="text-center">{t('archives.list.actions')}</div>
                </div>
                {/* List rows */}
                {filteredAndSortedFiles.map((file) => (
                  <div
                    key={file.id}
                    data-file-row
                    className={`grid grid-cols-subgrid col-span-full gap-4 px-4 py-3 items-center border-b border-bambu-dark-tertiary last:border-b-0 hover:bg-bambu-dark/50 transition-colors ${
                      selectedFiles.includes(file.id) ? 'bg-bambu-green/10' : ''
                    }`}
                  >
                    {/* Checkbox — the only select affordance (a plain row
                        click no longer toggles selection). */}
                    <button
                      type="button"
                      data-select-file
                      onClick={(e) => { e.stopPropagation(); handleFileSelect(file.id); }}
                      aria-pressed={selectedFiles.includes(file.id)}
                      aria-label={t('fileManager.selectFile', { defaultValue: 'Select file' })}
                      className={`w-5 h-5 rounded border-2 flex items-center justify-center cursor-pointer ${
                        selectedFiles.includes(file.id)
                          ? 'bg-bambu-green border-bambu-green'
                          : 'border-bambu-gray/50'
                      }`}
                    >
                      {selectedFiles.includes(file.id) && <div className="w-2 h-2 bg-white rounded-sm" />}
                    </button>
                    {/* Name with thumbnail */}
                    <div className="flex items-center gap-3 min-w-0">
                      <div className="relative group/thumb">
                        <div className="relative w-10 h-10 rounded bg-bambu-dark flex-shrink-0 overflow-hidden">
                          {file.thumbnail_path ? (
                            <img
                              src={`${api.getLibraryFileThumbnailUrl(file.id)}${thumbnailVersions[file.id] ? `?v=${thumbnailVersions[file.id]}` : ''}`}
                              alt=""
                              className="w-full h-full object-contain"
                            />
                          ) : (
                            <div className="w-full h-full flex items-center justify-center">
                              <FileBox className="w-5 h-5 text-bambu-gray/50" />
                            </div>
                          )}
                          {/* Regen overlay — list-mode variant; smaller
                              spinner (w-4 h-4) to fit the 40px thumb. */}
                          {regeneratingFileId === file.id && (
                            <div className="absolute inset-0 flex items-center justify-center bg-bambu-dark/70 backdrop-blur-sm pointer-events-none">
                              <Loader2 className="w-4 h-4 text-bambu-green animate-spin" />
                            </div>
                          )}
                        </div>
                        {/* Hover preview — popup's top-left corner anchors at
                            the thumbnail's bottom-right 1/3 point (i.e. 2/3
                            down and 2/3 right of the thumbnail). The popup
                            then extends down + to the right of that anchor. */}
                        {file.thumbnail_path && (
                          <div className="absolute top-2/3 left-2/3 z-50 hidden group-hover/thumb:block">
                            <div className="w-48 h-48 rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary shadow-xl overflow-hidden">
                              <img
                                src={`${api.getLibraryFileThumbnailUrl(file.id)}${thumbnailVersions[file.id] ? `?v=${thumbnailVersions[file.id]}` : ''}`}
                                alt={file.filename}
                                className="w-full h-full object-contain"
                              />
                            </div>
                          </div>
                        )}
                      </div>
                      <div className="min-w-0">
                        <button
                          type="button"
                          onClick={(e) => { e.stopPropagation(); handleOpenArchives(file); }}
                          title={t('fileManager.viewPrintsOf', { name: file.print_name || file.filename })}
                          className="block w-full text-sm text-white truncate text-left hover:text-bambu-green hover:underline transition-colors cursor-pointer"
                        >
                          {file.print_name || file.filename}
                        </button>
                        {/* Per-file facts sit UNDER the name, the way the
                            archive list carries its small print — they describe
                            this one file, whereas the badge row to the right is
                            the shared tag vocabulary. Keeping them there put
                            two different kinds of thing in one row. */}
                        {((file.object_count != null && file.object_count > 0) || file.print_count > 0) && (
                          <div className="flex items-center gap-2 mt-0.5">
                            {file.object_count != null && file.object_count > 0 && (
                              <button
                                type="button"
                                onClick={(e) => { e.stopPropagation(); setPreviewFileId(file.id); }}
                                className="flex items-center gap-1 text-[11px] text-bambu-gray hover:text-bambu-green transition-colors"
                                title={t('library.plateObjects.open')}
                              >
                                <Box className="w-3 h-3" />
                                {file.object_count}
                                {file.skip_objects_supported && (
                                  <SkipObjectsIcon className="w-3 h-3 text-bambu-green/70" />
                                )}
                              </button>
                            )}
                            {file.print_count > 0 && (
                              <button
                                type="button"
                                onClick={(e) => { e.stopPropagation(); handleOpenArchives(file); }}
                                aria-label={t('fileManager.printedTimes', { count: file.print_count })}
                                title={t('fileManager.printedTimes', { count: file.print_count })}
                                className="flex items-center gap-1 text-[11px] text-bambu-gray hover:text-bambu-green transition-colors"
                              >
                                <History className="w-3 h-3" />
                                {file.print_count}
                              </button>
                            )}
                          </div>
                        )}
                      </div>
                    </div>
                    {/* Uploaded By - only show when auth is enabled */}
                    {authEnabled && (
                      <div className="text-sm text-bambu-gray flex items-center gap-1">
                        {file.created_by_username ? (
                          <>
                            <User className="w-3 h-3" />
                            <span className="truncate">{file.created_by_username}</span>
                          </>
                        ) : (
                          '-'
                        )}
                      </div>
                    )}
                    {/* Composite badge row — same vocabulary + colours as
                        the grid view, just compact and reading
                        left-to-right (the row already scans LTR with
                        the rest of the table columns, so the format
                        chip leads from the left here, opposite of the
                        grid card's right-anchored layout).

                        Tags ONLY. The object and print counts used to sit here
                        too, on the argument that this was "where per-file facts
                        live" — but that conflated two things: a tag says what
                        KIND of file this is (shared vocabulary, and the chip row
                        above filters on it), while a count describes this one
                        file and nothing else. They now read under the name, the
                        way the archive list carries its small print. */}
                    <div className="flex items-center gap-1 flex-wrap">
                      <FileTagBadges tags={file.file_tags} compact direction="ltr" />
                      {/* #1268 — user-authored tag chips, inline in the same
                          cell (NOT a new subgrid column). Green pills, click
                          toggles the cross-cutting filter. */}
                      {(file.tags ?? []).map((tag) => (
                        <button
                          key={tag.id}
                          type="button"
                          onClick={(e) => { e.stopPropagation(); toggleTagFilter(tag.id); }}
                          className="text-[11px] px-1.5 py-0.5 rounded-full bg-bambu-green/15 text-bambu-green border border-bambu-green/30 hover:bg-bambu-green/25 transition-colors max-w-full truncate"
                          title={tag.name}
                        >
                          {tag.name}
                        </button>
                      ))}
                    </div>
                    {/* Size — right-aligned, same convention as the
                        Archives list. */}
                    <div className="text-sm text-bambu-gray text-right">{formatFileSize(file.file_size)}</div>
                    {/* Date */}
                    <div className="text-sm text-bambu-gray truncate">{formatDateTime(fileActivityAt(file), timeFormat, dateFormat)}</div>
                    {/* Actions — right-aligned within the column. When more
                        buttons appear (e.g. swap-mode adds Layers + Box for
                        a sliced .gcode.3mf), the row grows to the LEFT
                        instead of pushing the whole column wider. */}
                    <div className="flex items-center justify-end gap-1" onClick={(e) => e.stopPropagation()}>
                      {/* Plate gallery — leftmost so the eye scans the row
                          left-to-right with the most "what's inside" action
                          first. Multi-plate 3MFs (sliced or unsliced
                          MakerWorld/project imports) qualify; matches grid
                          view's overlay condition. */}
                      {isMultiPlate(file) && (
                        <button
                          onClick={() => setGalleryFile(file)}
                          className="p-1.5 rounded transition-colors hover:bg-bambu-dark text-bambu-gray hover:text-bambu-green"
                          title={t('fileManager.plateGallery')}
                        >
                          <Layers className="w-4 h-4" />
                        </button>
                      )}
                      {/* Project link / unlink — sits with the other inline actions */}
                      {(file.project_ids ?? []).length > 0 ? (
                        <button
                          onClick={() => setLinkFile(file)}
                          className="p-1.5 rounded bg-blue-500/20 hover:bg-blue-500/30 flex items-center gap-1 transition-colors"
                          title={t('fileManager.linkedToNProjects', { count: file.project_ids.length })}
                        >
                          <Link2 className="w-4 h-4 text-blue-600 dark:text-blue-400" />
                          <Briefcase className="w-3.5 h-3.5 text-blue-600 dark:text-blue-400" />
                          {file.project_ids.length > 1 && (
                            <span className="text-[10px] font-semibold text-blue-700 dark:text-blue-400">
                              ×{file.project_ids.length}
                            </span>
                          )}
                        </button>
                      ) : canModify('library', 'update', file.created_by_id) ? (
                        <button
                          onClick={() => setLinkFile(file)}
                          className="p-1.5 rounded transition-colors hover:bg-bambu-dark text-bambu-gray hover:text-bambu-green"
                          title={t('fileManager.linkToProject')}
                        >
                          <Link2 className="w-4 h-4" />
                        </button>
                      ) : null}
                      {/* Notes — always available, matches grid view's
                          overlay button (line 1029). MakerWorld imports and
                          unsliced project 3MFs deserve notes too. */}
                      <LibraryFileNotesButton fileId={file.id} initialCount={file.notes_count} variant="inline" />
                      {/* Print + Schedule — gated on isPrintable because they
                          send G-code to a printer. Unsliced 3MFs go through
                          the slice modal first (separate button further down). */}
                      {isPrintable(file) && (
                        <>
                          <button
                            onClick={() => hasPermission('printers:control') && setPrintFile(file)}
                            className={`p-1.5 rounded transition-colors ${
                              hasPermission('printers:control')
                                ? 'hover:bg-bambu-dark text-bambu-gray hover:text-bambu-green'
                                : 'text-bambu-gray/50 cursor-not-allowed'
                            }`}
                            title={hasPermission('printers:control') ? t('common.print') : t('fileManager.noPermissionPrint')}
                            disabled={!hasPermission('printers:control')}
                          >
                            <Printer className="w-4 h-4" />
                          </button>
                          <button
                            onClick={() => hasPermission('queue:create') && scheduleOne(file)}
                            className={`p-1.5 rounded transition-colors ${
                              hasPermission('queue:create')
                                ? 'hover:bg-bambu-dark text-bambu-gray hover:text-bambu-green'
                                : 'text-bambu-gray/50 cursor-not-allowed'
                            }`}
                            title={hasPermission('queue:create') ? t('fileManager.schedulePrint') : t('fileManager.noPermissionAddToQueue')}
                            disabled={!hasPermission('queue:create')}
                          >
                            <Clock className="w-4 h-4" />
                          </button>
                        </>
                      )}
                      {(file.file_type === '3mf' || file.file_type === 'gcode' || file.file_type === 'stl' || file.file_type === 'obj') && (
                        <button
                          onClick={() => hasPermission('library:read') && setViewerFile(file)}
                          className={`p-1.5 rounded transition-colors ${
                            hasPermission('library:read')
                              ? 'hover:bg-bambu-dark text-bambu-gray hover:text-bambu-green'
                              : 'text-bambu-gray/50 cursor-not-allowed'
                          }`}
                          title={hasPermission('library:read') ? t('fileManagerModal.threeView') : undefined}
                          disabled={!hasPermission('library:read')}
                        >
                          <Box className="w-4 h-4" />
                        </button>
                      )}
                      <FileListActions
                        file={file}
                        t={t}
                        hasPermission={hasPermission}
                        canModify={canModify}
                        onPrint={setPrintFile}
                        onSchedule={scheduleOne}
                        onSlice={setSliceFile}
                        onOpenInSlicer={handleOpenInSlicer}
                        useSlicerApi={settings?.use_slicer_api ?? false}
                        onPreview3d={setViewerFile}
                            onDownload={handleDownload}
                        onRename={(f) => setRenameItem({ type: 'file', id: f.id, name: f.filename })}
                        onGenerateThumbnail={(f) => singleThumbnailMutation.mutate(f.id)}
                        onMove={setMoveFile}
                        onTags={(f, anchor) => setTagsPopover({ file: f, anchor })}
                        onDelete={(id) => setDeleteConfirm({ type: 'file', id })}
                      />
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
          {/* Paging — same PaginationBar component as the Archives list,
              under the rows rather than above them. */}
          {meta && (
            <PaginationBar
              page={meta.current_page}
              totalPages={meta.last_page}
              perPage={perPage}
              total={meta.total}
              items={t('fileManager.fileCount', { count: meta.total })}
              variant="bare"
              // Default 50 isn't in PaginationBar's own [12,24,48,96] — the
              // select would render with nothing selected on first load.
              // 200 matches the backend's `per_page` upper bound (le=200).
              perPageOptions={[25, 50, 100, 200]}
              onPageChange={setPage}
              onPerPageChange={(size) => {
                setPerPage(size);
                setPage(1);
              }}
            />
          )}
        </div>

        {/* README rail (#2520) — a collapsible right-hand column of the same
            row as the folder tree and the file list, so the markdown sits
            BESIDE the files instead of above them; as a full-width block it
            pushed the model cards below the fold. Stacks on top on narrow
            screens via `order-first`. Auto-hides when the folder has no
            markdown, so folders without one pay no UI cost (#1268). */}
        {selectedFolder && <FolderReadmePanel folderId={selectedFolder.id} />}
      </div>

      {/* Modals */}
      {/* Page-level rather than per-row: the list view renders rows inline in
          this component's map, so there is no row component to hold the state
          the way FileCard does for the grid. */}
      {previewFileId != null && (
        <PlateObjectsPreviewModal
          source="library"
          id={previewFileId}
          isOpen
          onClose={() => setPreviewFileId(null)}
        />
      )}
      {galleryFile && (
        <LibraryPlateGalleryModal
          fileId={galleryFile.id}
          filename={galleryFile.print_name || galleryFile.filename}
          onClose={() => setGalleryFile(null)}
        />
      )}
      {showNewFolderModal && (
        <NewFolderModal
          // Inside a WRITABLE external the backend creates a real directory
          // on the share (mirroring upload semantics). A READ-ONLY external
          // cannot take one, so the folder goes to the library root and the
          // modal says so — the freshly linked external is auto-selected,
          // which is exactly how folders used to land somewhere unnoticed.
          parentId={selectedFolder?.is_external && selectedFolder.external_readonly ? null : selectedFolderId}
          parentName={
            selectedFolder?.is_external && selectedFolder.external_readonly
              ? null
              : (selectedFolder?.name ?? null)
          }
          externalRedirected={!!(selectedFolder?.is_external && selectedFolder.external_readonly)}
          onClose={() => setShowNewFolderModal(false)}
          onSave={(data) => createFolderMutation.mutate(data)}
          isLoading={createFolderMutation.isPending}
          t={t}
        />
      )}

      {showExternalFolderModal && (
        <ExternalFolderModal
          onClose={() => setShowExternalFolderModal(false)}
          onSave={(data) => createExternalFolderMutation.mutate(data)}
          isLoading={createExternalFolderMutation.isPending}
          t={t}
        />
      )}

      {showMoveModal && folders && (
        <MoveFilesModal
          folders={folders}
          selectedFiles={selectedFiles}
          currentFolderId={selectedFolderId}
          onClose={() => setShowMoveModal(false)}
          onMove={(folderId) => moveFilesMutation.mutate({ fileIds: selectedFiles, folderId })}
          isLoading={moveFilesMutation.isPending}
          t={t}
        />
      )}

      {/* Same dialog, one file. Reusing it rather than building a single-file
          twin keeps one definition of "where can this go". */}
      {moveFile && folders && (
        <MoveFilesModal
          folders={folders}
          selectedFiles={[moveFile.id]}
          currentFolderId={selectedFolderId}
          onClose={() => setMoveFile(null)}
          onMove={(folderId) => {
            moveFilesMutation.mutate({ fileIds: [moveFile.id], folderId });
            setMoveFile(null);
          }}
          isLoading={moveFilesMutation.isPending}
          t={t}
        />
      )}

      {tagsPopover && (
        <FileTagsPopover
          file={tagsPopover.file}
          anchor={tagsPopover.anchor}
          onClose={() => setTagsPopover(null)}
        />
      )}

      {showUploadModal && (
        <FileUploadModal
          folderId={selectedFolderId}
          onClose={() => {
            setShowUploadModal(false);
            setDroppedFiles([]);
          }}
          onUploadComplete={handleUploadComplete}
          initialFiles={droppedFiles.length > 0 ? droppedFiles : undefined}
        />
      )}

      {showPurgeModal && (
        <PurgeOldFilesModal onClose={() => setShowPurgeModal(false)} />
      )}

      {/* #1268 — user-authored tag catalog CRUD + bulk-tag picker. */}
      <LibraryTagsModal
        open={showTagsModal}
        onClose={() => setShowTagsModal(false)}
      />
      <BulkTagsPickerModal
        open={showBulkTagsModal}
        fileIds={selectedFiles}
        onClose={() => setShowBulkTagsModal(false)}
      />

      {linkFolder && (
        <LinkFolderModal
          folder={linkFolder}
          onClose={() => setLinkFolder(null)}
          onLink={(data) => updateFolderMutation.mutate({ id: linkFolder.id, data })}
          isLoading={updateFolderMutation.isPending}
          t={t}
        />
      )}

      {linkFile && (
        <LinkFileModal
          file={linkFile}
          onClose={() => setLinkFile(null)}
          onLink={(data) => linkFileMutation.mutate({ id: linkFile.id, data })}
          isLoading={linkFileMutation.isPending}
          t={t}
        />
      )}

      {deleteConfirm && (
        <ConfirmModal
          title={
            deleteConfirm.type === 'folder'
              ? t('fileManager.deleteFolder')
              : deleteConfirm.type === 'bulk'
              ? t('fileManager.deleteFilesCount', { count: deleteConfirm.count })
              : t('fileManager.deleteFile')
          }
          message={
            deleteConfirm.type === 'folder'
              ? t('fileManager.deleteFolderConfirm')
              : deleteConfirm.type === 'bulk'
              ? t('fileManager.deleteFilesConfirm', { count: deleteConfirm.count })
              : t('fileManager.deleteFileConfirm')
          }
          confirmText={t('common.delete')}
          variant="danger"
          isLoading={isDeleting}
          loadingText={t('fileManager.deleting')}
          onConfirm={handleDeleteConfirm}
          onCancel={() => setDeleteConfirm(null)}
        />
      )}

      {printFile && (
        <PrintModal
          mode="reprint"
          libraryFileId={printFile.id}
          archiveName={printFile.print_name || printFile.filename}
          onClose={() => setPrintFile(null)}
          onSuccess={() => {
            setPrintFile(null);
            queryClient.invalidateQueries({ queryKey: ['library-files'] });
            queryClient.invalidateQueries({ queryKey: ['archives'] });
          }}
        />
      )}

      {sliceFile && (
        <SliceModal
          source={{ kind: 'libraryFile', id: sliceFile.id, filename: sliceFile.filename }}
          onClose={() => setSliceFile(null)}
        />
      )}

      {printMultiFile && (
        <PrintModal
          mode="reprint"
          libraryFileId={printMultiFile.id}
          archiveName={printMultiFile.print_name || printMultiFile.filename}
          onClose={() => setPrintMultiFile(null)}
          onSuccess={() => {
            setPrintMultiFile(null);
            setSelectedFiles([]);
            queryClient.invalidateQueries({ queryKey: ['library-files'] });
            queryClient.invalidateQueries({ queryKey: ['archives'] });
          }}
        />
      )}

      {queueSequence && (
        <QueueSequencer
          // The sequencer takes the least a file must say about itself, so the
          // drop zones can feed it a freshly-uploaded {id, name} without
          // inventing a whole list row.
          files={queueSequence.files.map((f) => ({ id: f.id, name: f.print_name || f.filename }))}
          onDone={(remaining) => {
            const { fromSelection } = queueSequence;
            setQueueSequence(null);
            // What is left over stays ticked: the selection is the record of
            // what still has to be distributed. Everything queued → empty.
            if (fromSelection) setSelectedFiles(remaining.map((f) => f.id));
            queryClient.invalidateQueries({ queryKey: ['library-files'] });
            queryClient.invalidateQueries({ queryKey: ['queue'] });
            queryClient.invalidateQueries({ queryKey: ['archives'] });
          }}
        />
      )}

      {viewerFile && (
        <ModelViewerModal
          libraryFileId={viewerFile.id}
          title={viewerFile.print_name || viewerFile.filename}
          fileType={viewerFile.file_type}
          onClose={() => setViewerFile(null)}
          onSliceWithBamDude={
            // Mirror the file-row Cog gate: only offer in-app slicing on a
            // sliceable source the user may upload. ModelViewerModal itself
            // gates on settings.use_slicer_api.
            isSliceable(viewerFile) && hasPermission('library:upload')
              ? () => {
                  const f = viewerFile;
                  setViewerFile(null);
                  setSliceFile(f);
                }
              : undefined
          }
        />
      )}

      {renameItem && (
        <RenameModal
          type={renameItem.type}
          currentName={renameItem.name}
          onClose={() => setRenameItem(null)}
          onSave={(newName) => {
            if (renameItem.type === 'file') {
              renameFileMutation.mutate({ id: renameItem.id, filename: newName });
            } else {
              renameFolderMutation.mutate({ id: renameItem.id, name: newName });
            }
          }}
          isLoading={renameFileMutation.isPending || renameFolderMutation.isPending}
          t={t}
        />
      )}
    </div>
  );
}
