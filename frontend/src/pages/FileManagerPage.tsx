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
  Archive as ArchiveIcon,
  Briefcase,
  Printer,
  Pencil,
  Play,
  Image,
  User,
  Box,
  RefreshCw,
  Lock,
  FolderSymlink,
  WrapText,
  ListCollapse,
  Layers,
  Cog,
} from 'lucide-react';
import { api } from '../api/client';
import type {
  LibraryFolderTree,
  LibraryFileListItem,
  LibraryFileUpdate,
  LibraryFolderCreate,
  LibraryFolderUpdate,
  ExternalFolderCreate,
  AppSettings,
  Archive,
  Permission,
} from '../api/client';
import { Button } from '../components/Button';
import { ConfirmModal } from '../components/ConfirmModal';
import { LibraryPlateGalleryModal } from '../components/LibraryPlateGallery';
import { PrintModal } from '../components/PrintModal';
import { SliceModal } from '../components/SliceModal';
import { ModelViewerModal } from '../components/ModelViewerModal';
import { FileUploadModal } from '../components/FileUploadModal';
import { LibraryFileNotesButton } from '../components/LibraryFileNotesButton';
import { PurgeOldFilesModal } from '../components/PurgeOldFilesModal';
import { TrashSplitButton } from '../components/TrashSplitButton';
import { MakerWorldIcon } from '../components/BrandIcons';
import { useToast } from '../contexts/ToastContext';
import { useIsMobile } from '../hooks/useIsMobile';
import { useAuth } from '../contexts/AuthContext';
import { formatDateTime, formatDuration, parseUTCDate, type TimeFormat, type DateFormat } from '../utils/date';
import { formatFileSize } from '../utils/file';
import { FileTagBadges } from '../components/FileTagBadges';
import { KNOWN_FILE_TAGS, getTagStyle, isSliced, isSliceable, isMultiPlate } from '../lib/fileTags';

type SortField = 'name' | 'date' | 'size' | 'type';
type SortDirection = 'asc' | 'desc';
type TFunction = (key: string, options?: Record<string, unknown>) => string;

// New Folder Modal
interface NewFolderModalProps {
  parentId: number | null;
  onClose: () => void;
  onSave: (data: LibraryFolderCreate) => void;
  isLoading: boolean;
  t: TFunction;
}

function NewFolderModal({ parentId, onClose, onSave, isLoading, t }: NewFolderModalProps) {
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

function RenameModal({ type, currentName, onClose, onSave, isLoading, t }: RenameModalProps) {
  // For files, separate the extension so users can only edit the base name
  // Handle compound extensions like .gcode.3mf
  const fileExtension = type === 'file' ? (currentName.match(/(\.gcode\.3mf|\.3mf|\.gcode)$/i)?.[1] ?? '') : '';
  const baseName = type === 'file' && fileExtension ? currentName.slice(0, -fileExtension.length) : currentName;
  const [name, setName] = useState(baseName);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
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
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" variant="secondary" onClick={onClose}>
              {t('common.cancel')}
            </Button>
            <Button type="submit" disabled={!name.trim() || name.trim() === baseName || isLoading}>
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

  const flattenFolders = (items: LibraryFolderTree[], depth = 0): { id: number | null; name: string; depth: number }[] => {
    const result: { id: number | null; name: string; depth: number }[] = [];
    for (const item of items) {
      result.push({ id: item.id, name: item.name, depth });
      if (item.children.length > 0) {
        result.push(...flattenFolders(item.children, depth + 1));
      }
    }
    return result;
  };

  const flatFolders = [{ id: null, name: t('fileManager.rootNoFolder'), depth: 0 }, ...flattenFolders(folders)];

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4">
      <div className="bg-bambu-dark-secondary rounded-lg w-full max-w-sm border border-bambu-dark-tertiary">
        <div className="p-4 border-b border-bambu-dark-tertiary">
          <h2 className="text-lg font-semibold text-white">{t('fileManager.moveFiles', { count: selectedFiles.length })}</h2>
        </div>
        <div className="p-4 space-y-4">
          <div className="max-h-64 overflow-y-auto space-y-1">
            {flatFolders.map((folder) => (
              <button
                key={folder.id ?? 'root'}
                onClick={() => setTargetFolder(folder.id)}
                disabled={folder.id === currentFolderId}
                className={`w-full text-left px-3 py-2 rounded transition-colors flex items-center gap-2 ${
                  targetFolder === folder.id
                    ? 'bg-bambu-green/20 text-bambu-green'
                    : folder.id === currentFolderId
                    ? 'opacity-50 cursor-not-allowed text-bambu-gray'
                    : 'hover:bg-bambu-dark text-white'
                }`}
                style={{ paddingLeft: `${12 + folder.depth * 16}px` }}
              >
                <FolderOpen className="w-4 h-4" />
                {folder.name}
                {folder.id === currentFolderId && <span className="text-xs text-bambu-gray ml-auto">({t('fileManager.current')})</span>}
              </button>
            ))}
          </div>
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
  // m044: folder ↔ projects is M2M; archive stays single-link.
  // Mode toggles which surface the operator wants to edit; the modal
  // submits both halves of the state in one PUT.
  const [linkType, setLinkType] = useState<'project' | 'archive'>(
    folder.archive_id ? 'archive' : 'project',
  );
  const [selectedProjectIds, setSelectedProjectIds] = useState<Set<number>>(
    () => new Set(folder.projects.map((p) => p.id)),
  );
  const [selectedArchiveId, setSelectedArchiveId] = useState<number | null>(folder.archive_id);

  const { data: projects } = useQuery({
    queryKey: ['projects'],
    queryFn: () => api.getProjects(),
  });

  const { data: archives } = useQuery({
    queryKey: ['archives-for-link'],
    queryFn: () => api.getArchives({ per_page: 100 }),
  });

  const toggleProject = (projectId: number) => {
    setSelectedProjectIds((prev) => {
      const next = new Set(prev);
      if (next.has(projectId)) next.delete(projectId);
      else next.add(projectId);
      return next;
    });
  };

  const handleSave = () => {
    if (linkType === 'project') {
      // Replace the project list; leave archive untouched. Per-project
      // unlink happens by deselecting individual chips above; the
      // legacy "wipe everything" red button is gone.
      onLink({ project_ids: Array.from(selectedProjectIds) });
    } else {
      // Archive is single-link; clearing the selection (× button on the
      // active-archive row) sends archive_id=0.
      onLink({ archive_id: selectedArchiveId ?? 0 });
    }
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

          {/* Link type selector */}
          <div className="flex gap-2">
            <button
              onClick={() => setLinkType('project')}
              className={`flex-1 flex items-center justify-center gap-2 px-3 py-2 rounded-lg border transition-colors ${
                linkType === 'project'
                  ? 'border-bambu-green bg-bambu-green/10 text-bambu-green'
                  : 'border-bambu-dark-tertiary text-bambu-gray hover:text-white'
              }`}
            >
              <Briefcase className="w-4 h-4" />
              {t('fileManager.project')}
            </button>
            <button
              onClick={() => setLinkType('archive')}
              className={`flex-1 flex items-center justify-center gap-2 px-3 py-2 rounded-lg border transition-colors ${
                linkType === 'archive'
                  ? 'border-bambu-green bg-bambu-green/10 text-bambu-green'
                  : 'border-bambu-dark-tertiary text-bambu-gray hover:text-white'
              }`}
            >
              <ArchiveIcon className="w-4 h-4" />
              {t('fileManager.archive')}
            </button>
          </div>

          {linkType === 'project' ? (
            // Chip multi-select. Each project is a clickable colored chip;
            // selected = full color + check, unselected = outline only.
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
          ) : (
            <>
              {/* Currently linked archive — surfaced above the picker so the
                  per-link unlink affordance (× clears the selection) is
                  obvious without scrolling through the whole archive list. */}
              {selectedArchiveId != null && (
                <div className="flex items-center justify-between gap-2 bg-bambu-dark rounded-lg px-3 py-2">
                  <div className="flex items-center gap-2 text-sm text-white truncate">
                    <FileBox className="w-4 h-4 text-bambu-gray flex-shrink-0" />
                    <span className="truncate">
                      {archives?.data.find((a: Archive) => a.id === selectedArchiveId)?.print_name
                        ?? archives?.data.find((a: Archive) => a.id === selectedArchiveId)?.filename
                        ?? `#${selectedArchiveId}`}
                    </span>
                  </div>
                  <button
                    type="button"
                    onClick={() => setSelectedArchiveId(null)}
                    className="p-1 rounded hover:bg-bambu-dark-tertiary text-bambu-gray hover:text-red-400"
                    title={t('fileManager.unlink')}
                  >
                    <X className="w-4 h-4" />
                  </button>
                </div>
              )}
              <div className="max-h-64 overflow-y-auto space-y-1 bg-bambu-dark rounded-lg p-2">
                {archives?.data && archives.data.length > 0 ? (
                  archives.data.map((archive: Archive) => (
                    <button
                      key={archive.id}
                      onClick={() => setSelectedArchiveId(archive.id)}
                      className={`w-full text-left px-3 py-2 rounded transition-colors flex items-center gap-2 ${
                        selectedArchiveId === archive.id
                          ? 'bg-bambu-green/20 text-bambu-green'
                          : 'hover:bg-bambu-dark-tertiary text-white'
                      }`}
                    >
                      <FileBox className="w-4 h-4 text-bambu-gray flex-shrink-0" />
                      <span className="truncate">{archive.print_name || archive.filename}</span>
                    </button>
                  ))
                ) : (
                  <p className="text-sm text-bambu-gray text-center py-4">{t('fileManager.noArchivesFound')}</p>
                )}
              </div>
            </>
          )}
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

// Link File Modal — per-file project link (simpler than folder: files have no archive_id)
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

  const { data: projects } = useQuery({
    queryKey: ['projects'],
    queryFn: () => api.getProjects(),
  });

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
}

function FolderTreeItem({ folder, selectedFolderId, onSelect, onDelete, onLink, onRename, depth = 0, wrapNames = false, defaultExpanded = true, hasPermission, t }: FolderTreeItemProps) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const [showActions, setShowActions] = useState(false);
  const hasChildren = folder.children.length > 0;
  // m044: M2M projects + optional single archive.
  const isLinked = folder.projects.length > 0 || folder.archive_id != null;
  const isExternal = folder.is_external;

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
          <FolderSymlink className="w-4 h-4 text-purple-400 flex-shrink-0" />
        ) : (
          <FolderOpen className="w-4 h-4 text-bambu-green flex-shrink-0" />
        )}
        <span className={`text-sm flex-1 min-w-0 ${wrapNames ? 'break-all' : 'truncate'}`} title={folder.name}>{folder.name}</span>
        {/* Read-only indicator for external folders — non-interactive
            metadata, kept adjacent to the name. */}
        {isExternal && folder.external_readonly && (
          <span title={t('fileManager.readOnly')}>
            <Lock className="w-3 h-3 text-amber-400 flex-shrink-0" />
          </span>
        )}
        {/* Order across all rows is strictly: link/unlink → count → menu,
            so the count sits right next to the three-dots trigger and the
            row's right edge stays vertically aligned regardless of whether
            the folder is linked, external, or empty. */}
        {isLinked ? (
          <button
            onClick={(e) => { e.stopPropagation(); onLink(folder); }}
            className="flex-shrink-0 flex items-center gap-1 text-xs px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-400 hover:bg-blue-500/30 transition-colors"
            title={
              folder.projects.length > 0
                ? folder.projects.map(p => p.name).join(', ')
                : folder.archive_name
                  ? `Archive: ${folder.archive_name}`
                  : ''
            }
          >
            <Link2 className="w-3 h-3" />
            {folder.projects.length > 0 ? (
              <>
                <Briefcase className="w-3 h-3" />
                {folder.projects.length > 1 && (
                  <span className="text-[10px] font-semibold">×{folder.projects.length}</span>
                )}
              </>
            ) : (
              <ArchiveIcon className="w-3 h-3" />
            )}
          </button>
        ) : !isExternal ? (
          <button
            onClick={(e) => { e.stopPropagation(); onLink(folder); }}
            className="flex-shrink-0 p-1 rounded hover:bg-bambu-dark-tertiary"
            title={t('fileManager.linkToProjectOrArchive')}
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
                <button
                  className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${
                    hasPermission('library:delete_all') ? 'text-red-400 hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'
                  }`}
                  onClick={() => { if (hasPermission('library:delete_all')) { onDelete(folder.id); setShowActions(false); } }}
                  disabled={!hasPermission('library:delete_all')}
                  title={!hasPermission('library:delete_all') ? t('fileManager.noPermissionDeleteFolder') : undefined}
                >
                  <Trash2 className="w-3.5 h-3.5" />
                  {t('common.delete')}
                </button>
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
            />
          ))}
        </div>
      )}
    </div>
  );
}

// Slice-related predicates moved to ``lib/fileTags`` so FileCard /
// FileListActions / ProjectDetailPage / bulk-action handlers all read
// from the same ``file_tags`` source. ``isSliced(file)`` /
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
  useSlicerApi?: boolean;
  onPreview3d?: (file: LibraryFileListItem) => void;
  onRename?: (file: LibraryFileListItem) => void;
  onLink?: (file: LibraryFileListItem) => void;
  onGenerateThumbnail?: (file: LibraryFileListItem) => void;
  onPlateGallery?: (file: LibraryFileListItem) => void;
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

function FileListActions({ file, t, hasPermission, canModify, onPrint, onSchedule, onSlice, useSlicerApi, onPreview3d, onDownload, onRename, onGenerateThumbnail, onDelete }: {
  file: LibraryFileListItem;
  t: TFunction;
  hasPermission: (permission: Permission) => boolean;
  canModify: (resource: 'queue' | 'archives' | 'library', action: 'update' | 'delete' | 'reprint', createdById: number | null | undefined) => boolean;
  onPrint: (f: LibraryFileListItem) => void;
  onSchedule: (f: LibraryFileListItem) => void;
  onSlice?: (f: LibraryFileListItem) => void;
  useSlicerApi?: boolean;
  onPreview3d: (f: LibraryFileListItem) => void;
  onDownload: (id: number) => void;
  onRename: (f: LibraryFileListItem) => void;
  onGenerateThumbnail: (f: LibraryFileListItem) => void;
  onDelete: (id: number) => void;
}) {
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
            {isSliced(file) && (
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
            {onSlice && useSlicerApi && isSliceable(file) && (
              <button
                className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${hasPermission('library:upload') ? 'text-white hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'}`}
                onClick={() => { if (hasPermission('library:upload')) { onSlice(file); setOpen(false); } }}
                disabled={!hasPermission('library:upload')}
                title={!hasPermission('library:upload') ? t('fileManager.noPermissionSlice', { defaultValue: 'You do not have permission to slice' }) : undefined}
              >
                <Cog className="w-3.5 h-3.5" />
                {t('slice.action', { defaultValue: 'Slice' })}
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
              className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${canModify('library', 'delete', file.created_by_id) ? 'text-red-400 hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'}`}
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

function FileCard({ file, isSelected, isMobile, onSelect, onOpenArchives, onDelete, onDownload, onAddToQueue, onPrint, onSlice, useSlicerApi, onPreview3d, onRename, onLink, onGenerateThumbnail, onPlateGallery, thumbnailVersion, isRegeneratingThumbnail, hasPermission, canModify, authEnabled, timeFormat, dateFormat, t }: FileCardProps) {
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
            <span
              className="flex items-center gap-1"
              title={file.object_count === 1
                ? t('archives.card.object', { count: file.object_count })
                : t('archives.card.objects', { count: file.object_count })}
            >
              <Box className="w-3 h-3" />
              {file.object_count}
            </span>
          )}
        </div>
        {file.sliced_for_model && (
          <div className="mt-1 text-xs text-bambu-gray flex items-center gap-1">
            <Printer className="w-3 h-3" />
            {file.sliced_for_model}
          </div>
        )}
        <div className="mt-1 text-xs text-bambu-gray truncate">
          {formatDateTime(file.created_at, timeFormat, dateFormat)}
        </div>
        {authEnabled && file.created_by_username && (
          <div className="mt-0.5 text-xs text-bambu-gray flex items-center gap-1">
            <User className="w-3 h-3" />
            {file.created_by_username}
          </div>
        )}
      </div>

      {/* Actions - always visible on mobile, hover on desktop */}
      <div className={`absolute bottom-2 right-2 transition-opacity ${isMobile ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'}`} onClick={(e) => e.stopPropagation()}>
        <button
          ref={triggerRef}
          onClick={() => setShowActions(!showActions)}
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
              {onPrint && isSliced(file) && (
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
              {onAddToQueue && isSliced(file) && (
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
              {onSlice && useSlicerApi && isSliceable(file) && (
                <button
                  className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 ${
                    hasPermission('library:upload') ? 'text-white hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'
                  }`}
                  onClick={() => { if (hasPermission('library:upload')) { onSlice(file); setShowActions(false); } }}
                  disabled={!hasPermission('library:upload')}
                  title={!hasPermission('library:upload') ? t('fileManager.noPermissionSlice', { defaultValue: 'You do not have permission to slice' }) : undefined}
                >
                  <Cog className="w-3.5 h-3.5" />
                  {t('slice.action', { defaultValue: 'Slice' })}
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
                  canModify('library', 'delete', file.created_by_id) ? 'text-red-400 hover:bg-bambu-dark' : 'text-bambu-gray cursor-not-allowed'
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
    </div>
  );
}

export function FileManagerPage() {
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
  const [selectedFiles, setSelectedFiles] = useState<number[]>([]);
  const [showNewFolderModal, setShowNewFolderModal] = useState(false);
  const [showExternalFolderModal, setShowExternalFolderModal] = useState(false);
  const [showMoveModal, setShowMoveModal] = useState(false);
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
  const [scheduleFile, setScheduleFile] = useState<LibraryFileListItem | null>(null);
  const [sliceFile, setSliceFile] = useState<LibraryFileListItem | null>(null);
  const [renameItem, setRenameItem] = useState<{ type: 'file' | 'folder'; id: number; name: string } | null>(null);
  const [thumbnailVersions, setThumbnailVersions] = useState<Record<number, number>>({});
  const [viewerFile, setViewerFile] = useState<LibraryFileListItem | null>(null);
  // Per-plate gallery modal — opened from list-mode "plates" button. Null when closed.
  const [galleryFile, setGalleryFile] = useState<LibraryFileListItem | null>(null);

  const [viewMode, setViewMode] = useState<'grid' | 'list'>(() => {
    return (localStorage.getItem('library-view-mode') as 'grid' | 'list') || 'grid';
  });
  const [wrapFolderNames, setWrapFolderNames] = useState(() => {
    return localStorage.getItem('library-wrap-folders') === 'true';
  });
  const [collapseFoldersByDefault, setCollapseFoldersByDefault] = useState(() => {
    return localStorage.getItem('library-collapse-folders') === 'true';
  });

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
  const [filterType, setFilterType] = useState<string>('all');
  // Tag chip-row — additive on top of `filterType`. AND across selected
  // tags so the user can express "sliced multi-plate 3MFs" by activating
  // both `sliced` and `multiplate`. Persisted to localStorage so a
  // power-user's filter survives page reloads.
  const [filterTags, setFilterTags] = useState<string[]>(() => {
    try {
      const raw = localStorage.getItem('library-filter-tags');
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed.filter((t) => typeof t === 'string') : [];
    } catch {
      return [];
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem('library-filter-tags', JSON.stringify(filterTags));
    } catch {
      /* private mode / quota — silently skip */
    }
  }, [filterTags]);
  const [filterUsername, setFilterUsername] = useState('');
  const [sortField, setSortField] = useState<SortField>(() => {
    const saved = localStorage.getItem('library-sort-field');
    return (saved as SortField) || 'name';
  });
  const [sortDirection, setSortDirection] = useState<SortDirection>(() => {
    const saved = localStorage.getItem('library-sort-direction');
    return (saved as SortDirection) || 'asc';
  });

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
  const { data: folders, isLoading: foldersLoading } = useQuery({
    queryKey: ['library-folders'],
    queryFn: () => api.getLibraryFolders(),
  });

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

  const { data: files, isLoading: filesLoading } = useQuery({
    queryKey: ['library-files', selectedFolderId],
    // "All Files" (selectedFolderId === null) lists every file across folders,
    // so include_root must be false — true would scope the result to files at
    // the library root only and hide everything nested in subfolders (#1499).
    queryFn: () => api.getLibraryFiles(selectedFolderId, false),
  });

  const { data: stats } = useQuery({
    queryKey: ['library-stats'],
    queryFn: () => api.getLibraryStats(),
  });

  // Get users for the username filter autocomplete
  const { data: users } = useQuery({
    queryKey: ['users'],
    queryFn: () => api.getUsers(),
  });

  // Get unique file types for filter dropdown
  const fileTypes = useMemo(() => {
    if (!files) return [];
    const types = new Set(files.map((f) => f.file_type));
    return Array.from(types).sort();
  }, [files]);

  // Filter and sort files
  const filteredAndSortedFiles = useMemo(() => {
    if (!files) return [];

    let result = [...files];

    // Apply search filter
    if (searchQuery.trim()) {
      const query = searchQuery.toLowerCase();
      result = result.filter(
        (f) =>
          f.filename.toLowerCase().includes(query) ||
          (f.print_name && f.print_name.toLowerCase().includes(query))
      );
    }

    // Apply type filter
    if (filterType !== 'all') {
      result = result.filter((f) => f.file_type === filterType);
    }
    // Tag chip-row filter — every selected tag must be present (AND).
    // ``file_tags`` defaults to ``[]`` on the server side so older rows
    // before m036 (impossible in practice — backfill runs on upgrade)
    // would simply not match and silently drop. That's intentional: a
    // user with a tag chip active expects to see only tagged rows.
    if (filterTags.length > 0) {
      result = result.filter((f) =>
        filterTags.every((tag) => (f.file_tags ?? []).includes(tag)),
      );
    }

    // Apply username filter
    if (filterUsername.trim()) {
      const query = filterUsername.toLowerCase();
      result = result.filter(
        (f) => f.created_by_username && f.created_by_username.toLowerCase().includes(query)
      );
    }

    // Apply sorting
    result.sort((a, b) => {
      let comparison = 0;
      switch (sortField) {
        case 'name':
          comparison = (a.print_name || a.filename).localeCompare(b.print_name || b.filename);
          break;
        case 'date':
          comparison = (parseUTCDate(a.created_at)?.getTime() ?? 0) - (parseUTCDate(b.created_at)?.getTime() ?? 0);
          break;
        case 'size':
          comparison = a.file_size - b.file_size;
          break;
        case 'type':
          comparison = a.file_type.localeCompare(b.file_type);
          break;
      }
      return sortDirection === 'asc' ? comparison : -comparison;
    });

    return result;
  }, [files, searchQuery, filterType, filterTags, filterUsername, sortField, sortDirection]);

  // Check if disk space is low
  const isDiskSpaceLow = useMemo(() => {
    if (!stats || !settings) return false;
    const thresholdBytes = (settings.library_disk_warning_gb || 5) * 1024 * 1024 * 1024;
    return stats.disk_free_bytes < thresholdBytes;
  }, [stats, settings]);

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
      // Auto-scan after creation
      await api.scanExternalFolder(folder.id);
      return folder;
    },
    onSuccess: (folder) => {
      queryClient.invalidateQueries({ queryKey: ['library-folders'] });
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      queryClient.invalidateQueries({ queryKey: ['library-stats'] });
      setShowExternalFolderModal(false);
      setSelectedFolderId(folder.id);
      showToast(t('fileManager.toast.externalFolderLinked'), 'success');
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const scanExternalFolderMutation = useMutation({
    mutationFn: (folderId: number) => api.scanExternalFolder(folderId),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      queryClient.invalidateQueries({ queryKey: ['library-folders'] });
      queryClient.invalidateQueries({ queryKey: ['library-stats'] });
      showToast(t('fileManager.toast.folderScanned', { added: result.added, removed: result.removed }), 'success');
    },
    onError: (error: Error) => showToast(error.message, 'error'),
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
    onSuccess: (_, fileIds) => {
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      queryClient.invalidateQueries({ queryKey: ['library-folders'] });
      queryClient.invalidateQueries({ queryKey: ['library-stats'] });
      queryClient.invalidateQueries({ queryKey: ['library-trash'] });
      queryClient.invalidateQueries({ queryKey: ['library-trash-count'] });
      showToast(t('fileManager.toast.filesDeleted', { count: fileIds.length }), 'success');
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
      // Invalidate project/archive folder queries so other pages see the update
      queryClient.invalidateQueries({ queryKey: ['project-folders'] });
      queryClient.invalidateQueries({ queryKey: ['archive-folders'] });
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
      // m044: project_ids is an array; treat empty list + cleared
      // archive as a full unlink, otherwise as a link/update.
      const projectsCleared =
        Array.isArray(variables.data.project_ids) && variables.data.project_ids.length === 0;
      const archiveCleared = variables.data.archive_id === 0;
      const isUnlink = projectsCleared && archiveCleared;
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
  // ``file_tags`` via the shared ``isSliced`` helper instead of a
  // local filename-suffix scan, so bulk-print actions agree with the
  // per-row Print button on what counts as "printable".
  const selectedSlicedFiles = useMemo(() => {
    if (!files) return [];
    return files.filter((f) => selectedFiles.includes(f.id) && isSliced(f));
  }, [files, selectedFiles]);

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

  return (
    <div className="p-4 md:p-6 min-h-[calc(100vh-64px)] lg:h-[calc(100vh-64px)] flex flex-col">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-4">
        <div className="flex items-center gap-3">
          <FolderOpen className="w-6 h-6 text-bambu-green" />
          <div>
            <h1 className="text-2xl font-bold text-white">{t('fileManager.title')}</h1>
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
            disabled={!hasPermission('library:upload')}
            title={!hasPermission('library:upload') ? t('fileManager.noPermissionCreateFolder') : undefined}
          >
            <FolderPlus className="w-4 h-4 mr-2" />
            {t('fileManager.newFolder')}
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
            <FolderOpen className="w-4 h-4 text-blue-400" />
            <span className="text-bambu-gray">{t('fileManager.folders')}:</span>
            <span className="text-white font-medium">{stats.total_folders}</span>
          </div>
          <div className="flex items-center gap-2 text-sm">
            <HardDrive className="w-4 h-4 text-amber-400" />
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
            value={selectedFolderId ?? ''}
            onChange={(e) => setSelectedFolderId(e.target.value ? parseInt(e.target.value, 10) : null)}
            className="w-full bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg px-3 py-2.5 text-white focus:outline-none focus:border-bambu-green"
          >
            <option value="">📁 {t('fileManager.allFiles')}</option>
            {folders && (() => {
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
              return flattenFolders(folders).map((folder) => (
                <option key={folder.id} value={folder.id}>
                  {'│ '.repeat(folder.depth)}📂 {folder.name} {folder.fileCount > 0 ? `(${folder.fileCount})` : ''}
                </option>
              ));
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
            {/* All Files (root) */}
            <div
              className={`flex items-center gap-2 px-2 py-1.5 rounded cursor-pointer transition-colors ${
                selectedFolderId === null
                  ? 'bg-bambu-green/20 text-bambu-green'
                  : 'hover:bg-bambu-dark text-white'
              }`}
              onClick={() => setSelectedFolderId(null)}
            >
              <FileBox className="w-4 h-4" />
              <span className="text-sm">{t('fileManager.allFiles')}</span>
            </div>

            {/* Folder tree — re-key on the collapse toggle so flipping it
                remounts every FolderTreeItem, which re-reads defaultExpanded
                and makes the preference take effect immediately. */}
            {folders?.map((folder) => (
              <FolderTreeItem
                key={`${folder.id}-${collapseFoldersByDefault ? 'c' : 'e'}`}
                folder={folder}
                selectedFolderId={selectedFolderId}
                onSelect={setSelectedFolderId}
                onDelete={(id) => setDeleteConfirm({ type: 'folder', id })}
                onLink={setLinkFolder}
                onRename={(f) => setRenameItem({ type: 'folder', id: f.id, name: f.name })}
                wrapNames={wrapFolderNames}
                defaultExpanded={!collapseFoldersByDefault}
                hasPermission={hasPermission}
                t={t}
              />
            ))}
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
            <div className="flex items-center gap-3 mb-4 p-3 bg-purple-500/10 border border-purple-500/30 rounded-lg">
              <FolderSymlink className="w-5 h-5 text-purple-400 flex-shrink-0" />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-purple-300">{t('fileManager.externalFolder')}</span>
                  {selectedFolder.external_readonly && (
                    <span className="text-xs px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-400 flex items-center gap-1">
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
                disabled={scanExternalFolderMutation.isPending}
                title={t('fileManager.scanFolder')}
              >
                {scanExternalFolderMutation.isPending ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <RefreshCw className="w-4 h-4" />
                )}
                <span className="ml-1.5">{t('fileManager.scanFolder')}</span>
              </Button>
            </div>
          )}
          {/* Combined toolbar: search/filters/sort (row 1) + selection actions (row 2) */}
          {files && files.length > 0 && (
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

              {/* Results count */}
              {(searchQuery || filterType !== 'all' || filterUsername) && (
                <span className="h-9 flex items-center text-sm text-bambu-gray hidden sm:inline-flex">
                  {t('fileManager.resultsCount', { showing: filteredAndSortedFiles.length, total: files.length })}
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

            {/* Tag chip-row — additive multi-select on top of the type
                dropdown. Each chip toggles AND-membership so users can
                express "sliced multi-plate 3MFs" by activating both
                ``sliced`` and ``multiplate``. Only renders the chips
                whose tag actually appears on at least one of the loaded
                files — keeps the row tight on installs that don't use
                e.g. MakerWorld. */}
            {(() => {
              const presentTags = new Set<string>();
              for (const f of files ?? []) {
                for (const tag of f.file_tags ?? []) presentTags.add(tag);
              }
              const visibleChips = KNOWN_FILE_TAGS.filter((t) => presentTags.has(t));
              if (visibleChips.length === 0) return null;
              return (
                <div className="flex items-center gap-1 flex-wrap pt-2 border-t border-bambu-dark-tertiary">
                  <span className="text-xs text-bambu-gray mr-1">{t('library.tagFilter')}:</span>
                  {visibleChips.map((tag) => {
                    const active = filterTags.includes(tag);
                    const style = getTagStyle(tag);
                    const label = style ? t(`library.tags.${tag}`, style.label) : tag.toUpperCase();
                    return (
                      <button
                        key={tag}
                        type="button"
                        onClick={() =>
                          setFilterTags((current) =>
                            current.includes(tag)
                              ? current.filter((c) => c !== tag)
                              : [...current, tag],
                          )
                        }
                        className={`text-xs px-2 py-0.5 rounded font-medium transition-colors ${
                          active
                            ? `${style?.bg ?? 'bg-bambu-gray/70'} ${style?.text ?? 'text-white'}`
                            : 'bg-bambu-dark border border-bambu-dark-tertiary text-bambu-gray hover:text-white hover:border-bambu-gray'
                        }`}
                      >
                        {label}
                      </button>
                    );
                  })}
                  {filterTags.length > 0 && (
                    <button
                      type="button"
                      onClick={() => setFilterTags([])}
                      className="text-xs px-2 py-0.5 rounded text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary transition-colors"
                    >
                      {t('library.tagFilterClear')}
                    </button>
                  )}
                </div>
              );
            })()}

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
                    {selectedSlicedFiles.length === 1 && (
                      <Button
                        variant="secondary"
                        size="sm"
                        // Note: Schedule dialog (PrintModal) is designed for single file at a time
                        // but supports scheduling to multiple printers. This provides more control
                        // over scheduling options compared to the previous bulk queue mutation.
                        onClick={() => setScheduleFile(selectedSlicedFiles[0])}
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
          ) : files?.length === 0 ? (
            <div className="flex-1 flex flex-col items-center justify-center">
              <div className="p-4 bg-bambu-dark rounded-2xl mb-4">
                <FileBox className="w-12 h-12 text-bambu-gray/50" />
              </div>
              <h3 className="text-lg font-medium text-white mb-2">
                {selectedFolderId !== null ? t('fileManager.folderIsEmpty') : t('fileManager.noFilesYet')}
              </h3>
              <p className="text-bambu-gray text-center max-w-md mb-6">
                {selectedFolderId !== null
                  ? t('fileManager.folderEmptyDescription')
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
              <Button variant="secondary" onClick={() => { setSearchQuery(''); setFilterType('all'); }}>
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
                      if (file) setScheduleFile(file);
                    }}
                    onPrint={setPrintFile}
                    onSlice={setSliceFile}
                    useSlicerApi={settings?.use_slicer_api ?? false}
                    onPreview3d={setViewerFile}
                    onRename={(f) => setRenameItem({ type: 'file', id: f.id, name: f.filename })}
                    onLink={setLinkFile}
                    onGenerateThumbnail={(f) => singleThumbnailMutation.mutate(f.id)}
                    onPlateGallery={setGalleryFile}
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
                    className={`grid grid-cols-subgrid col-span-full gap-4 px-4 py-3 items-center border-b border-bambu-dark-tertiary last:border-b-0 hover:bg-bambu-dark/50 transition-colors ${
                      selectedFiles.includes(file.id) ? 'bg-bambu-green/10' : ''
                    }`}
                  >
                    {/* Checkbox — the only select affordance (a plain row
                        click no longer toggles selection). */}
                    <button
                      type="button"
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
                        grid card's right-anchored layout). */}
                    <div className="flex items-center gap-1 flex-wrap">
                      <FileTagBadges tags={file.file_tags} compact direction="ltr" />
                    </div>
                    {/* Size — right-aligned, same convention as the
                        Archives list. */}
                    <div className="text-sm text-bambu-gray text-right">{formatFileSize(file.file_size)}</div>
                    {/* Date */}
                    <div className="text-sm text-bambu-gray truncate">{formatDateTime(file.created_at, timeFormat, dateFormat)}</div>
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
                          <Link2 className="w-4 h-4 text-blue-400" />
                          <Briefcase className="w-3.5 h-3.5 text-blue-400" />
                          {file.project_ids.length > 1 && (
                            <span className="text-[10px] font-semibold text-blue-400">
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
                      {/* Print + Schedule — gated on isSliced because they
                          send G-code to a printer. Unsliced 3MFs go through
                          the slice modal first (separate button further down). */}
                      {isSliced(file) && (
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
                            onClick={() => hasPermission('queue:create') && setScheduleFile(file)}
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
                        onSchedule={setScheduleFile}
                        onSlice={setSliceFile}
                        useSlicerApi={settings?.use_slicer_api ?? false}
                        onPreview3d={setViewerFile}
                            onDownload={handleDownload}
                        onRename={(f) => setRenameItem({ type: 'file', id: f.id, name: f.filename })}
                        onGenerateThumbnail={(f) => singleThumbnailMutation.mutate(f.id)}
                        onDelete={(id) => setDeleteConfirm({ type: 'file', id })}
                      />
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Modals */}
      {galleryFile && (
        <LibraryPlateGalleryModal
          fileId={galleryFile.id}
          filename={galleryFile.print_name || galleryFile.filename}
          onClose={() => setGalleryFile(null)}
        />
      )}
      {showNewFolderModal && (
        <NewFolderModal
          parentId={selectedFolderId}
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

      {scheduleFile && (
        <PrintModal
          mode="add-to-queue"
          libraryFileId={scheduleFile.id}
          archiveName={scheduleFile.print_name || scheduleFile.filename}
          onClose={() => setScheduleFile(null)}
          onSuccess={() => {
            setScheduleFile(null);
            setSelectedFiles([]);
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
