import { useState, useRef, useEffect, useCallback, useMemo } from 'react';
import { Link, useSearchParams, useNavigate } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  Download,
  Trash2,
  Clock,
  Package,
  Coins,
  Layers,
  Search,
  Filter,
  Image,
  Box,
  Printer,
  Upload,
  ExternalLink,
  CheckSquare,
  Square,
  X,
  Globe,
  Pencil,
  LayoutGrid,
  List,
  CalendarDays,
  Star,
  Tag,
  StickyNote,
  FolderOpen,
  Calendar,
  AlertCircle,
  Copy,
  Film,
  ScanSearch,
  QrCode,
  Camera,
  FileText,
  FileCode,
  MoreVertical,
  FileSpreadsheet,
  GitCompare,
  GitBranch,
  Loader2,
  FolderKanban,
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
  Settings,
  User,
  Play,
  Zap,
  ArrowUpNarrowWide,
  ArrowDownWideNarrow,
  DownloadCloud,
  Cog,
} from 'lucide-react';
import { MakerWorldIcon } from '../components/BrandIcons';
import { api } from '../api/client';
import { openInSlicer, type SlicerType } from '../utils/slicer';
import { getArchiveStatusBadge } from '../utils/archiveStatus';
import { formatDateTime, formatDateOnly, type TimeFormat, type DateFormat, formatDuration } from '../utils/date';
import { getCurrencySymbol } from '../utils/currency';
import { useIsMobile } from '../hooks/useIsMobile';
import type { Archive, ProjectListItem, ArchiveListParams } from '../api/client';
import { Card, CardContent } from '../components/Card';
import { Button } from '../components/Button';
import { ModelViewerModal } from '../components/ModelViewerModal';
import { PlatePickerModal } from '../components/PlatePickerModal';
import { PrintModal } from '../components/PrintModal';
import type { PlateMetadata } from '../types/plates';
import { SliceModal } from '../components/SliceModal';
import { ConfirmModal } from '../components/ConfirmModal';
import { PurgeArchivesModal } from '../components/PurgeArchivesModal';
import { TrashSplitButton } from '../components/TrashSplitButton';
import { EditArchiveModal } from '../components/EditArchiveModal';
import { ContextMenu, type ContextMenuItem } from '../components/ContextMenu';
import { BatchTagModal } from '../components/BatchTagModal';
import { BatchProjectModal } from '../components/BatchProjectModal';
import { CalendarView } from '../components/CalendarView';
import { QRCodeModal } from '../components/QRCodeModal';
import { PhotoGalleryModal } from '../components/PhotoGalleryModal';
import { ProjectPageModal } from '../components/ProjectPageModal';
import { TimelapseViewer } from '../components/TimelapseViewer';
import { CompareArchivesModal } from '../components/CompareArchivesModal';
import { TagManagementModal } from '../components/TagManagementModal';
import { useToast } from '../contexts/ToastContext';
import { useAuth } from '../contexts/AuthContext';
import { formatFileSize } from '../utils/file';

type TFunction = (key: string, options?: Record<string, unknown>) => string;

/**
 * Check if an archive represents a sliced/printable file.
 * Uses filename (.gcode, .gcode.3mf) as primary check, then falls back to
 * metadata - a .3mf with total_layers or print_time is sliced (contains gcode),
 * while a raw source .3mf (CAD export) has neither.
 */
function isSlicedFile(archive: { filename?: string | null; total_layers?: number | null; print_time_seconds?: number | null }): boolean {
  const filename = archive.filename;
  if (filename) {
    const lower = filename.toLowerCase();
    if (lower.endsWith('.gcode') || lower.includes('.gcode.')) return true;
  }
  // .3mf can be either sliced or source - check for gcode metadata
  if (archive.total_layers || archive.print_time_seconds) return true;
  return false;
}

function getArchiveFileType(filename: string | null | undefined): string | undefined {
  if (!filename) return undefined;
  const lower = filename.toLowerCase();
  if (lower.endsWith('.3mf')) return '3mf';
  if (lower.endsWith('.stl')) return 'stl';
  if (lower.endsWith('.gcode') || lower.includes('.gcode.')) return 'gcode';
  return lower.split('.').pop();
}

// formatDate imported from '../utils/date' - handles UTC conversion

/**
 * Open an archive file in the slicer.
 * Fetches a short-lived download token, then builds a token-authenticated URL
 * that bypasses auth middleware (slicer protocol handlers can't send auth headers).
 */
async function openInSlicerWithToken(
  archiveId: number,
  filename: string,
  resourceType: 'file' | 'source',
  slicer: SlicerType,
): Promise<void> {
  try {
    if (resourceType === 'source') {
      const { token } = await api.createSourceSlicerToken(archiveId);
      const path = api.getSourceSlicerDownloadUrl(archiveId, token, filename);
      openInSlicer(`${window.location.origin}${path}`, slicer);
    } else {
      const { token } = await api.createArchiveSlicerToken(archiveId);
      const path = api.getArchiveSlicerDownloadUrl(archiveId, token, filename);
      openInSlicer(`${window.location.origin}${path}`, slicer);
    }
  } catch {
    // Fallback to direct URL (works when auth is disabled)
    const path = resourceType === 'source'
      ? api.getSource3mfForSlicer(archiveId, filename)
      : api.getArchiveForSlicer(archiveId, filename);
    openInSlicer(`${window.location.origin}${path}`, slicer);
  }
}

function ArchiveCard({
  archive,
  printerName,
  isSelected,
  onSelect,
  selectionMode,
  projects,
  isHighlighted,
  timeFormat = 'system',
  dateFormat = 'system',
  preferredSlicer = 'bambu_studio',
  currency,
  t,
  onNavigateToArchive,
}: {
  archive: Archive;
  printerName: string;
  isSelected: boolean;
  onSelect: (id: number) => void;
  selectionMode: boolean;
  projects: ProjectListItem[] | undefined;
  isHighlighted?: boolean;
  timeFormat?: TimeFormat;
  dateFormat?: DateFormat;
  preferredSlicer?: SlicerType;
  currency: string;
  t: TFunction;
  onNavigateToArchive?: (archiveId: number) => void;
}) {
  // Debug: log when card is highlighted
  if (isHighlighted) {
    console.log('ArchiveCard isHighlighted=true for archive:', archive.id);
  }

  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { hasPermission, canModify } = useAuth();
  const isMobile = useIsMobile();
  const { data: cardSettings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings });
  const useSlicerApi: boolean = !!(cardSettings as Record<string, unknown> | undefined)?.use_slicer_api;
  const [showViewer, setShowViewer] = useState(false);
  const [showReprint, setShowReprint] = useState(false);
  const [showSlice, setShowSlice] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [showEdit, setShowEdit] = useState(false);
  const [showTimelapse, setShowTimelapse] = useState(false);
  const [showTimelapseSelect, setShowTimelapseSelect] = useState(false);
  const [availableTimelapses, setAvailableTimelapses] = useState<Array<{ name: string; path: string; size: number; mtime: string | null }>>([]);
  // PrettyGCode viewer (B.8). null = closed; otherwise the plates to pick from.
  // Single-plate sliced archives skip the modal and navigate straight in;
  // source-only (no sliced gcode) archives surface a noGcode toast instead.
  const [platePickerPlates, setPlatePickerPlates] = useState<PlateMetadata[] | null>(null);
  const navigate = useNavigate();
  const [showQRCode, setShowQRCode] = useState(false);
  const [showPhotos, setShowPhotos] = useState(false);
  const [showProjectPage, setShowProjectPage] = useState(false);
  const [showSchedule, setShowSchedule] = useState(false);
  const [showDeleteSource3mfConfirm, setShowDeleteSource3mfConfirm] = useState(false);
  const [showDeleteF3dConfirm, setShowDeleteF3dConfirm] = useState(false);
  const [showDeleteTimelapseConfirm, setShowDeleteTimelapseConfirm] = useState(false);
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number } | null>(null);
  const [currentPlateIndex, setCurrentPlateIndex] = useState<number | null>(null);
  const [showPlateNav, setShowPlateNav] = useState(false);
  const source3mfInputRef = useRef<HTMLInputElement>(null);
  const f3dInputRef = useRef<HTMLInputElement>(null);
  const timelapseInputRef = useRef<HTMLInputElement>(null);

  // Fetch plates data for multi-plate browsing (lazy - only when hovering)
  const { data: platesData } = useQuery({
    queryKey: ['archive-plates', archive.id],
    queryFn: () => api.getArchivePlates(archive.id),
    enabled: showPlateNav, // Only fetch when user hovers to see navigation
    staleTime: 5 * 60 * 1000, // Cache for 5 minutes
  });

  // Use pre-computed duplicate sequence and original archive ID from list response
  const duplicateSequence = archive.duplicate_sequence ?? 0;
  const originalArchiveId = archive.original_archive_id ?? null;

  const plates = platesData?.plates ?? [];
  const isMultiPlate = platesData?.is_multi_plate ?? false;
  const displayPlateIndex = currentPlateIndex ?? 0;

  const retryDownloadMutation = useMutation({
    mutationFn: () => api.retryArchiveDownload(archive.id),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      if (data.status === 'recovered') {
        showToast(t('archives.toast.downloadRecovered'));
      } else if (data.status === 'in_progress') {
        // Not an error — just another retry is already running.
        showToast(t('archives.toast.downloadRetryInProgress'), 'info');
      } else {
        showToast(data.message || t('archives.toast.downloadRetryFailed'), 'error');
      }
    },
    onError: (error: Error) => {
      showToast(error.message || t('archives.toast.downloadRetryFailed'), 'error');
    },
  });

  const timelapseDeleteMutation = useMutation({
    mutationFn: () => api.deleteArchiveTimelapse(archive.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      showToast(t('archives.toast.timelapseRemoved'));
    },
    onError: (error: Error) => {
      showToast(error.message || t('archives.toast.failedRemoveTimelapse'), 'error');
    },
  });

  const timelapseUploadMutation = useMutation({
    mutationFn: (file: File) => api.uploadArchiveTimelapse(archive.id, file),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      showToast(t('archives.toast.timelapseUploaded', { filename: data.filename }));
    },
    onError: (error: Error) => {
      showToast(error.message || t('archives.toast.failedUploadTimelapse'), 'error');
    },
  });

  const source3mfUploadMutation = useMutation({
    mutationFn: (file: File) => api.uploadSource3mf(archive.id, file),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      showToast(t('archives.toast.source3mfAttached', { filename: data.filename }));
    },
    onError: (error: Error) => {
      showToast(error.message || t('archives.toast.failedUploadSource3mf'), 'error');
    },
  });

  const source3mfDeleteMutation = useMutation({
    mutationFn: () => api.deleteSource3mf(archive.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      showToast(t('archives.toast.source3mfRemoved'));
    },
    onError: (error: Error) => {
      showToast(error.message || t('archives.toast.failedRemoveSource3mf'), 'error');
    },
  });

  const f3dUploadMutation = useMutation({
    mutationFn: (file: File) => api.uploadF3d(archive.id, file),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      showToast(t('archives.toast.f3dAttached', { filename: data.filename }));
    },
    onError: (error: Error) => {
      showToast(error.message || t('archives.toast.failedUploadF3d'), 'error');
    },
  });

  const f3dDeleteMutation = useMutation({
    mutationFn: () => api.deleteF3d(archive.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      showToast(t('archives.toast.f3dRemoved'));
    },
    onError: (error: Error) => {
      showToast(error.message || t('archives.toast.failedRemoveF3d'), 'error');
    },
  });

  const timelapseScanMutation = useMutation({
    mutationFn: () => api.scanArchiveTimelapse(archive.id),
    onSuccess: (data) => {
      if (data.status === 'attached') {
        queryClient.invalidateQueries({ queryKey: ['archives'] });
        showToast(t('archives.toast.timelapseAttached', { filename: data.filename }));
      } else if (data.status === 'exists') {
        showToast(t('archives.toast.timelapseAlreadyAttached'));
      } else if (data.status === 'not_found' && data.available_files && data.available_files.length > 0) {
        // Show selection dialog
        setAvailableTimelapses(data.available_files);
        setShowTimelapseSelect(true);
      } else {
        showToast(data.message || t('archives.toast.noMatchingTimelapse'), 'warning');
      }
    },
    onError: (error: Error) => {
      showToast(error.message || t('archives.toast.failedScanTimelapse'), 'error');
    },
  });

  const timelapseSelectMutation = useMutation({
    mutationFn: (filename: string) => api.selectArchiveTimelapse(archive.id, filename),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      showToast(t('archives.toast.timelapseAttached', { filename: data.filename }));
      setShowTimelapseSelect(false);
      setAvailableTimelapses([]);
    },
    onError: (error: Error) => {
      showToast(error.message || t('archives.toast.failedAttachTimelapse'), 'error');
    },
  });

  const deleteMutation = useMutation({
    mutationFn: () => api.deleteArchive(archive.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      // Soft-delete shifts the row into the archive trash table —
      // refresh both the trash badge counter on this page's header and
      // the trash list query so a follow-up navigation to the archive
      // trash page picks the new row up immediately. Without this the
      // global 60s staleTime keeps the trash queries on a snapshot that
      // pre-dates this delete.
      queryClient.invalidateQueries({ queryKey: ['archive-trash'] });
      queryClient.invalidateQueries({ queryKey: ['archive-trash-count'] });
      showToast(t('archives.toast.archiveDeleted'));
    },
    onError: () => {
      showToast(t('archives.toast.failedDeleteArchive'), 'error');
    },
  });

  const favoriteMutation = useMutation({
    mutationFn: () => api.toggleFavorite(archive.id),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      showToast(data.is_favorite ? t('archives.toast.addedToFavorites') : t('archives.toast.removedFromFavorites'));
    },
  });

  // Query for linked folders
  const { data: linkedFolders } = useQuery({
    queryKey: ['archive-folders', archive.id],
    queryFn: () => api.getLibraryFoldersByArchive(archive.id),
  });

  const assignProjectMutation = useMutation({
    mutationFn: (projectId: number | null) => api.updateArchive(archive.id, { project_id: projectId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      showToast(t('archives.toast.projectUpdated'));
    },
    onError: () => {
      showToast(t('archives.toast.failedUpdateProject'), 'error');
    },
  });

  const handleContextMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    setContextMenu({ x: e.clientX, y: e.clientY });
  };

  const isGcodeFile = isSlicedFile(archive);

  // PrettyGCode viewer (B.8) entry point. Multi-plate sliced archives go
  // through a picker first; single-plate archives navigate straight in;
  // source-only (no sliced gcode) archives surface a noGcode toast instead
  // of opening an empty viewer iframe. The /plates fetch double-serves as
  // both the multi-plate detector and the source-only short-circuit.
  const openGcodeViewer = async () => {
    try {
      const resp = await api.getArchivePlates(archive.id);
      if (resp.has_gcode === false) {
        showToast(t('archives.platePicker.noGcode'), 'info');
        return;
      }
      if (resp.is_multi_plate && resp.plates.length > 1) {
        setPlatePickerPlates(resp.plates);
        return;
      }
    } catch {
      // Swallow — fall through to the no-plate navigate so the viewer
      // still opens on the first plate (the backend's default).
    }
    navigate(`/gcode-viewer?archive=${archive.id}`);
  };

  const contextMenuItems: ContextMenuItem[] = [
    // Retry download — only shown for fallback archives (file_path empty).
    // Hidden once the archive has a file.
    ...(!archive.file_path ? [
      {
        label: t('archives.menu.retryDownload'),
        icon: <DownloadCloud className="w-4 h-4" />,
        onClick: () => retryDownloadMutation.mutate(),
        disabled: retryDownloadMutation.isPending,
      },
    ] : []),
    // For gcode files: show Print option
    // For source files: show Slice as the primary action
    ...(isGcodeFile ? [
      {
        label: t('archives.menu.print'),
        icon: <Printer className="w-4 h-4" />,
        onClick: () => setShowReprint(true),
        disabled: !archive.file_path || !canModify('archives', 'reprint', archive.created_by_id),
        title: !archive.file_path ? t('archives.card.noFileForReprint') : !canModify('archives', 'reprint', archive.created_by_id) ? t('archives.permission.noReprint') : undefined,
      },
      {
        label: t('archives.menu.schedule'),
        icon: <Calendar className="w-4 h-4" />,
        onClick: () => setShowSchedule(true),
        disabled: !archive.file_path || !hasPermission('queue:create'),
        title: !archive.file_path ? t('archives.card.noFileForReprint') : !hasPermission('queue:create') ? t('archives.permission.noAddToQueue') : undefined,
      },
      {
        label: t('archives.menu.openInBambuStudio'),
        icon: <ExternalLink className="w-4 h-4" />,
        onClick: () => {
          const filename = archive.print_name || archive.filename || 'model';
          openInSlicerWithToken(archive.id, filename, 'file', preferredSlicer);
        },
        disabled: !archive.file_path,
        title: !archive.file_path ? t('archives.card.noFileForReprint') : undefined,
      },
    ] : [
      {
        label: t('archives.menu.slice'),
        icon: <ExternalLink className="w-4 h-4" />,
        onClick: () => {
          const filename = archive.print_name || archive.filename || 'model';
          openInSlicerWithToken(archive.id, filename, 'file', preferredSlicer);
        },
      },
      ...(useSlicerApi ? [{
        label: t('slice.actionServerSide', { defaultValue: 'Slice (server-side)' }),
        icon: <Cog className="w-4 h-4" />,
        onClick: () => setShowSlice(true),
        disabled: !archive.file_path || !hasPermission('library:upload'),
        title: !archive.file_path ? t('archives.card.noFileForReprint') : !hasPermission('library:upload') ? t('fileManager.noPermissionSlice', { defaultValue: 'You do not have permission to slice' }) : undefined,
      }] : []),
    ]),
    {
      label: archive.external_url ? t('archives.menu.externalLink') : t('archives.menu.viewOnMakerWorld'),
      // Icon mirrors the label: ``external_url`` overrides → generic Globe;
      // otherwise (MakerWorld plate OR disabled "no link" entry whose label
      // still reads "View on MakerWorld") show the MakerWorld glyph.
      icon: archive.external_url
        ? <Globe className="w-4 h-4" />
        : <MakerWorldIcon className="w-4 h-4" />,
      onClick: () => {
        const url = archive.external_url || archive.makerworld_url;
        if (url) window.open(url, '_blank');
      },
      disabled: !archive.external_url && !archive.makerworld_url,
    },
    { label: '', divider: true, onClick: () => {} },
    {
      label: t('archives.menu.preview3d'),
      icon: <Box className="w-4 h-4" />,
      onClick: () => setShowViewer(true),
    },
    {
      // PrettyGCode viewer (B.8). Multi-plate sliced archives go through
      // a picker first; single-plate archives navigate straight in;
      // source-only archives surface a noGcode toast.
      label: t('archives.menu.gcodeViewer'),
      icon: <Layers className="w-4 h-4" />,
      onClick: () => { openGcodeViewer(); },
    },
    {
      label: t('archives.menu.viewTimelapse'),
      icon: <Film className="w-4 h-4" />,
      onClick: () => setShowTimelapse(true),
      disabled: !archive.timelapse_path,
    },
    {
      label: t('archives.menu.scanForTimelapse'),
      icon: <ScanSearch className="w-4 h-4" />,
      onClick: () => timelapseScanMutation.mutate(),
      disabled: !archive.printer_id || !!archive.timelapse_path || timelapseScanMutation.isPending || !canModify('archives', 'update', archive.created_by_id),
      title: !canModify('archives', 'update', archive.created_by_id) ? t('archives.permission.noUpdateArchives') : undefined,
    },
    {
      label: t('archives.menu.uploadTimelapse'),
      icon: <Upload className="w-4 h-4" />,
      onClick: () => timelapseInputRef.current?.click(),
      disabled: !!archive.timelapse_path || !canModify('archives', 'update', archive.created_by_id),
      title: !canModify('archives', 'update', archive.created_by_id) ? t('archives.permission.noUpdateArchives') : undefined,
    },
    ...(archive.timelapse_path ? [{
      label: t('archives.menu.removeTimelapse'),
      icon: <Trash2 className="w-4 h-4" />,
      onClick: () => setShowDeleteTimelapseConfirm(true),
      danger: true,
      disabled: !canModify('archives', 'update', archive.created_by_id),
      title: !canModify('archives', 'update', archive.created_by_id) ? t('archives.permission.noUpdateArchives') : undefined,
    }] : []),
    { label: '', divider: true, onClick: () => {} },
    {
      label: archive.source_3mf_path ? t('archives.menu.downloadSource3mf') : t('archives.menu.uploadSource3mf'),
      icon: <FileCode className="w-4 h-4" />,
      onClick: () => {
        if (archive.source_3mf_path) {
          api.downloadSource3mf(archive.id).catch((err) => {
            console.error('Source 3MF download failed:', err);
          });
        } else {
          source3mfInputRef.current?.click();
        }
      },
      disabled: !archive.source_3mf_path && !canModify('archives', 'update', archive.created_by_id),
      title: !archive.source_3mf_path && !canModify('archives', 'update', archive.created_by_id) ? t('archives.permission.noUploadFiles') : undefined,
    },
    ...(archive.source_3mf_path ? [{
      label: t('archives.menu.replaceSource3mf'),
      icon: <Upload className="w-4 h-4" />,
      onClick: () => source3mfInputRef.current?.click(),
      disabled: !canModify('archives', 'update', archive.created_by_id),
      title: !canModify('archives', 'update', archive.created_by_id) ? t('archives.permission.noUpdateArchives') : undefined,
    },
    {
      label: t('archives.menu.removeSource3mf'),
      icon: <Trash2 className="w-4 h-4" />,
      onClick: () => setShowDeleteSource3mfConfirm(true),
      danger: true,
      disabled: !canModify('archives', 'update', archive.created_by_id),
      title: !canModify('archives', 'update', archive.created_by_id) ? t('archives.permission.noUpdateArchives') : undefined,
    }] : []),
    {
      label: archive.f3d_path ? t('archives.menu.replaceF3d') : t('archives.menu.uploadF3d'),
      icon: <Box className="w-4 h-4" />,
      onClick: () => f3dInputRef.current?.click(),
      disabled: !canModify('archives', 'update', archive.created_by_id),
      title: !canModify('archives', 'update', archive.created_by_id) ? t('archives.permission.noUpdateArchives') : undefined,
    },
    ...(archive.f3d_path ? [{
      label: t('archives.menu.downloadF3d'),
      icon: <Download className="w-4 h-4" />,
      onClick: () => {
        api.downloadF3d(archive.id).catch((err) => {
          console.error('F3D download failed:', err);
        });
      },
    },
    {
      label: t('archives.menu.removeF3d'),
      icon: <Trash2 className="w-4 h-4" />,
      onClick: () => setShowDeleteF3dConfirm(true),
      danger: true,
      disabled: !canModify('archives', 'update', archive.created_by_id),
      title: !canModify('archives', 'update', archive.created_by_id) ? t('archives.permission.noUpdateArchives') : undefined,
    }] : []),
    { label: '', divider: true, onClick: () => {} },
    {
      label: t('archives.menu.download'),
      icon: <Download className="w-4 h-4" />,
      onClick: () => {
        api.downloadArchive(archive.id, `${archive.print_name || archive.filename}.3mf`).catch((err) => {
          console.error('Archive download failed:', err);
        });
      },
      disabled: !hasPermission('archives:read'),
      title: !hasPermission('archives:read') ? t('archives.permission.noDownload') : undefined,
    },
    {
      label: t('archives.menu.copyDownloadLink'),
      icon: <Copy className="w-4 h-4" />,
      onClick: () => {
        const url = `${window.location.origin}${api.getArchiveDownload(archive.id)}`;
        navigator.clipboard.writeText(url).then(() => {
          showToast(t('archives.toast.linkCopied'));
        }).catch(() => {
          showToast(t('archives.toast.failedCopyLink'), 'error');
        });
      },
      disabled: !hasPermission('archives:read'),
      title: !hasPermission('archives:read') ? t('archives.permission.noCopyLink') : undefined,
    },
    {
      label: t('archives.menu.qrCode'),
      icon: <QrCode className="w-4 h-4" />,
      onClick: () => setShowQRCode(true),
    },
    {
      label: archive.photos?.length ? t('archives.menu.viewPhotosCount', { count: archive.photos.length }) : t('archives.menu.viewPhotos'),
      icon: <Camera className="w-4 h-4" />,
      onClick: () => setShowPhotos(true),
      disabled: !archive.photos?.length,
    },
    {
      label: t('archives.menu.projectPage'),
      icon: <FileText className="w-4 h-4" />,
      onClick: () => setShowProjectPage(true),
    },
    { label: '', divider: true, onClick: () => {} },
    {
      label: archive.is_favorite ? t('archives.menu.removeFromFavorites') : t('archives.menu.addToFavorites'),
      icon: <Star className={`w-4 h-4 ${archive.is_favorite ? 'fill-yellow-400 text-yellow-400' : ''}`} />,
      onClick: () => favoriteMutation.mutate(),
      disabled: !canModify('archives', 'update', archive.created_by_id),
      title: !canModify('archives', 'update', archive.created_by_id) ? t('archives.permission.noUpdateArchives') : undefined,
    },
    {
      label: t('archives.menu.edit'),
      icon: <Pencil className="w-4 h-4" />,
      onClick: () => setShowEdit(true),
      disabled: !canModify('archives', 'update', archive.created_by_id),
      title: !canModify('archives', 'update', archive.created_by_id) ? t('archives.permission.noUpdateArchives') : undefined,
    },
    ...(archive.project_id && archive.project_name ? [{
      label: t('archives.menu.goToProject', { name: archive.project_name }),
      icon: <FolderKanban className="w-4 h-4 text-bambu-green" />,
      onClick: () => window.location.href = '/projects',
    }] : []),
    {
      label: t('archives.menu.addToProject'),
      icon: <FolderKanban className="w-4 h-4" />,
      onClick: () => {},
      disabled: !canModify('archives', 'update', archive.created_by_id),
      title: !canModify('archives', 'update', archive.created_by_id) ? t('archives.permission.noUpdateArchives') : undefined,
      submenu: (() => {
        const items: ContextMenuItem[] = [];

        // Add "Remove from Project" if archive is in a project
        if (archive.project_id) {
          items.push({
            label: t('archives.menu.removeFromProject'),
            icon: <X className="w-4 h-4" />,
            onClick: () => assignProjectMutation.mutate(null),
            disabled: !canModify('archives', 'update', archive.created_by_id),
          });
        }

        // Add project options
        if (!projects) {
          items.push({
            label: t('archives.menu.loading'),
            icon: <Loader2 className="w-4 h-4 animate-spin" />,
            onClick: () => {},
            disabled: true,
          });
        } else {
          const activeProjects = projects.filter(p => p.status === 'active');
          if (activeProjects.length === 0) {
            items.push({
              label: t('archives.menu.noProjectsAvailable'),
              icon: <FolderKanban className="w-4 h-4 opacity-50" />,
              onClick: () => {},
              disabled: true,
            });
          } else {
            activeProjects.forEach(p => {
              items.push({
                label: p.name,
                icon: <div className="w-3 h-3 rounded-full flex-shrink-0" style={{ backgroundColor: p.color || '#888' }} />,
                onClick: () => assignProjectMutation.mutate(p.id),
                disabled: archive.project_id === p.id || !canModify('archives', 'update', archive.created_by_id),
              });
            });
          }
        }

        return items;
      })(),
    },
    {
      label: isSelected ? t('archives.menu.deselect') : t('archives.menu.select'),
      icon: isSelected ? <CheckSquare className="w-4 h-4" /> : <Square className="w-4 h-4" />,
      onClick: () => onSelect(archive.id),
    },
    { label: '', divider: true, onClick: () => {} },
    {
      label: t('archives.menu.delete'),
      icon: <Trash2 className="w-4 h-4" />,
      onClick: () => setShowDeleteConfirm(true),
      danger: true,
      disabled: !canModify('archives', 'delete', archive.created_by_id),
      title: !canModify('archives', 'delete', archive.created_by_id) ? t('archives.permission.noDelete') : undefined,
    },
  ];

  return (
    <Card
      data-archive-id={archive.id}
      className={`relative flex flex-col group ${isSelected ? 'ring-2 ring-bambu-green' : ''} ${selectionMode ? 'cursor-pointer' : ''}`}
      style={isHighlighted ? { outline: '4px solid #facc15', outlineOffset: '2px' } : undefined}
      onContextMenu={handleContextMenu}
      onClick={selectionMode ? () => onSelect(archive.id) : undefined}
    >
      {/* Selection checkbox */}
      {selectionMode && (
        <button
          className="absolute top-2 left-2 z-10 p-1 rounded bg-black/50 hover:bg-black/70 transition-colors"
          onClick={(e) => { e.stopPropagation(); onSelect(archive.id); }}
        >
          {isSelected ? (
            <CheckSquare className="w-5 h-5 text-bambu-green" />
          ) : (
            <Square className="w-5 h-5 text-white" />
          )}
        </button>
      )}

      {/* Thumbnail with plate navigation */}
      <div
        className="aspect-video bg-bambu-dark relative flex-shrink-0 overflow-hidden rounded-t-xl"
        onMouseEnter={() => setShowPlateNav(true)}
        onMouseLeave={() => setShowPlateNav(false)}
      >
        {archive.thumbnail_path ? (
          <img
            src={
              currentPlateIndex !== null && plates.length > 0
                ? api.getArchivePlateThumbnail(archive.id, plates[displayPlateIndex]?.index ?? 0)
                : api.getArchiveThumbnail(archive.id)
            }
            alt={archive.print_name || archive.filename}
            className="w-full h-full object-cover"
          />
        ) : (
          <div className="w-full h-full flex items-center justify-center">
            <Image className="w-12 h-12 text-bambu-dark-tertiary" />
          </div>
        )}
        {/* Plate navigation - only show for multi-plate archives */}
        {isMultiPlate && plates.length > 1 && (
          <>
            {/* Left arrow */}
            <button
              className={`absolute left-1 top-1/2 -translate-y-1/2 p-1 rounded-full bg-black/60 hover:bg-black/80 transition-all ${
                isMobile ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'
              }`}
              onClick={(e) => {
                e.stopPropagation();
                setCurrentPlateIndex((prev) => {
                  const current = prev ?? 0;
                  return current > 0 ? current - 1 : plates.length - 1;
                });
              }}
              title={t('archives.card.previousPlate')}
            >
              <ChevronLeft className="w-4 h-4 text-white" />
            </button>
            {/* Right arrow */}
            <button
              className={`absolute right-1 top-1/2 -translate-y-1/2 p-1 rounded-full bg-black/60 hover:bg-black/80 transition-all ${
                isMobile ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'
              }`}
              onClick={(e) => {
                e.stopPropagation();
                setCurrentPlateIndex((prev) => {
                  const current = prev ?? 0;
                  return current < plates.length - 1 ? current + 1 : 0;
                });
              }}
              title={t('archives.card.nextPlate')}
            >
              <ChevronRight className="w-4 h-4 text-white" />
            </button>
            {/* Dots indicator */}
            <div
              className={`absolute bottom-1 left-1/2 -translate-x-1/2 flex gap-1 px-2 py-1 rounded-full bg-black/50 transition-all ${
                isMobile ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'
              }`}
            >
              {plates.map((plate, idx) => (
                <button
                  key={plate.index}
                  className={`w-2 h-2 rounded-full transition-colors ${
                    idx === displayPlateIndex ? 'bg-bambu-green' : 'bg-white/50 hover:bg-white/80'
                  }`}
                  onClick={(e) => {
                    e.stopPropagation();
                    setCurrentPlateIndex(idx);
                  }}
                  title={plate.name || t('archives.card.plateNumber', { index: plate.index })}
                />
              ))}
            </div>
          </>
        )}
        {/* Context menu button - visible on mobile, shows on hover for desktop */}
        <button
          className={`absolute top-2 left-2 p-1.5 rounded bg-black/50 hover:bg-black/70 transition-all ${
            isMobile ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'
          } ${selectionMode ? 'left-10' : ''}`}
          onClick={(e) => {
            e.stopPropagation();
            const rect = e.currentTarget.getBoundingClientRect();
            setContextMenu({ x: rect.left, y: rect.bottom + 4 });
          }}
          title={t('archives.card.moreOptions')}
        >
          <MoreVertical className="w-5 h-5 text-white" />
        </button>
        {/* Favorite star */}
        <button
          className={`absolute top-2 right-2 p-1 rounded transition-colors ${
            canModify('archives', 'update', archive.created_by_id)
              ? 'bg-black/50 hover:bg-black/70'
              : 'bg-black/30 cursor-not-allowed'
          }`}
          onClick={(e) => {
            e.stopPropagation();
            if (canModify('archives', 'update', archive.created_by_id)) {
              favoriteMutation.mutate();
            }
          }}
          disabled={!canModify('archives', 'update', archive.created_by_id)}
          title={!canModify('archives', 'update', archive.created_by_id) ? t('archives.permission.noUpdateArchives') : (archive.is_favorite ? t('archives.card.removeFromFavorites') : t('archives.card.addToFavorites'))}
        >
          <Star
            className={`w-5 h-5 ${archive.is_favorite ? 'text-yellow-400 fill-yellow-400' : 'text-white'} ${!canModify('archives', 'update', archive.created_by_id) ? 'opacity-50' : ''}`}
          />
        </button>
        {(() => {
          const badge = getArchiveStatusBadge(archive.status);
          if (!badge) return null;
          const isPrinting = archive.status === 'printing';
          const base = `absolute top-2 left-12 px-2 py-1 rounded text-xs flex items-center gap-1 ${badge.className}`;
          const pulseCls = badge.pulse ? 'animate-pulse' : '';
          if (isPrinting && archive.printer_id) {
            return (
              <Link
                to={`/#printer-${archive.printer_id}`}
                onClick={(e) => e.stopPropagation()}
                className={`${base} ${pulseCls} hover:brightness-125 transition`}
                title={t('archives.card.printingClickHint')}
              >
                <Loader2 className="w-3 h-3 animate-spin" />
                {t(badge.labelKey)}
              </Link>
            );
          }
          // Verbose diagnostic on hover for failed/cancelled archives (m019).
          const errorTooltip = archive.error_message || archive.failure_reason || undefined;
          return (
            <div className={`${base} ${pulseCls}`} title={errorTooltip}>
              {isPrinting && <Loader2 className="w-3 h-3 animate-spin" />}
              {t(badge.labelKey)}
            </div>
          );
        })()}
        {/* Duplicate badge */}
        {archive.duplicate_count > 0 && duplicateSequence > 0 && originalArchiveId && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              onNavigateToArchive?.(originalArchiveId);
            }}
            className="absolute top-2 right-12 px-2 py-1 rounded text-xs bg-purple-500/80 hover:bg-purple-600/90 text-white flex items-center gap-1 transition-colors cursor-pointer"
            title={t('archives.viewOriginalPrint', { id: originalArchiveId })}
          >
            <Copy className="w-3 h-3" />
            #{duplicateSequence}
          </button>
        )}
        {archive.duplicate_count > 0 && duplicateSequence === 0 && (
          <span
            className="absolute top-2 right-12 px-2 py-1 rounded text-xs bg-purple-500/80 text-white flex items-center gap-1"
            title={`${archive.duplicate_count} reprint${archive.duplicate_count === 1 ? '' : 's'}`}
          >
            <GitBranch className="w-3 h-3" />
            +{archive.duplicate_count}
          </span>
        )}
        {/* Source 3MF badge */}
        {archive.source_3mf_path && (
          <button
            className="absolute bottom-2 left-2 p-1.5 rounded bg-black/60 hover:bg-black/80 transition-colors"
            onClick={(e) => {
              e.stopPropagation();
              // Open source 3MF in Bambu Studio - use filename in URL for slicer compatibility
              const sourceName = (archive.print_name || archive.filename || 'source').replace(/\.gcode\.3mf$/i, '') + '_source';
              openInSlicerWithToken(archive.id, sourceName, 'source', preferredSlicer);
            }}
            title={t('archives.card.openSource3mf')}
          >
            <FileCode className="w-4 h-4 text-orange-400" />
          </button>
        )}
        {/* F3D badge */}
        {archive.f3d_path && (
          <button
            className={`absolute bottom-2 ${archive.source_3mf_path ? 'left-12' : 'left-2'} p-1.5 rounded bg-black/60 hover:bg-black/80 transition-colors`}
            onClick={(e) => {
              e.stopPropagation();
              // Download F3D file
              api.downloadF3d(archive.id).catch((err) => {
                console.error('F3D download failed:', err);
              });
            }}
            title={t('archives.card.downloadF3d')}
          >
            <Box className="w-4 h-4 text-cyan-400" />
          </button>
        )}
        {/* 3D preview badge */}
        <button
          className="absolute bottom-2 right-2 p-1.5 rounded bg-black/60 hover:bg-black/80 transition-colors"
          onClick={(e) => {
            e.stopPropagation();
            setShowViewer(true);
          }}
          title={t('archives.card.preview3d')}
        >
          <Box className="w-4 h-4 text-white" />
        </button>
        {/* Timelapse badge */}
        {archive.timelapse_path && (
          <button
            className="absolute bottom-2 right-12 p-1.5 rounded bg-black/60 hover:bg-black/80 transition-colors"
            onClick={(e) => {
              e.stopPropagation();
              setShowTimelapse(true);
            }}
            title={t('archives.card.viewTimelapse')}
          >
            <Film className="w-4 h-4 text-bambu-green" />
          </button>
        )}
        {/* Photos badge */}
        {archive.photos && archive.photos.length > 0 && (
          <button
            className={`absolute bottom-2 ${archive.timelapse_path ? 'right-[5.5rem]' : 'right-12'} p-1.5 rounded bg-black/60 hover:bg-black/80 transition-colors`}
            onClick={(e) => {
              e.stopPropagation();
              setShowPhotos(true);
            }}
            title={archive.photos.length === 1 ? t('archives.card.viewPhoto') : t('archives.card.viewPhotos', { count: archive.photos.length })}
          >
            <Camera className="w-4 h-4 text-blue-400" />
            {archive.photos.length > 1 && (
              <span className="absolute -top-1 -right-1 w-4 h-4 bg-blue-500 rounded-full text-[10px] text-white flex items-center justify-center">
                {archive.photos.length}
              </span>
            )}
          </button>
        )}
        {/* Linked folder badge */}
        {linkedFolders && linkedFolders.length > 0 && (
          <Link
            to={`/files?folder=${linkedFolders[0].id}`}
            className="absolute bottom-2 p-1.5 rounded bg-black/60 hover:bg-black/80 transition-colors"
            onClick={(e) => e.stopPropagation()}
            title={t('archives.card.openFolder', { name: linkedFolders[0].name })}
            style={{ left: archive.source_3mf_path ? (archive.f3d_path ? '5.5rem' : '3rem') : (archive.f3d_path ? '3rem' : '0.5rem') }}
          >
            <FolderOpen className="w-4 h-4 text-yellow-400" />
          </Link>
        )}
      </div>

      <CardContent className="p-4 flex-1 flex flex-col">
        {/* Archive ID */}
        <p className="text-[10px] text-bambu-gray/70 mb-1">#{archive.id}</p>

        {/* Title */}
        <div className="flex items-center justify-between gap-2 mb-1">
          <h3 className="min-w-0 font-medium text-white truncate">
            {archive.print_name || archive.filename}
          </h3>
          <Button
            variant="ghost"
            size="sm"
            className="p-1 sm:p-1.5 shrink-0"
            onClick={() => setShowEdit(true)}
            disabled={!canModify('archives', 'update', archive.created_by_id)}
            title={!canModify('archives', 'update', archive.created_by_id) ? t('archives.card.noPermissionEdit') : t('archives.card.edit')}
          >
            <Pencil className="w-3 h-3 sm:w-4 sm:h-4" />
          </Button>
        </div>
        <div className="flex items-center gap-2 mb-3 flex-wrap">
          <p className="text-xs text-bambu-gray">{printerName}</p>
          {/* File type badge */}
          <span
            className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
              isSlicedFile(archive)
                ? 'bg-bambu-green/20 text-bambu-green'
                : 'bg-orange-500/20 text-orange-400'
            }`}
            title={
              isSlicedFile(archive)
                ? t('archives.card.slicedFile')
                : t('archives.card.sourceFile')
            }
          >
            {isSlicedFile(archive) ? t('archives.card.gcode') : t('archives.card.source')}
          </span>
          {archive.swap_compatible && (
            <span className="text-[10px] px-1 py-0.5 bg-amber-500/20 text-amber-400 rounded">SWAP</span>
          )}
          {/* File hash badge */}
          {archive.content_hash && (
            <span
              className="text-[10px] px-1.5 py-0.5 rounded font-mono bg-bambu-dark-tertiary/50 text-bambu-gray-light opacity-0 transition-opacity duration-150 group-hover:opacity-100"
              title={`SHA256: ${archive.content_hash}`}
            >
              {archive.content_hash.slice(0, 8).toUpperCase()}
            </span>
          )}
          {archive.project_name && archive.project_id != null && (
            <Link
              to={`/projects/${archive.project_id}`}
              onClick={(e) => e.stopPropagation()}
              className="text-xs px-1.5 py-0.5 rounded-full truncate max-w-[120px] hover:brightness-125 transition"
              style={{
                backgroundColor: `${projects?.find(p => p.id === archive.project_id)?.color || '#6b7280'}20`,
                color: projects?.find(p => p.id === archive.project_id)?.color || '#6b7280'
              }}
              title={t('archives.card.project', { name: archive.project_name })}
            >
              {archive.project_name}
            </Link>
          )}
        </div>

        {/* Stats */}
        <div className="grid grid-cols-2 gap-2 text-xs mb-4 min-h-[48px]">
          {(archive.print_time_seconds || archive.actual_time_seconds) && (
            <div className="flex items-center gap-1.5 text-bambu-gray" title={
              archive.time_accuracy
                ? `Estimated: ${formatDuration(archive.print_time_seconds || 0)}\nActual: ${formatDuration(archive.actual_time_seconds || 0)}\nAccuracy: ${archive.time_accuracy.toFixed(0)}%`
                : archive.actual_time_seconds
                  ? `Actual: ${formatDuration(archive.actual_time_seconds)}`
                  : `Estimated: ${formatDuration(archive.print_time_seconds || 0)}`
            }>
              <Clock className="w-3 h-3" />
              {formatDuration(archive.actual_time_seconds || archive.print_time_seconds || 0)}
              {archive.time_accuracy && (
                <span className={`text-[10px] px-1 rounded ${
                  archive.time_accuracy >= 95 && archive.time_accuracy <= 105
                    ? 'bg-bambu-green/20 text-bambu-green'
                    : archive.time_accuracy > 105
                      ? 'bg-blue-500/20 text-blue-400'
                      : 'bg-orange-500/20 text-orange-400'
                }`}>
                  {archive.time_accuracy > 100 ? '+' : ''}{(archive.time_accuracy - 100).toFixed(0)}%
                </span>
              )}
            </div>
          )}
          {archive.filament_used_grams && (
            <div className="flex items-center gap-1.5 text-bambu-gray">
              <Package className="w-3 h-3" />
              {archive.filament_used_grams.toFixed(1)}g
            </div>
          )}
          {(archive.cost != null || archive.energy_cost != null) && (
            <div className="flex items-center gap-3 text-bambu-gray">
              {archive.cost != null && (
                <div className="flex items-center gap-1.5">
                  <Coins className="w-3 h-3" />
                  {currency}{archive.cost.toFixed(2)}
                </div>
              )}
                {archive.energy_cost != null && (
                  <div className="flex items-center gap-1.5" title={`${t('stats.energyUsed')}: ${archive.energy_kwh?.toFixed(3) || 'N/A'} kWh`}>
                    <Zap className="w-3 h-3" />
                    {currency}{archive.energy_cost.toFixed(2)}
                  </div>
                )}
            </div>
          )}
          {(archive.layer_height || archive.total_layers) && (
            <div className="flex items-center gap-1.5 text-bambu-gray">
              <Layers className="w-3 h-3" />
              {archive.total_layers && <span>{archive.total_layers === 1 ? t('archives.card.layer', { count: archive.total_layers }) : t('archives.card.layers', { count: archive.total_layers })}</span>}
              {archive.total_layers && archive.layer_height && <span className="text-bambu-gray/50">·</span>}
              {archive.layer_height && <span>{archive.layer_height}mm</span>}
            </div>
          )}
          {archive.object_count != null && archive.object_count > 0 && (
            <div className="flex items-center gap-1.5 text-bambu-gray" title={archive.object_count === 1 ? t('archives.card.object', { count: archive.object_count }) : t('archives.card.objects', { count: archive.object_count })}>
              <Box className="w-3 h-3" />
              {archive.object_count === 1 ? t('archives.card.object', { count: archive.object_count }) : t('archives.card.objects', { count: archive.object_count })}
            </div>
          )}
          {archive.sliced_for_model && (
            <div className="flex items-center gap-1.5 text-bambu-gray" title={t('archives.card.slicedFor', { model: archive.sliced_for_model })}>
              <Printer className="w-3 h-3" />
              {archive.sliced_for_model}
            </div>
          )}
          {archive.filament_type && (
            <div className="flex items-center gap-1.5 col-span-2">
              <span className="text-bambu-gray text-xs">{archive.filament_type}</span>
              {archive.filament_color && (
                <div className="flex items-center gap-0.5 flex-wrap">
                  {archive.filament_color.split(',').map((color, i) => (
                    <div
                      key={i}
                      className="w-3 h-3 rounded-full border border-black/20"
                      style={{ backgroundColor: color }}
                      title={color}
                    />
                  ))}
                </div>
              )}
            </div>
          )}
        </div>

        {/* Tags & Notes */}
        {(archive.tags || archive.notes) && (
          <div className="flex flex-wrap items-center gap-1.5 mb-3">
            {archive.notes && (
              <div
                className="flex items-center gap-1 px-1.5 py-0.5 bg-blue-500/20 text-blue-400 rounded text-xs"
                title={archive.notes}
              >
                <StickyNote className="w-3 h-3" />
              </div>
            )}
            {archive.tags?.split(',').map((tag, i) => (
              <span
                key={i}
                className="px-1.5 py-0.5 bg-bambu-dark-tertiary text-bambu-gray-light rounded text-xs"
              >
                {tag.trim()}
              </span>
            ))}
          </div>
        )}

        {/* Spacer to push content to bottom */}
        <div className="flex-1" />

        {/* Date, Size & Creator */}
        <div className="flex items-center justify-between text-xs text-bambu-gray border-t border-bambu-dark-tertiary pt-3">
          <span>{formatDateTime(archive.created_at, timeFormat, dateFormat)}</span>
          <div className="flex items-center gap-2">
            {archive.created_by_username && (
              <span className="flex items-center gap-1" title={t('archives.card.uploadedBy', { name: archive.created_by_username })}>
                <User className="w-3 h-3" />
                {archive.created_by_username}
              </span>
            )}
            <span>{formatFileSize(archive.file_size)}</span>
          </div>
        </div>

        {/* Actions */}
        <div className="flex gap-1 mt-3">
          {isSlicedFile(archive) ? (
            // Sliced file - can print directly
            <>
              <Button
                variant="primary"
                size="sm"
                className="flex-1 min-w-0 overflow-hidden"
                onClick={() => setShowReprint(true)}
                disabled={!archive.file_path || !canModify('archives', 'reprint', archive.created_by_id)}
                title={!archive.file_path ? t('archives.card.noFileForReprint') : !canModify('archives', 'reprint', archive.created_by_id) ? t('archives.card.noPermissionReprint') : undefined}
              >
                <Printer className="w-3 h-3 flex-shrink-0" />
                <span className="hidden sm:inline truncate">{t('archives.card.reprint')}</span>
              </Button>
              <Button
                variant="secondary"
                size="sm"
                className="flex-1 min-w-0 overflow-hidden"
                onClick={() => setShowSchedule(true)}
                disabled={!archive.file_path || !hasPermission('queue:create')}
                title={!archive.file_path ? t('archives.card.noFileForReprint') : !hasPermission('queue:create') ? t('archives.permission.noAddToQueue') : t('archives.card.schedulePrint')}
              >
                <Calendar className="w-3 h-3 flex-shrink-0" />
                <span className="hidden sm:inline truncate">{t('archives.card.schedule')}</span>
              </Button>
              <Button
                variant="secondary"
                size="sm"
                className="min-w-0 p-1 sm:p-1.5"
                onClick={() => {
                  const filename = archive.print_name || archive.filename || 'model';
                  openInSlicerWithToken(archive.id, filename, 'file', preferredSlicer);
                }}
                title={t('archives.card.openInBambuStudio')}
              >
                <ExternalLink className="w-3 h-3 sm:w-4 sm:h-4" />
              </Button>
            </>
          ) : (
            // Source file only - must open in slicer first
            <Button
              variant="primary"
              size="sm"
              className="flex-1 min-w-0 overflow-hidden"
              onClick={() => {
                const filename = archive.print_name || archive.filename || 'model';
                openInSlicerWithToken(archive.id, filename, 'file', preferredSlicer);
              }}
              title={t('archives.card.openInBambuStudioToSlice')}
            >
              <ExternalLink className="w-3 h-3 flex-shrink-0" />
              <span className="hidden sm:inline truncate">{t('archives.card.slice')}</span>
            </Button>
          )}
          <Button
            variant="secondary"
            size="sm"
            className="min-w-0 p-1 sm:p-1.5"
            onClick={() => {
              const url = archive.external_url || archive.makerworld_url;
              if (url) window.open(url, '_blank');
            }}
            disabled={!archive.external_url && !archive.makerworld_url}
            title={
              archive.external_url
                ? t('archives.card.externalLink')
                : archive.makerworld_url
                  ? t('archives.card.makerWorld', { designer: archive.designer || t('archives.card.viewProject') })
                  : t('archives.card.noExternalLink')
            }
          >
            {archive.external_url ? (
              <Globe className="w-3 h-3 sm:w-4 sm:h-4" />
            ) : (
              <MakerWorldIcon className={`w-3 h-3 sm:w-4 sm:h-4 ${!archive.makerworld_url ? 'opacity-20' : ''}`} />
            )}
          </Button>
          <Button
            variant="secondary"
            size="sm"
            className="min-w-0 p-1 sm:p-1.5"
            onClick={() => {
              api.downloadArchive(archive.id, `${archive.print_name || archive.filename}.3mf`).catch((err) => {
                console.error('Archive download failed:', err);
              });
            }}
            title={t('archives.card.download')}
          >
            <Download className="w-3 h-3 sm:w-4 sm:h-4" />
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="min-w-0 p-1 sm:p-1.5"
            onClick={() => setShowDeleteConfirm(true)}
            disabled={!canModify('archives', 'delete', archive.created_by_id)}
            title={!canModify('archives', 'delete', archive.created_by_id) ? t('archives.card.noPermissionDelete') : t('archives.card.delete')}
          >
            <Trash2 className="w-3 h-3 sm:w-4 sm:h-4 text-red-400" />
          </Button>
        </div>
      </CardContent>

      {/* Edit Modal */}
      {showEdit && (
        <EditArchiveModal
          archive={archive}
          onClose={() => setShowEdit(false)}
        />
      )}

      {/* 3D Viewer Modal */}
      {showViewer && (
        <ModelViewerModal
          archiveId={archive.id}
          title={archive.print_name || archive.filename}
          fileType={getArchiveFileType(archive.filename)}
          archivePlateIndex={archive.plate_index}
          onClose={() => setShowViewer(false)}
        />
      )}

      {/* Plate picker for the PrettyGCode viewer (B.8). Shown only for
          multi-plate sliced archives; single-plate + source-only flows
          short-circuit before this mounts. */}
      {platePickerPlates && (
        <PlatePickerModal
          plates={platePickerPlates}
          onSelect={(plateIndex) => {
            setPlatePickerPlates(null);
            navigate(`/gcode-viewer?archive=${archive.id}&plate=${plateIndex}`);
          }}
          onClose={() => setPlatePickerPlates(null)}
        />
      )}

      {/* Reprint Modal */}
      {showReprint && (
        <PrintModal
          mode="reprint"
          archiveId={archive.id}
          archiveName={archive.print_name || archive.filename}
          onClose={() => setShowReprint(false)}
        />
      )}

      {/* Server-side slice modal */}
      {showSlice && (
        <SliceModal
          source={{ kind: 'archive', id: archive.id, filename: archive.filename }}
          onClose={() => setShowSlice(false)}
        />
      )}

      {/* Delete Confirmation */}
      {showDeleteConfirm && (
        <ConfirmModal
          title={t('archives.modal.deleteArchive')}
          message={t('archives.modal.deleteConfirm', { name: archive.print_name || archive.filename })}
          confirmText={t('archives.modal.deleteButton')}
          variant="danger"
          onConfirm={() => {
            deleteMutation.mutate();
            setShowDeleteConfirm(false);
          }}
          onCancel={() => setShowDeleteConfirm(false)}
        />
      )}

      {/* Delete Source 3MF Confirmation */}
      {showDeleteSource3mfConfirm && (
        <ConfirmModal
          title={t('archives.modal.removeSource3mf')}
          message={t('archives.modal.removeSource3mfConfirm', { name: archive.print_name || archive.filename })}
          confirmText={t('archives.modal.removeButton')}
          variant="danger"
          onConfirm={() => {
            source3mfDeleteMutation.mutate();
            setShowDeleteSource3mfConfirm(false);
          }}
          onCancel={() => setShowDeleteSource3mfConfirm(false)}
        />
      )}

      {/* Delete F3D Confirmation */}
      {showDeleteF3dConfirm && (
        <ConfirmModal
          title={t('archives.modal.removeF3d')}
          message={t('archives.modal.removeF3dConfirm', { name: archive.print_name || archive.filename })}
          confirmText={t('archives.modal.removeButton')}
          variant="danger"
          onConfirm={() => {
            f3dDeleteMutation.mutate();
            setShowDeleteF3dConfirm(false);
          }}
          onCancel={() => setShowDeleteF3dConfirm(false)}
        />
      )}

      {/* Delete Timelapse Confirmation */}
      {showDeleteTimelapseConfirm && (
        <ConfirmModal
          title={t('archives.modal.removeTimelapse')}
          message={t('archives.modal.removeTimelapseConfirm', { name: archive.print_name || archive.filename })}
          confirmText={t('archives.modal.removeButton')}
          variant="danger"
          onConfirm={() => {
            timelapseDeleteMutation.mutate();
            setShowDeleteTimelapseConfirm(false);
          }}
          onCancel={() => setShowDeleteTimelapseConfirm(false)}
        />
      )}

      {/* Context Menu */}
      {contextMenu && (
        <ContextMenu
          x={contextMenu.x}
          y={contextMenu.y}
          items={contextMenuItems}
          onClose={() => setContextMenu(null)}
        />
      )}

      {/* Timelapse Viewer Modal */}
      {showTimelapse && archive.timelapse_path && (
        <TimelapseViewer
          src={api.getArchiveTimelapse(archive.id)}
          title={t('archives.modal.timelapse', { name: archive.print_name || archive.filename })}
          downloadFilename={`${archive.print_name || archive.filename}_timelapse.mp4`}
          archiveId={archive.id}
          onClose={() => setShowTimelapse(false)}
          onEdit={() => {
            queryClient.invalidateQueries({ queryKey: ['archives'] });
            setShowTimelapse(false);  // Close viewer to reload fresh video
          }}
        />
      )}

      {/* Timelapse Selection Modal */}
      {showTimelapseSelect && availableTimelapses.length > 0 && (
        <div className="fixed inset-0 bg-black/80 flex items-center justify-center z-50 p-4">
          <div className="bg-card-dark rounded-lg max-w-lg w-full max-h-[80vh] flex flex-col">
            <div className="flex items-center justify-between p-4 border-b border-gray-700">
              <div>
                <h3 className="text-lg font-semibold text-white">{t('archives.modal.selectTimelapse')}</h3>
                <p className="text-sm text-gray-400 mt-1">
                  {t('archives.modal.selectTimelapseDesc')}
                </p>
              </div>
              <button
                onClick={() => {
                  setShowTimelapseSelect(false);
                  setAvailableTimelapses([]);
                }}
                className="text-gray-400 hover:text-white p-1"
              >
                <X className="w-5 h-5" />
              </button>
            </div>
            <div className="overflow-y-auto flex-1 p-2">
              {availableTimelapses.map((file) => (
                <button
                  key={file.name}
                  onClick={() => timelapseSelectMutation.mutate(file.name)}
                  disabled={timelapseSelectMutation.isPending}
                  className="w-full text-left p-3 rounded-lg hover:bg-gray-700 transition-colors flex items-center gap-3 disabled:opacity-50"
                >
                  <Film className="w-8 h-8 text-bambu-green flex-shrink-0" />
                  <div className="flex-1 min-w-0">
                    <p className="text-white font-medium truncate">{file.name}</p>
                    <p className="text-sm text-gray-400">
                      {formatFileSize(file.size)}
                      {file.mtime && ` • ${formatDateTime(file.mtime, timeFormat, dateFormat)}`}
                    </p>
                  </div>
                </button>
              ))}
            </div>
            <div className="p-4 border-t border-gray-700">
              <Button
                variant="secondary"
                onClick={() => {
                  setShowTimelapseSelect(false);
                  setAvailableTimelapses([]);
                }}
                className="w-full"
              >
                {t('common.cancel')}
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* QR Code Modal */}
      {showQRCode && (
        <QRCodeModal
          archiveId={archive.id}
          archiveName={archive.print_name || archive.filename}
          onClose={() => setShowQRCode(false)}
        />
      )}

      {/* Photo Gallery Modal */}
      {showPhotos && archive.photos && archive.photos.length > 0 && (
        <PhotoGalleryModal
          archiveId={archive.id}
          archiveName={archive.print_name || archive.filename}
          photos={archive.photos}
          onClose={() => setShowPhotos(false)}
          onDelete={async (filename) => {
            try {
              await api.deleteArchivePhoto(archive.id, filename);
              queryClient.invalidateQueries({ queryKey: ['archives'] });
              showToast(t('archives.toast.photoDeleted'));
            } catch {
              showToast(t('archives.toast.failedDeletePhoto'), 'error');
            }
          }}
        />
      )}

      {/* Project Page Modal */}
      {showProjectPage && (
        <ProjectPageModal
          archiveId={archive.id}
          archiveName={archive.print_name || archive.filename}
          onClose={() => setShowProjectPage(false)}
        />
      )}

      {showSchedule && (
        <PrintModal
          mode="add-to-queue"
          archiveId={archive.id}
          archiveName={archive.print_name || archive.filename}
          onClose={() => setShowSchedule(false)}
        />
      )}

      {/* Hidden file input for source 3MF upload */}
      <input
        ref={source3mfInputRef}
        type="file"
        accept=".3mf"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) {
            source3mfUploadMutation.mutate(file);
          }
          e.target.value = '';
        }}
      />
      {/* Hidden file input for F3D upload */}
      <input
        ref={f3dInputRef}
        type="file"
        accept=".f3d"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) {
            f3dUploadMutation.mutate(file);
          }
          e.target.value = '';
        }}
      />
      {/* Hidden file input for timelapse upload */}
      <input
        ref={timelapseInputRef}
        type="file"
        accept=".mp4,.avi,.mkv"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) {
            timelapseUploadMutation.mutate(file);
          }
          e.target.value = '';
        }}
      />
    </Card>
  );
}

function ArchiveListRow({
  archive,
  printerName,
  isSelected,
  onSelect,
  selectionMode,
  projects,
  isHighlighted,
  preferredSlicer = 'bambu_studio',
  t,
  onNavigateToArchive,
}: {
  archive: Archive;
  printerName: string;
  isSelected: boolean;
  onSelect: (id: number) => void;
  selectionMode: boolean;
  projects: ProjectListItem[] | undefined;
  isHighlighted?: boolean;
  preferredSlicer?: SlicerType;
  t: TFunction;
  onNavigateToArchive?: (archiveId: number) => void;
}) {
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { hasPermission, canModify } = useAuth();
  const { data: rowSettings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings });
  const timeFormat: TimeFormat = (rowSettings as Record<string, string> | undefined)?.time_format as TimeFormat || 'system';
  const dateFormat: DateFormat = (rowSettings as Record<string, string> | undefined)?.date_format as DateFormat || 'system';
  const useSlicerApi: boolean = !!(rowSettings as Record<string, unknown> | undefined)?.use_slicer_api;
  const [showEdit, setShowEdit] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [showReprint, setShowReprint] = useState(false);
  const [showSchedule, setShowSchedule] = useState(false);
  const [showSlice, setShowSlice] = useState(false);
  const [showViewer, setShowViewer] = useState(false);
  const [showTimelapse, setShowTimelapse] = useState(false);
  const [showTimelapseSelect, setShowTimelapseSelect] = useState(false);
  const [availableTimelapses, setAvailableTimelapses] = useState<Array<{ name: string; path: string; size: number; mtime: string | null }>>([]);
  const [showQRCode, setShowQRCode] = useState(false);
  const [showPhotos, setShowPhotos] = useState(false);
  const [showProjectPage, setShowProjectPage] = useState(false);
  // PrettyGCode viewer (B.8) — see ArchiveCard for the same pattern.
  const [platePickerPlates, setPlatePickerPlates] = useState<PlateMetadata[] | null>(null);
  const navigate = useNavigate();
  const [showDeleteSource3mfConfirm, setShowDeleteSource3mfConfirm] = useState(false);
  const [showDeleteF3dConfirm, setShowDeleteF3dConfirm] = useState(false);
  const [showDeleteTimelapseConfirm, setShowDeleteTimelapseConfirm] = useState(false);
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number } | null>(null);
  const source3mfInputRef = useRef<HTMLInputElement>(null);
  const f3dInputRef = useRef<HTMLInputElement>(null);
  const timelapseInputRef = useRef<HTMLInputElement>(null);

  // Use pre-computed duplicate sequence and original archive ID from list response
  const duplicateSequence = archive.duplicate_sequence ?? 0;
  const originalArchiveId = archive.original_archive_id ?? null;

  const timelapseDeleteMutation = useMutation({
    mutationFn: () => api.deleteArchiveTimelapse(archive.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      showToast(t('archives.toast.timelapseRemoved'));
    },
    onError: (error: Error) => {
      showToast(error.message || t('archives.toast.failedRemoveTimelapse'), 'error');
    },
  });

  const timelapseUploadMutation = useMutation({
    mutationFn: (file: File) => api.uploadArchiveTimelapse(archive.id, file),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      showToast(t('archives.toast.timelapseUploaded', { filename: data.filename }));
    },
    onError: (error: Error) => {
      showToast(error.message || t('archives.toast.failedUploadTimelapse'), 'error');
    },
  });

  const source3mfUploadMutation = useMutation({
    mutationFn: (file: File) => api.uploadSource3mf(archive.id, file),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      showToast(t('archives.toast.source3mfAttached', { filename: data.filename }));
    },
    onError: (error: Error) => {
      showToast(error.message || t('archives.toast.failedUploadSource3mf'), 'error');
    },
  });

  const source3mfDeleteMutation = useMutation({
    mutationFn: () => api.deleteSource3mf(archive.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      showToast(t('archives.toast.source3mfRemoved'));
    },
    onError: (error: Error) => {
      showToast(error.message || t('archives.toast.failedRemoveSource3mf'), 'error');
    },
  });

  const f3dUploadMutation = useMutation({
    mutationFn: (file: File) => api.uploadF3d(archive.id, file),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      showToast(t('archives.toast.f3dAttached', { filename: data.filename }));
    },
    onError: (error: Error) => {
      showToast(error.message || t('archives.toast.failedUploadF3d'), 'error');
    },
  });

  const f3dDeleteMutation = useMutation({
    mutationFn: () => api.deleteF3d(archive.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      showToast(t('archives.toast.f3dRemoved'));
    },
    onError: (error: Error) => {
      showToast(error.message || t('archives.toast.failedRemoveF3d'), 'error');
    },
  });

  const timelapseScanMutation = useMutation({
    mutationFn: () => api.scanArchiveTimelapse(archive.id),
    onSuccess: (data) => {
      if (data.status === 'attached') {
        queryClient.invalidateQueries({ queryKey: ['archives'] });
        showToast(t('archives.toast.timelapseAttached', { filename: data.filename }));
      } else if (data.status === 'exists') {
        showToast(t('archives.toast.timelapseAlreadyAttached'));
      } else if (data.status === 'not_found' && data.available_files && data.available_files.length > 0) {
        setAvailableTimelapses(data.available_files);
        setShowTimelapseSelect(true);
      } else {
        showToast(data.message || t('archives.toast.noMatchingTimelapse'), 'warning');
      }
    },
    onError: (error: Error) => {
      showToast(error.message || t('archives.toast.failedScanTimelapse'), 'error');
    },
  });

  const timelapseSelectMutation = useMutation({
    mutationFn: (filename: string) => api.selectArchiveTimelapse(archive.id, filename),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      showToast(t('archives.toast.timelapseAttached', { filename: data.filename }));
      setShowTimelapseSelect(false);
      setAvailableTimelapses([]);
    },
    onError: (error: Error) => {
      showToast(error.message || t('archives.toast.failedAttachTimelapse'), 'error');
    },
  });

  const deleteMutation = useMutation({
    mutationFn: () => api.deleteArchive(archive.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      // Soft-delete shifts the row into the archive trash table —
      // refresh both the trash badge counter on this page's header and
      // the trash list query so a follow-up navigation to the archive
      // trash page picks the new row up immediately. Without this the
      // global 60s staleTime keeps the trash queries on a snapshot that
      // pre-dates this delete.
      queryClient.invalidateQueries({ queryKey: ['archive-trash'] });
      queryClient.invalidateQueries({ queryKey: ['archive-trash-count'] });
      showToast(t('archives.toast.archiveDeleted'));
    },
    onError: () => {
      showToast(t('archives.toast.failedDeleteArchive'), 'error');
    },
  });

  const favoriteMutation = useMutation({
    mutationFn: () => api.toggleFavorite(archive.id),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      showToast(data.is_favorite ? t('archives.toast.addedToFavorites') : t('archives.toast.removedFromFavorites'));
    },
  });

  // Query for linked folders
  const { data: linkedFolders } = useQuery({
    queryKey: ['archive-folders', archive.id],
    queryFn: () => api.getLibraryFoldersByArchive(archive.id),
  });

  const assignProjectMutation = useMutation({
    mutationFn: (projectId: number | null) => api.updateArchive(archive.id, { project_id: projectId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      showToast(t('archives.toast.projectUpdated'));
    },
    onError: () => {
      showToast(t('archives.toast.failedUpdateProject'), 'error');
    },
  });

  const handleContextMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    setContextMenu({ x: e.clientX, y: e.clientY });
  };

  const isGcodeFile = isSlicedFile(archive);

  // PrettyGCode viewer (B.8) — see ArchiveCard for the same pattern.
  const openGcodeViewer = async () => {
    try {
      const resp = await api.getArchivePlates(archive.id);
      if (resp.has_gcode === false) {
        showToast(t('archives.platePicker.noGcode'), 'info');
        return;
      }
      if (resp.is_multi_plate && resp.plates.length > 1) {
        setPlatePickerPlates(resp.plates);
        return;
      }
    } catch {
      // Swallow — fall through to navigate on the first-plate default.
    }
    navigate(`/gcode-viewer?archive=${archive.id}`);
  };

  const contextMenuItems: ContextMenuItem[] = [
    ...(isGcodeFile ? [
      {
        label: t('archives.menu.print'),
        icon: <Printer className="w-4 h-4" />,
        onClick: () => setShowReprint(true),
        disabled: !archive.file_path || !canModify('archives', 'reprint', archive.created_by_id),
        title: !archive.file_path ? t('archives.card.noFileForReprint') : !canModify('archives', 'reprint', archive.created_by_id) ? t('archives.permission.noReprint') : undefined,
      },
      {
        label: t('archives.menu.schedule'),
        icon: <Calendar className="w-4 h-4" />,
        onClick: () => setShowSchedule(true),
        disabled: !archive.file_path || !hasPermission('queue:create'),
        title: !archive.file_path ? t('archives.card.noFileForReprint') : !hasPermission('queue:create') ? t('archives.permission.noAddToQueue') : undefined,
      },
      {
        label: t('archives.menu.openInBambuStudio'),
        icon: <ExternalLink className="w-4 h-4" />,
        onClick: () => {
          const filename = archive.print_name || archive.filename || 'model';
          openInSlicerWithToken(archive.id, filename, 'file', preferredSlicer);
        },
        disabled: !archive.file_path,
        title: !archive.file_path ? t('archives.card.noFileForReprint') : undefined,
      },
    ] : [
      {
        label: t('archives.menu.slice'),
        icon: <ExternalLink className="w-4 h-4" />,
        onClick: () => {
          const filename = archive.print_name || archive.filename || 'model';
          openInSlicerWithToken(archive.id, filename, 'file', preferredSlicer);
        },
      },
      ...(useSlicerApi ? [{
        label: t('slice.actionServerSide', { defaultValue: 'Slice (server-side)' }),
        icon: <Cog className="w-4 h-4" />,
        onClick: () => setShowSlice(true),
        disabled: !archive.file_path || !hasPermission('library:upload'),
        title: !archive.file_path ? t('archives.card.noFileForReprint') : !hasPermission('library:upload') ? t('fileManager.noPermissionSlice', { defaultValue: 'You do not have permission to slice' }) : undefined,
      }] : []),
    ]),
    {
      label: archive.external_url ? t('archives.menu.externalLink') : t('archives.menu.viewOnMakerWorld'),
      // Icon mirrors the label: ``external_url`` overrides → generic Globe;
      // otherwise (MakerWorld plate OR disabled "no link" entry whose label
      // still reads "View on MakerWorld") show the MakerWorld glyph.
      icon: archive.external_url
        ? <Globe className="w-4 h-4" />
        : <MakerWorldIcon className="w-4 h-4" />,
      onClick: () => {
        const url = archive.external_url || archive.makerworld_url;
        if (url) window.open(url, '_blank');
      },
      disabled: !archive.external_url && !archive.makerworld_url,
    },
    { label: '', divider: true, onClick: () => {} },
    {
      label: t('archives.menu.preview3d'),
      icon: <Box className="w-4 h-4" />,
      onClick: () => setShowViewer(true),
    },
    {
      // PrettyGCode viewer (B.8). Multi-plate sliced archives go through
      // a picker first; single-plate archives navigate straight in;
      // source-only archives surface a noGcode toast.
      label: t('archives.menu.gcodeViewer'),
      icon: <Layers className="w-4 h-4" />,
      onClick: () => { openGcodeViewer(); },
    },
    {
      label: t('archives.menu.viewTimelapse'),
      icon: <Film className="w-4 h-4" />,
      onClick: () => setShowTimelapse(true),
      disabled: !archive.timelapse_path,
    },
    {
      label: t('archives.menu.scanForTimelapse'),
      icon: <ScanSearch className="w-4 h-4" />,
      onClick: () => timelapseScanMutation.mutate(),
      disabled: !archive.printer_id || !!archive.timelapse_path || timelapseScanMutation.isPending || !canModify('archives', 'update', archive.created_by_id),
      title: !canModify('archives', 'update', archive.created_by_id) ? t('archives.permission.noUpdateArchives') : undefined,
    },
    {
      label: t('archives.menu.uploadTimelapse'),
      icon: <Upload className="w-4 h-4" />,
      onClick: () => timelapseInputRef.current?.click(),
      disabled: !!archive.timelapse_path || !canModify('archives', 'update', archive.created_by_id),
      title: !canModify('archives', 'update', archive.created_by_id) ? t('archives.permission.noUpdateArchives') : undefined,
    },
    ...(archive.timelapse_path ? [{
      label: t('archives.menu.removeTimelapse'),
      icon: <Trash2 className="w-4 h-4" />,
      onClick: () => setShowDeleteTimelapseConfirm(true),
      danger: true,
      disabled: !canModify('archives', 'update', archive.created_by_id),
      title: !canModify('archives', 'update', archive.created_by_id) ? t('archives.permission.noUpdateArchives') : undefined,
    }] : []),
    { label: '', divider: true, onClick: () => {} },
    {
      label: archive.source_3mf_path ? t('archives.menu.downloadSource3mf') : t('archives.menu.uploadSource3mf'),
      icon: <FileCode className="w-4 h-4" />,
      onClick: () => {
        if (archive.source_3mf_path) {
          api.downloadSource3mf(archive.id).catch((err) => {
            console.error('Source 3MF download failed:', err);
          });
        } else {
          source3mfInputRef.current?.click();
        }
      },
      disabled: !archive.source_3mf_path && !canModify('archives', 'update', archive.created_by_id),
      title: !archive.source_3mf_path && !canModify('archives', 'update', archive.created_by_id) ? t('archives.permission.noUploadFiles') : undefined,
    },
    ...(archive.source_3mf_path ? [{
      label: t('archives.menu.replaceSource3mf'),
      icon: <Upload className="w-4 h-4" />,
      onClick: () => source3mfInputRef.current?.click(),
      disabled: !canModify('archives', 'update', archive.created_by_id),
      title: !canModify('archives', 'update', archive.created_by_id) ? t('archives.permission.noUpdateArchives') : undefined,
    },
    {
      label: t('archives.menu.removeSource3mf'),
      icon: <Trash2 className="w-4 h-4" />,
      onClick: () => setShowDeleteSource3mfConfirm(true),
      danger: true,
      disabled: !canModify('archives', 'update', archive.created_by_id),
      title: !canModify('archives', 'update', archive.created_by_id) ? t('archives.permission.noUpdateArchives') : undefined,
    }] : []),
    {
      label: archive.f3d_path ? t('archives.menu.replaceF3d') : t('archives.menu.uploadF3d'),
      icon: <Box className="w-4 h-4" />,
      onClick: () => f3dInputRef.current?.click(),
      disabled: !canModify('archives', 'update', archive.created_by_id),
      title: !canModify('archives', 'update', archive.created_by_id) ? t('archives.permission.noUpdateArchives') : undefined,
    },
    ...(archive.f3d_path ? [{
      label: t('archives.menu.downloadF3d'),
      icon: <Download className="w-4 h-4" />,
      onClick: () => {
        api.downloadF3d(archive.id).catch((err) => {
          console.error('F3D download failed:', err);
        });
      },
    },
    {
      label: t('archives.menu.removeF3d'),
      icon: <Trash2 className="w-4 h-4" />,
      onClick: () => setShowDeleteF3dConfirm(true),
      danger: true,
      disabled: !canModify('archives', 'update', archive.created_by_id),
      title: !canModify('archives', 'update', archive.created_by_id) ? t('archives.permission.noUpdateArchives') : undefined,
    }] : []),
    { label: '', divider: true, onClick: () => {} },
    {
      label: t('archives.menu.download'),
      icon: <Download className="w-4 h-4" />,
      onClick: () => {
        api.downloadArchive(archive.id, `${archive.print_name || archive.filename}.3mf`).catch((err) => {
          console.error('Archive download failed:', err);
        });
      },
      disabled: !hasPermission('archives:read'),
      title: !hasPermission('archives:read') ? t('archives.permission.noDownload') : undefined,
    },
    {
      label: t('archives.menu.copyDownloadLink'),
      icon: <Copy className="w-4 h-4" />,
      onClick: () => {
        const url = `${window.location.origin}${api.getArchiveDownload(archive.id)}`;
        navigator.clipboard.writeText(url).then(() => {
          showToast(t('archives.toast.linkCopied'));
        }).catch(() => {
          showToast(t('archives.toast.failedCopyLink'), 'error');
        });
      },
      disabled: !hasPermission('archives:read'),
      title: !hasPermission('archives:read') ? t('archives.permission.noCopyLink') : undefined,
    },
    {
      label: t('archives.menu.qrCode'),
      icon: <QrCode className="w-4 h-4" />,
      onClick: () => setShowQRCode(true),
    },
    {
      label: archive.photos?.length ? t('archives.menu.viewPhotosCount', { count: archive.photos.length }) : t('archives.menu.viewPhotos'),
      icon: <Camera className="w-4 h-4" />,
      onClick: () => setShowPhotos(true),
      disabled: !archive.photos?.length,
    },
    {
      label: t('archives.menu.projectPage'),
      icon: <FileText className="w-4 h-4" />,
      onClick: () => setShowProjectPage(true),
    },
    { label: '', divider: true, onClick: () => {} },
    {
      label: archive.is_favorite ? t('archives.menu.removeFromFavorites') : t('archives.menu.addToFavorites'),
      icon: <Star className={`w-4 h-4 ${archive.is_favorite ? 'fill-yellow-400 text-yellow-400' : ''}`} />,
      onClick: () => favoriteMutation.mutate(),
      disabled: !canModify('archives', 'update', archive.created_by_id),
      title: !canModify('archives', 'update', archive.created_by_id) ? t('archives.permission.noUpdateArchives') : undefined,
    },
    {
      label: t('archives.menu.edit'),
      icon: <Pencil className="w-4 h-4" />,
      onClick: () => setShowEdit(true),
      disabled: !canModify('archives', 'update', archive.created_by_id),
      title: !canModify('archives', 'update', archive.created_by_id) ? t('archives.permission.noUpdateArchives') : undefined,
    },
    ...(archive.project_id && archive.project_name ? [{
      label: t('archives.menu.goToProject', { name: archive.project_name }),
      icon: <FolderKanban className="w-4 h-4 text-bambu-green" />,
      onClick: () => window.location.href = '/projects',
    }] : []),
    {
      label: t('archives.menu.addToProject'),
      icon: <FolderKanban className="w-4 h-4" />,
      onClick: () => {},
      submenu: (() => {
        const items: ContextMenuItem[] = [];
        if (archive.project_id) {
          items.push({
            label: t('archives.menu.removeFromProject'),
            icon: <X className="w-4 h-4" />,
            onClick: () => assignProjectMutation.mutate(null),
          });
        }
        if (!projects) {
          items.push({
            label: t('archives.menu.loading'),
            icon: <Loader2 className="w-4 h-4 animate-spin" />,
            onClick: () => {},
            disabled: true,
          });
        } else {
          const activeProjects = projects.filter(p => p.status === 'active');
          if (activeProjects.length === 0) {
            items.push({
              label: t('archives.menu.noProjectsAvailable'),
              icon: <FolderKanban className="w-4 h-4 opacity-50" />,
              onClick: () => {},
              disabled: true,
            });
          } else {
            activeProjects.forEach(p => {
              items.push({
                label: p.name,
                icon: <div className="w-3 h-3 rounded-full flex-shrink-0" style={{ backgroundColor: p.color || '#888' }} />,
                onClick: () => assignProjectMutation.mutate(p.id),
                disabled: archive.project_id === p.id,
              });
            });
          }
        }
        return items;
      })(),
    },
    {
      label: isSelected ? t('archives.menu.deselect') : t('archives.menu.select'),
      icon: isSelected ? <CheckSquare className="w-4 h-4" /> : <Square className="w-4 h-4" />,
      onClick: () => onSelect(archive.id),
    },
    { label: '', divider: true, onClick: () => {} },
    {
      label: t('archives.menu.delete'),
      icon: <Trash2 className="w-4 h-4" />,
      onClick: () => setShowDeleteConfirm(true),
      danger: true,
      disabled: !canModify('archives', 'delete', archive.created_by_id),
      title: !canModify('archives', 'delete', archive.created_by_id) ? t('archives.permission.noDelete') : undefined,
    },
  ];

  return (
    <>
      <div
        data-archive-id={archive.id}
        className={`col-span-full grid grid-cols-subgrid items-center px-4 py-3 hover:bg-bambu-dark-tertiary/30 ${
          isSelected ? 'bg-bambu-green/10' : ''
        }`}
        style={isHighlighted ? { outline: '4px solid #facc15', outlineOffset: '-4px' } : undefined}
        onContextMenu={handleContextMenu}
      >
        <div className="flex items-center gap-2">
          {selectionMode && (
            <button onClick={() => onSelect(archive.id)}>
              {isSelected ? (
                <CheckSquare className="w-4 h-4 text-bambu-green" />
              ) : (
                <Square className="w-4 h-4 text-bambu-gray" />
              )}
            </button>
          )}
          {archive.thumbnail_path ? (
            <img
              src={api.getArchiveThumbnail(archive.id)}
              alt=""
              className="w-10 h-10 object-cover rounded"
            />
          ) : (
            <div className="w-10 h-10 bg-bambu-dark rounded flex items-center justify-center">
              <Image className="w-5 h-5 text-bambu-dark-tertiary" />
            </div>
          )}
        </div>
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <p className="text-white text-sm truncate">{archive.print_name || archive.filename}</p>
            {(() => {
              const badge = getArchiveStatusBadge(archive.status);
              if (!badge) return null;
              const isPrinting = archive.status === 'printing';
              const base = `px-1.5 py-0.5 rounded text-[10px] leading-tight flex-shrink-0 flex items-center gap-1 ${badge.className}`;
              const pulseCls = badge.pulse ? 'animate-pulse' : '';
              if (isPrinting && archive.printer_id) {
                return (
                  <Link
                    to={`/#printer-${archive.printer_id}`}
                    onClick={(e) => e.stopPropagation()}
                    className={`${base} ${pulseCls} hover:brightness-125 transition`}
                    title={t('archives.card.printingClickHint')}
                  >
                    <Loader2 className="w-2.5 h-2.5 animate-spin" />
                    {t(badge.labelKey)}
                  </Link>
                );
              }
              const errorTooltip = archive.error_message || archive.failure_reason || undefined;
              return (
                <span className={`${base} ${pulseCls}`} title={errorTooltip}>
                  {isPrinting && <Loader2 className="w-2.5 h-2.5 animate-spin" />}
                  {t(badge.labelKey)}
                </span>
              );
            })()}
            {archive.duplicate_count > 0 && duplicateSequence > 0 && originalArchiveId && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onNavigateToArchive?.(originalArchiveId);
                }}
                className="px-1.5 py-0.5 rounded text-[10px] leading-tight bg-purple-500/80 hover:bg-purple-600/90 text-white flex-shrink-0 transition-colors flex items-center gap-1"
                title={t('archives.viewOriginalPrint', { id: originalArchiveId })}
              >
                <Copy className="w-3 h-3" />
                #{duplicateSequence}
              </button>
            )}
            {archive.duplicate_count > 0 && duplicateSequence === 0 && (
              <span
                className="px-1.5 py-0.5 rounded text-[10px] leading-tight bg-purple-500/80 text-white flex-shrink-0 flex items-center gap-1"
                title={`${archive.duplicate_count} reprint${archive.duplicate_count === 1 ? '' : 's'}`}
              >
                <GitBranch className="w-3 h-3" />
                +{archive.duplicate_count}
              </span>
            )}
            {archive.swap_compatible && (
              <span className="text-[10px] px-1 py-0.5 bg-amber-500/20 text-amber-400 rounded flex-shrink-0">SWAP</span>
            )}
            {archive.timelapse_path && (
              <span title={t('archives.list.hasTimelapse')}>
                <Film className="w-3.5 h-3.5 text-bambu-green flex-shrink-0" />
              </span>
            )}
            {linkedFolders && linkedFolders.length > 0 && (
              <Link
                to={`/files?folder=${linkedFolders[0].id}`}
                className="flex-shrink-0"
                title={t('archives.card.openFolder', { name: linkedFolders[0].name })}
                onClick={(e) => e.stopPropagation()}
              >
                <FolderOpen className="w-3.5 h-3.5 text-yellow-400" />
              </Link>
            )}
          </div>
          {(archive.filament_type || archive.sliced_for_model) && (
            <div className="flex items-center gap-1.5 mt-0.5">
              {archive.sliced_for_model && (
                <span className="text-xs text-bambu-gray flex items-center gap-1" title={t('archives.card.slicedFor', { model: archive.sliced_for_model })}>
                  <Printer className="w-2.5 h-2.5" />
                  {archive.sliced_for_model}
                </span>
              )}
              {archive.sliced_for_model && archive.filament_type && (
                <span className="text-bambu-gray/50">·</span>
              )}
              {archive.filament_type && (
                <span className="text-xs text-bambu-gray">{archive.filament_type}</span>
              )}
              {archive.filament_color && (
                <div className="flex items-center gap-0.5 flex-wrap">
                  {archive.filament_color.split(',').map((color, i) => (
                    <div
                      key={i}
                      className="w-2.5 h-2.5 rounded-full border border-black/20"
                      style={{ backgroundColor: color }}
                      title={color}
                    />
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
        <div className="text-sm text-bambu-gray whitespace-nowrap">
          {printerName}
        </div>
        <div className="text-sm text-bambu-gray whitespace-nowrap">
          <div>{formatDateTime(archive.created_at, timeFormat, dateFormat)}</div>
          {archive.created_by_username && (
            <div className="flex items-center gap-1 text-xs opacity-75" title={t('archives.card.uploadedBy', { name: archive.created_by_username })}>
              <User className="w-3 h-3" />
              {archive.created_by_username}
            </div>
          )}
        </div>
        <div className="text-sm text-bambu-gray whitespace-nowrap text-right">
          {formatFileSize(archive.file_size)}
        </div>
        <div className="flex justify-end gap-1">
          {isSlicedFile(archive) && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setShowReprint(true)}
              disabled={!canModify('archives', 'reprint', archive.created_by_id)}
              title={!canModify('archives', 'reprint', archive.created_by_id) ? t('archives.card.noPermissionReprint') : t('archives.card.reprint')}
              className="text-bambu-green hover:text-bambu-green-light hover:bg-bambu-green/10"
            >
              <Play className="w-4 h-4" />
            </Button>
          )}
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              const filename = archive.print_name || archive.filename || 'model';
              openInSlicerWithToken(archive.id, filename, 'file', preferredSlicer);
            }}
            title={t('archives.card.openInBambuStudio')}
          >
            <ExternalLink className="w-4 h-4" />
          </Button>
          {(archive.external_url || archive.makerworld_url) && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => window.open((archive.external_url || archive.makerworld_url)!, '_blank')}
              title={archive.external_url ? t('archives.card.externalLink') : t('archives.menu.viewOnMakerWorld')}
            >
              {archive.external_url ? (
                <Globe className="w-4 h-4" />
              ) : (
                <MakerWorldIcon className="w-4 h-4" />
              )}
            </Button>
          )}
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              api.downloadArchive(archive.id, `${archive.print_name || archive.filename}.3mf`).catch((err) => {
                console.error('Archive download failed:', err);
              });
            }}
            title={t('archives.card.download')}
          >
            <Download className="w-4 h-4" />
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setShowEdit(true)}
            disabled={!canModify('archives', 'update', archive.created_by_id)}
            title={!canModify('archives', 'update', archive.created_by_id) ? t('archives.card.noPermissionEdit') : t('archives.card.edit')}
          >
            <Pencil className="w-4 h-4" />
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setShowDeleteConfirm(true)}
            disabled={!canModify('archives', 'delete', archive.created_by_id)}
            title={!canModify('archives', 'delete', archive.created_by_id) ? t('archives.card.noPermissionDelete') : t('archives.card.delete')}
          >
            <Trash2 className="w-4 h-4 text-red-400" />
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={(e) => {
              const rect = e.currentTarget.getBoundingClientRect();
              setContextMenu({ x: rect.left, y: rect.bottom + 4 });
            }}
            title={t('archives.card.moreOptions')}
          >
            <MoreVertical className="w-4 h-4" />
          </Button>
        </div>
      </div>

      {/* Edit Modal */}
      {showEdit && (
        <EditArchiveModal
          archive={archive}
          onClose={() => setShowEdit(false)}
        />
      )}

      {/* 3D Viewer Modal */}
      {showViewer && (
        <ModelViewerModal
          archiveId={archive.id}
          title={archive.print_name || archive.filename}
          fileType={getArchiveFileType(archive.filename)}
          archivePlateIndex={archive.plate_index}
          onClose={() => setShowViewer(false)}
        />
      )}

      {/* Plate picker for the PrettyGCode viewer (B.8). Shown only for
          multi-plate sliced archives; single-plate + source-only flows
          short-circuit before this mounts. */}
      {platePickerPlates && (
        <PlatePickerModal
          plates={platePickerPlates}
          onSelect={(plateIndex) => {
            setPlatePickerPlates(null);
            navigate(`/gcode-viewer?archive=${archive.id}&plate=${plateIndex}`);
          }}
          onClose={() => setPlatePickerPlates(null)}
        />
      )}

      {/* Reprint Modal */}
      {showReprint && (
        <PrintModal
          mode="reprint"
          archiveId={archive.id}
          archiveName={archive.print_name || archive.filename}
          onClose={() => setShowReprint(false)}
        />
      )}

      {/* Server-side slice modal */}
      {showSlice && (
        <SliceModal
          source={{ kind: 'archive', id: archive.id, filename: archive.filename }}
          onClose={() => setShowSlice(false)}
        />
      )}

      {/* Delete Confirmation */}
      {showDeleteConfirm && (
        <ConfirmModal
          title={t('archives.modal.deleteArchive')}
          message={t('archives.modal.deleteConfirm', { name: archive.print_name || archive.filename })}
          confirmText={t('archives.modal.deleteButton')}
          variant="danger"
          onConfirm={() => {
            deleteMutation.mutate();
            setShowDeleteConfirm(false);
          }}
          onCancel={() => setShowDeleteConfirm(false)}
        />
      )}

      {/* Delete Source 3MF Confirmation */}
      {showDeleteSource3mfConfirm && (
        <ConfirmModal
          title={t('archives.modal.removeSource3mf')}
          message={t('archives.modal.removeSource3mfConfirm', { name: archive.print_name || archive.filename })}
          confirmText={t('archives.modal.removeButton')}
          variant="danger"
          onConfirm={() => {
            source3mfDeleteMutation.mutate();
            setShowDeleteSource3mfConfirm(false);
          }}
          onCancel={() => setShowDeleteSource3mfConfirm(false)}
        />
      )}

      {/* Delete F3D Confirmation */}
      {showDeleteF3dConfirm && (
        <ConfirmModal
          title={t('archives.modal.removeF3d')}
          message={t('archives.modal.removeF3dConfirm', { name: archive.print_name || archive.filename })}
          confirmText={t('archives.modal.removeButton')}
          variant="danger"
          onConfirm={() => {
            f3dDeleteMutation.mutate();
            setShowDeleteF3dConfirm(false);
          }}
          onCancel={() => setShowDeleteF3dConfirm(false)}
        />
      )}

      {/* Delete Timelapse Confirmation */}
      {showDeleteTimelapseConfirm && (
        <ConfirmModal
          title={t('archives.modal.removeTimelapse')}
          message={t('archives.modal.removeTimelapseConfirm', { name: archive.print_name || archive.filename })}
          confirmText={t('archives.modal.removeButton')}
          variant="danger"
          onConfirm={() => {
            timelapseDeleteMutation.mutate();
            setShowDeleteTimelapseConfirm(false);
          }}
          onCancel={() => setShowDeleteTimelapseConfirm(false)}
        />
      )}

      {/* Context Menu */}
      {contextMenu && (
        <ContextMenu
          x={contextMenu.x}
          y={contextMenu.y}
          items={contextMenuItems}
          onClose={() => setContextMenu(null)}
        />
      )}

      {/* Timelapse Viewer Modal */}
      {showTimelapse && archive.timelapse_path && (
        <TimelapseViewer
          src={api.getArchiveTimelapse(archive.id)}
          title={t('archives.modal.timelapse', { name: archive.print_name || archive.filename })}
          downloadFilename={`${archive.print_name || archive.filename}_timelapse.mp4`}
          archiveId={archive.id}
          onClose={() => setShowTimelapse(false)}
          onEdit={() => {
            queryClient.invalidateQueries({ queryKey: ['archives'] });
            setShowTimelapse(false);
          }}
        />
      )}

      {/* Timelapse Selection Modal */}
      {showTimelapseSelect && availableTimelapses.length > 0 && (
        <div className="fixed inset-0 bg-black/80 flex items-center justify-center z-50 p-4">
          <div className="bg-card-dark rounded-lg max-w-lg w-full max-h-[80vh] flex flex-col">
            <div className="flex items-center justify-between p-4 border-b border-gray-700">
              <div>
                <h3 className="text-lg font-semibold text-white">{t('archives.modal.selectTimelapse')}</h3>
                <p className="text-sm text-gray-400 mt-1">
                  {t('archives.modal.selectTimelapseDesc')}
                </p>
              </div>
              <button
                onClick={() => {
                  setShowTimelapseSelect(false);
                  setAvailableTimelapses([]);
                }}
                className="text-gray-400 hover:text-white p-1"
              >
                <X className="w-5 h-5" />
              </button>
            </div>
            <div className="overflow-y-auto flex-1 p-2">
              {availableTimelapses.map((file) => (
                <button
                  key={file.name}
                  onClick={() => timelapseSelectMutation.mutate(file.name)}
                  disabled={timelapseSelectMutation.isPending}
                  className="w-full text-left p-3 rounded-lg hover:bg-gray-700 transition-colors mb-1"
                >
                  <div className="font-medium text-white">{file.name}</div>
                  <div className="text-sm text-gray-400 flex gap-3">
                    <span>{formatFileSize(file.size)}</span>
                    {file.mtime && (
                      <span>{formatDateOnly(file.mtime)}</span>
                    )}
                  </div>
                </button>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* QR Code Modal */}
      {showQRCode && (
        <QRCodeModal
          archiveId={archive.id}
          archiveName={archive.print_name || archive.filename}
          onClose={() => setShowQRCode(false)}
        />
      )}

      {/* Photo Gallery Modal */}
      {showPhotos && archive.photos && (
        <PhotoGalleryModal
          archiveId={archive.id}
          archiveName={archive.print_name || archive.filename}
          photos={archive.photos}
          onClose={() => setShowPhotos(false)}
          onDelete={async (filename) => {
            try {
              await api.deleteArchivePhoto(archive.id, filename);
              queryClient.invalidateQueries({ queryKey: ['archives'] });
              showToast(t('archives.toast.photoDeleted'));
            } catch {
              showToast(t('archives.toast.failedDeletePhoto'), 'error');
            }
          }}
        />
      )}

      {/* Project Page Modal */}
      {showProjectPage && (
        <ProjectPageModal
          archiveId={archive.id}
          archiveName={archive.print_name || archive.filename}
          onClose={() => setShowProjectPage(false)}
        />
      )}

      {/* Schedule Modal */}
      {showSchedule && (
        <PrintModal
          mode="add-to-queue"
          archiveId={archive.id}
          archiveName={archive.print_name || archive.filename}
          onClose={() => setShowSchedule(false)}
        />
      )}

      {/* Hidden file input for source 3MF upload */}
      <input
        ref={source3mfInputRef}
        type="file"
        accept=".3mf"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) {
            source3mfUploadMutation.mutate(file);
          }
          e.target.value = '';
        }}
      />
      {/* Hidden file input for F3D upload */}
      <input
        ref={f3dInputRef}
        type="file"
        accept=".f3d"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) {
            f3dUploadMutation.mutate(file);
          }
          e.target.value = '';
        }}
      />
      {/* Hidden file input for timelapse upload */}
      <input
        ref={timelapseInputRef}
        type="file"
        accept=".mp4,.avi,.mkv"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) {
            timelapseUploadMutation.mutate(file);
          }
          e.target.value = '';
        }}
      />
    </>
  );
}

type SortOption = 'date-desc' | 'date-asc' | 'name-asc' | 'name-desc' | 'size-desc' | 'size-asc';
type ViewMode = 'grid' | 'list' | 'calendar';
type Collection = 'all' | 'recent' | 'this-week' | 'this-month' | 'favorites' | 'printed' | 'failed' | 'duplicates';

// `printed` is a server-side filter in backend/app/services/archive.py.
// The pre-0.4.2 `not-printed` chip showed `status='archived'` rows from
// the now-removed manual-upload + VP-placeholder + pending-approval
// writers (Audits 1+2+3 + the m041 drain). Those rows can no longer
// exist, so the chip was dropped along with the writers.
const collections: { id: Collection; labelKey: string; icon: React.ReactNode }[] = [
  { id: 'all', labelKey: 'archives.page.collection.all', icon: <FolderOpen className="w-4 h-4" /> },
  { id: 'recent', labelKey: 'archives.page.collection.recent', icon: <Clock className="w-4 h-4" /> },
  { id: 'this-week', labelKey: 'archives.page.collection.thisWeek', icon: <Calendar className="w-4 h-4" /> },
  { id: 'this-month', labelKey: 'archives.page.collection.thisMonth', icon: <Calendar className="w-4 h-4" /> },
  { id: 'favorites', labelKey: 'archives.page.collection.favorites', icon: <Star className="w-4 h-4" /> },
  { id: 'printed', labelKey: 'archives.page.collection.printed', icon: <Printer className="w-4 h-4" /> },
  { id: 'failed', labelKey: 'archives.page.collection.failed', icon: <AlertCircle className="w-4 h-4" /> },
  { id: 'duplicates', labelKey: 'archives.page.collection.duplicates', icon: <Copy className="w-4 h-4" /> },
];

export function ArchivesPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { hasAnyPermission, hasPermission } = useAuth();
  const searchInputRef = useRef<HTMLInputElement>(null);
  const [searchParams, setSearchParams] = useSearchParams();
  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState(search);
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(() => {
    const saved = localStorage.getItem('archivePerPage');
    return saved ? Number(saved) : 24;
  });
  // Printer filter: URL `?printer=<id>` wins over localStorage on mount so
  // entry points like the queue-card footer link can pre-select a printer.
  const [filterPrinter, setFilterPrinter] = useState<number | null>(() => {
    const urlPrinter = searchParams.get('printer');
    if (urlPrinter) return Number(urlPrinter);
    const saved = localStorage.getItem('archiveFilterPrinter');
    return saved ? Number(saved) : null;
  });
  const [filterMaterial, setFilterMaterial] = useState<string | null>(() =>
    localStorage.getItem('archiveFilterMaterial')
  );
  const [filterColors, setFilterColors] = useState<Set<string>>(() => {
    const saved = localStorage.getItem('archiveFilterColors');
    return saved ? new Set(JSON.parse(saved)) : new Set();
  });
  const [colorFilterMode, setColorFilterMode] = useState<'or' | 'and'>(() =>
    (localStorage.getItem('archiveColorFilterMode') as 'or' | 'and') || 'or'
  );
  const [filterFavorites, setFilterFavorites] = useState(() =>
    localStorage.getItem('archiveFilterFavorites') === 'true'
  );
  const [hideFailed, setHideFailed] = useState(() =>
    localStorage.getItem('archiveHideFailed') === 'true'
  );
  const [hideDuplicates, setHideDuplicates] = useState(() =>
    localStorage.getItem('archiveHideDuplicates') === 'true'
  );
  const [filterTag, setFilterTag] = useState<string | null>(() =>
    localStorage.getItem('archiveFilterTag')
  );
  const [filterFileType, setFilterFileType] = useState<'all' | 'gcode' | 'source'>(() =>
    (localStorage.getItem('archiveFilterFileType') as 'all' | 'gcode' | 'source') || 'all'
  );
  const [showPurgeModal, setShowPurgeModal] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [isSelectionMode, setIsSelectionMode] = useState(false);
  const [showBulkDeleteConfirm, setShowBulkDeleteConfirm] = useState(false);
  const [showBatchTag, setShowBatchTag] = useState(false);
  const [showBatchProject, setShowBatchProject] = useState(false);
  const [viewMode, setViewMode] = useState<ViewMode>(() =>
    (localStorage.getItem('archiveViewMode') as ViewMode) || 'grid'
  );
  const [sortBy, setSortBy] = useState<SortOption>(() =>
    (localStorage.getItem('archiveSortBy') as SortOption) || 'date-desc'
  );
  // Derived field+direction split for the two-control sort UI (select + toggle).
  // Keeps `sortBy` as the single source-of-truth so localStorage and the
  // backend query param stay backward-compatible.
  const sortField = sortBy.split('-')[0] as 'date' | 'name' | 'size';
  const sortDir = sortBy.split('-')[1] as 'asc' | 'desc';
  const setSortField = (field: 'date' | 'name' | 'size') => {
    setSortBy(`${field}-${sortDir}` as SortOption);
    setPage(1);
  };
  const toggleSortDir = () => {
    const next = sortDir === 'asc' ? 'desc' : 'asc';
    setSortBy(`${sortField}-${next}` as SortOption);
    setPage(1);
  };
  const [collection, setCollection] = useState<Collection>(() =>
    (localStorage.getItem('archiveCollection') as Collection) || 'all'
  );
  const [showExportMenu, setShowExportMenu] = useState(false);
  const [isExporting, setIsExporting] = useState(false);
  const [showCompareModal, setShowCompareModal] = useState(false);
  const [showTagManagement, setShowTagManagement] = useState(false);
  const [highlightedArchiveId, setHighlightedArchiveId] = useState<number | null>(null);
  const [pendingNavigationArchiveId, setPendingNavigationArchiveId] = useState<number | null>(null);



  const handleNavigateToArchive = useCallback((archiveId: number) => {
    setPendingNavigationArchiveId(archiveId);
    setHighlightedArchiveId(archiveId);
  }, []);

  // Clear highlight after 5 seconds and scroll to highlighted element
  useEffect(() => {
    if (highlightedArchiveId) {
      // Scroll to highlighted element after a short delay (to let the view render)
      const scrollTimer = setTimeout(() => {
        const element = document.querySelector(`[data-archive-id="${highlightedArchiveId}"]`);
        if (element) {
          element.scrollIntoView({ behavior: 'smooth', block: 'center' });
        } else if (pendingNavigationArchiveId === highlightedArchiveId) {
          showToast(t('archives.originalPrintNotVisible'), 'warning');
        }
        if (pendingNavigationArchiveId === highlightedArchiveId) {
          setPendingNavigationArchiveId(null);
        }
      }, 100);

      // Clear highlight after 5 seconds
      const clearTimer = setTimeout(() => setHighlightedArchiveId(null), 5000);
      return () => {
        clearTimeout(scrollTimer);
        clearTimeout(clearTimer);
      };
    }
  }, [highlightedArchiveId, pendingNavigationArchiveId, showToast, t]);

  const archiveParams: ArchiveListParams = {
    page,
    per_page: perPage,
    printer_id: filterPrinter || undefined,
    search: debouncedSearch || undefined,
    collection: collection !== 'all' ? collection : undefined,
    material: filterMaterial || undefined,
    colors: filterColors.size > 0 ? [...filterColors].join(',') : undefined,
    color_mode: filterColors.size > 1 ? colorFilterMode : undefined,
    favorites_only: filterFavorites || undefined,
    hide_failed: hideFailed || undefined,
    hide_duplicates: hideDuplicates || undefined,
    tag: filterTag || undefined,
    file_type: filterFileType !== 'all' ? filterFileType : undefined,
    sort_by: sortBy,
  };

  const { data: archivesResponse, isLoading } = useQuery({
    queryKey: ['archives', archiveParams],
    queryFn: () => api.getArchives(archiveParams),
    placeholderData: (prev) => prev,
  });

  const archives = archivesResponse?.data;
  const paginationMeta = archivesResponse?.meta;

  const { data: printers } = useQuery({
    queryKey: ['printers'],
    queryFn: api.getPrinters,
  });

  const { data: filterOptions } = useQuery({
    queryKey: ['archive-filter-options'],
    queryFn: api.getArchiveFilterOptions,
    staleTime: 60_000,
  });

  // Calendar: fetch last 30 days (separate lightweight query)
  const calendarDateFrom = useMemo(() => {
    const d = new Date();
    d.setDate(d.getDate() - 29);
    return d.toISOString().split('T')[0];
  }, []);
  const calendarDateTo = useMemo(() => new Date().toISOString().split('T')[0], []);

  const { data: calendarArchives } = useQuery({
    queryKey: ['archives-calendar', calendarDateFrom, calendarDateTo],
    queryFn: () => api.getArchivesSlim(calendarDateFrom, calendarDateTo),
    enabled: viewMode === 'calendar',
  });

  const { data: projects } = useQuery({
    queryKey: ['projects'],
    queryFn: () => api.getProjects(),
  });

  // Archive trash count for the header badge (#1008 follow-up). Empty/error
  // is silently treated as zero so a broken trash endpoint doesn't break
  // the Archives page.
  const { data: archiveTrashCount } = useQuery({
    queryKey: ['archive-trash-count'],
    queryFn: async () => {
      try {
        const res = await api.listArchiveTrash(1, 0);
        return res.total;
      } catch {
        return 0;
      }
    },
    staleTime: 30_000,
  });

  const { data: settings } = useQuery({
    queryKey: ['settings'],
    queryFn: api.getSettings,
  });

  const timeFormat: TimeFormat = settings?.time_format || 'system';
  const dateFormat: DateFormat = settings?.date_format || 'system';
  const preferredSlicer: SlicerType = settings?.preferred_slicer || 'bambu_studio';
  const currency = getCurrencySymbol(settings?.currency || 'USD');

  const bulkDeleteMutation = useMutation({
    mutationFn: async (ids: number[]) => {
      await Promise.all(ids.map((id) => api.deleteArchive(id)));
      return ids.length;
    },
    onSuccess: (count) => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      queryClient.invalidateQueries({ queryKey: ['archive-filter-options'] });
      queryClient.invalidateQueries({ queryKey: ['archive-trash'] });
      queryClient.invalidateQueries({ queryKey: ['archive-trash-count'] });
      setSelectedIds(new Set());
      showToast(t('archives.page.archivesDeleted', { count }));
    },
    onError: () => {
      showToast(t('archives.toast.failedDeleteArchives'), 'error');
    },
  });


  // Strip `?printer=<id>` from the URL once consumed so reload/back falls
  // back to localStorage instead of re-forcing the filter.
  useEffect(() => {
    if (searchParams.has('printer')) {
      const next = new URLSearchParams(searchParams);
      next.delete('printer');
      setSearchParams(next, { replace: true });
    }
  }, [searchParams, setSearchParams]);

  // Persist all filters to localStorage
  useEffect(() => {
    if (filterPrinter !== null) {
      localStorage.setItem('archiveFilterPrinter', filterPrinter.toString());
    } else {
      localStorage.removeItem('archiveFilterPrinter');
    }
  }, [filterPrinter]);

  useEffect(() => {
    if (filterMaterial) {
      localStorage.setItem('archiveFilterMaterial', filterMaterial);
    } else {
      localStorage.removeItem('archiveFilterMaterial');
    }
  }, [filterMaterial]);

  useEffect(() => {
    localStorage.setItem('archiveFilterColors', JSON.stringify([...filterColors]));
  }, [filterColors]);

  useEffect(() => {
    localStorage.setItem('archiveColorFilterMode', colorFilterMode);
  }, [colorFilterMode]);

  useEffect(() => {
    localStorage.setItem('archiveFilterFavorites', filterFavorites.toString());
  }, [filterFavorites]);

  useEffect(() => {
    localStorage.setItem('archiveHideFailed', hideFailed.toString());
  }, [hideFailed]);

  useEffect(() => {
    localStorage.setItem('archiveHideDuplicates', hideDuplicates.toString());
  }, [hideDuplicates]);

  useEffect(() => {
    if (filterTag) {
      localStorage.setItem('archiveFilterTag', filterTag);
    } else {
      localStorage.removeItem('archiveFilterTag');
    }
  }, [filterTag]);

  useEffect(() => {
    localStorage.setItem('archiveFilterFileType', filterFileType);
  }, [filterFileType]);

  useEffect(() => {
    localStorage.setItem('archiveViewMode', viewMode);
  }, [viewMode]);

  useEffect(() => {
    localStorage.setItem('archiveSortBy', sortBy);
  }, [sortBy]);

  useEffect(() => {
    localStorage.setItem('archiveCollection', collection);
  }, [collection]);

  useEffect(() => {
    localStorage.setItem('archivePerPage', String(perPage));
  }, [perPage]);

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(timer);
  }, [search]);


  const printerMap = new Map(printers?.map((p) => [p.id, p.name]) || []);

  // Get unique filter values from server
  const uniqueMaterials = filterOptions?.materials || [];
  const uniqueColors = filterOptions?.colors || [];
  const uniqueTags = filterOptions?.tags || [];

  const selectionMode = isSelectionMode || selectedIds.size > 0;

  const toggleSelect = (id: number) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  const selectAll = () => {
    if (archives) {
      setSelectedIds(new Set(archives.map((a) => a.id)));
    }
  };

  const clearSelection = () => {
    setSelectedIds(new Set());
    setIsSelectionMode(false);
  };

  const toggleColor = (color: string) => {
    setFilterColors((prev) => {
      const next = new Set(prev);
      if (next.has(color)) {
        next.delete(color);
      } else {
        next.add(color);
      }
      return next;
    });
    setPage(1);
  };

  const clearColorFilter = () => {
    setFilterColors(new Set());
  };

  const clearTopFilters = () => {
    setSearch('');
    setFilterPrinter(null);
    setFilterMaterial(null);
    setFilterFavorites(false);
    setHideFailed(false);
    setHideDuplicates(false);
    setFilterTag(null);
    setFilterFileType('all');
    setPage(1);
  };

  const hasTopFilters = search || filterPrinter || filterMaterial || filterFavorites || hideFailed || hideDuplicates || filterTag || filterFileType !== 'all';

  // Keyboard shortcuts. Archive uploads were removed in 0.4.2 (Audit-1):
  // archives are now strictly the print history of record — drag-drop +
  // upload paths live on Printer (drop = upload + print), Library (file
  // manager modal), and the slicer-facing Virtual Printer FTP. The `u`
  // hotkey was dropped along with the page-wide drop zone + UploadModal.
  const handleKeyDown = useCallback((e: KeyboardEvent) => {
    const target = e.target as HTMLElement;
    // Ignore if typing in an input/textarea
    if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable) {
      if (e.key === 'Escape') {
        target.blur();
      }
      return;
    }

    switch (e.key) {
      case '/':
        e.preventDefault();
        searchInputRef.current?.focus();
        break;
      case 'Escape':
        if (selectionMode) {
          clearSelection();
        }
        break;
    }
  }, [selectionMode]);

  useEffect(() => {
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [handleKeyDown]);

  return (
    <div className="p-4 md:p-6 relative">
      {/* Selection Toolbar */}
      {selectionMode && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-40 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl px-4 py-3 flex items-center gap-4">
          <Button variant="secondary" size="sm" onClick={clearSelection}>
            <X className="w-4 h-4" />
            {t('common.close')}
          </Button>
          <div className="w-px h-6 bg-bambu-dark-tertiary" />
          <span className="text-white font-medium">
            {t('archives.page.selected', { count: selectedIds.size })}
          </span>
          <div className="w-px h-6 bg-bambu-dark-tertiary" />
          <Button variant="secondary" size="sm" onClick={selectAll}>
            {t('archives.page.selectAll')}
          </Button>
          <div className="w-px h-6 bg-bambu-dark-tertiary" />
          <Button
            variant="secondary"
            size="sm"
            onClick={() => setShowBatchTag(true)}
            disabled={!hasAnyPermission('archives:update_own', 'archives:update_all')}
            title={!hasAnyPermission('archives:update_own', 'archives:update_all') ? t('archives.permission.noUpdateArchives') : undefined}
          >
            <Tag className="w-4 h-4" />
            {t('archives.page.tags')}
          </Button>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => setShowBatchProject(true)}
            disabled={!hasAnyPermission('archives:update_own', 'archives:update_all')}
            title={!hasAnyPermission('archives:update_own', 'archives:update_all') ? t('archives.permission.noUpdateArchives') : undefined}
          >
            <FolderKanban className="w-4 h-4" />
            {t('archives.page.project')}
          </Button>
          <Button
            variant="secondary"
            size="sm"
            disabled={!hasAnyPermission('archives:update_own', 'archives:update_all')}
            title={!hasAnyPermission('archives:update_own', 'archives:update_all') ? t('archives.permission.noUpdateArchives') : undefined}
            onClick={() => {
              const ids = Array.from(selectedIds);
              Promise.all(ids.map(id => api.toggleFavorite(id)))
                .then(() => {
                  queryClient.invalidateQueries({ queryKey: ['archives'] });
                  showToast(t('archives.page.toggledFavorites', { count: ids.length }));
                })
                .catch(() => {
                  showToast(t('archives.toast.failedUpdateFavorites'), 'error');
                });
            }}
          >
            <Star className="w-4 h-4" />
            {t('archives.page.favorite')}
          </Button>
          <Button
            size="sm"
            className="bg-red-500 hover:bg-red-600"
            onClick={() => setShowBulkDeleteConfirm(true)}
            disabled={!hasAnyPermission('archives:delete_own', 'archives:delete_all')}
            title={!hasAnyPermission('archives:delete_own', 'archives:delete_all') ? t('archives.permission.noDelete') : undefined}
          >
            <Trash2 className="w-4 h-4" />
            {t('common.delete')}
          </Button>
        </div>
      )}

      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 mb-4">
        <div>
          <h1 className="text-2xl font-bold text-white">{t('archives.page.title')}</h1>
          {viewMode === 'calendar' && (
            <p className="text-bambu-gray text-sm">{t('archives.calendarView')}</p>
          )}
          {(viewMode === 'grid' || viewMode === 'list') && paginationMeta && (
            <p className="text-bambu-gray text-sm">
              {t('common.showingRange', {
                from: ((paginationMeta.current_page - 1) * paginationMeta.per_page) + 1,
                to: Math.min(paginationMeta.current_page * paginationMeta.per_page, paginationMeta.total),
                total: paginationMeta.total,
              })}
            </p>
          )}
        </div>
        <div className="flex items-center gap-2 sm:gap-3 flex-wrap">
          {/* View mode toggle - matches PrintersPage card-size selector style */}
          <div className="flex items-center bg-bambu-dark rounded-lg border border-bambu-dark-tertiary">
            <button
              onClick={() => setViewMode('grid')}
              className={`px-2 py-1.5 transition-colors rounded-l-lg ${
                viewMode === 'grid'
                  ? 'bg-bambu-green text-white'
                  : 'text-bambu-gray hover:bg-bambu-dark-tertiary hover:text-white'
              }`}
              title={t('archives.gridView')}
            >
              <LayoutGrid className="w-4 h-4" />
            </button>
            <button
              onClick={() => setViewMode('list')}
              className={`px-2 py-1.5 transition-colors ${
                viewMode === 'list'
                  ? 'bg-bambu-green text-white'
                  : 'text-bambu-gray hover:bg-bambu-dark-tertiary hover:text-white'
              }`}
              title={t('archives.listView')}
            >
              <List className="w-4 h-4" />
            </button>
            <button
              onClick={() => setViewMode('calendar')}
              className={`px-2 py-1.5 transition-colors rounded-r-lg ${
                viewMode === 'calendar'
                  ? 'bg-bambu-green text-white'
                  : 'text-bambu-gray hover:bg-bambu-dark-tertiary hover:text-white'
              }`}
              title={t('archives.calendarView')}
            >
              <CalendarDays className="w-4 h-4" />
            </button>
          </div>

          <div className="w-px h-6 bg-bambu-dark-tertiary" />

          {/* Export dropdown */}
          <div className="relative">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setShowExportMenu(!showExportMenu)}
              disabled={isExporting}
            >
              {isExporting ? (
                <Loader2 className="w-4 h-4 mr-2 animate-spin" />
              ) : (
                <FileSpreadsheet className="w-4 h-4 mr-2" />
              )}
              {t('common.export')}
            </Button>
            {showExportMenu && (
              <div className="absolute right-0 top-full mt-1 w-48 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl z-20">
                <button
                  className="w-full px-4 py-2 text-left text-white hover:bg-bambu-dark-tertiary transition-colors flex items-center gap-2 rounded-t-lg"
                  onClick={async () => {
                    setShowExportMenu(false);
                    setIsExporting(true);
                    try {
                      const { blob, filename } = await api.exportArchives({
                        format: 'csv',
                        printerId: filterPrinter || undefined,
                        status: collection === 'failed' ? 'failed' : undefined,
                        search: search || undefined,
                      });
                      const url = URL.createObjectURL(blob);
                      const a = document.createElement('a');
                      a.href = url;
                      a.download = filename;
                      a.click();
                      URL.revokeObjectURL(url);
                      showToast(t('archives.toast.exportDownloaded'));
                    } catch {
                      showToast(t('archives.toast.exportFailed'), 'error');
                    } finally {
                      setIsExporting(false);
                    }
                  }}
                >
                  <FileText className="w-4 h-4" />
                  {t('archives.page.exportAsCsv')}
                </button>
                <button
                  className="w-full px-4 py-2 text-left text-white hover:bg-bambu-dark-tertiary transition-colors flex items-center gap-2 rounded-b-lg"
                  onClick={async () => {
                    setShowExportMenu(false);
                    setIsExporting(true);
                    try {
                      const { blob, filename } = await api.exportArchives({
                        format: 'xlsx',
                        printerId: filterPrinter || undefined,
                        status: collection === 'failed' ? 'failed' : undefined,
                        search: search || undefined,
                      });
                      const url = URL.createObjectURL(blob);
                      const a = document.createElement('a');
                      a.href = url;
                      a.download = filename;
                      a.click();
                      URL.revokeObjectURL(url);
                      showToast(t('archives.toast.exportDownloaded'));
                    } catch {
                      showToast(t('archives.toast.exportFailed'), 'error');
                    } finally {
                      setIsExporting(false);
                    }
                  }}
                >
                  <FileSpreadsheet className="w-4 h-4" />
                  {t('archives.page.exportAsExcel')}
                </button>
              </div>
            )}
          </div>
          {/* Compare button (only when 2-5 items selected) */}
          {selectedIds.size >= 2 && selectedIds.size <= 5 && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => setShowCompareModal(true)}
            >
              <GitCompare className="w-4 h-4 mr-2" />
              {t('archives.page.compare', { count: selectedIds.size })}
            </Button>
          )}
          {!selectionMode && (
            <Button variant="outline" size="sm" onClick={() => setIsSelectionMode(true)}>
              <CheckSquare className="w-4 h-4 mr-2" />
              {t('archives.page.select')}
            </Button>
          )}
          {hasAnyPermission('archives:delete_own', 'archives:delete_all') && (
            <TrashSplitButton
              trashHref="/archives/trash"
              trashLabel={t('archiveTrash.headerButton')}
              trashTooltip={t('archiveTrash.headerTooltip')}
              count={archiveTrashCount}
              onPurgeClick={hasPermission('archives:purge') ? () => setShowPurgeModal(true) : undefined}
              purgeLabel={t('archivePurge.headerButton')}
              purgeTooltip={t('archivePurge.headerTooltip')}
            />
          )}
        </div>
      </div>

      {/* Pagination row - only rendered when there are multiple pages */}
      {(viewMode === 'grid' || viewMode === 'list') && paginationMeta && paginationMeta.last_page > 1 && (
        <div className="flex items-center justify-end gap-2 mb-4">
          <button onClick={() => setPage(1)} disabled={page <= 1} className="p-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white disabled:opacity-50 hover:bg-bambu-dark-secondary">
            <ChevronsLeft className="w-4 h-4" />
          </button>
          <button onClick={() => setPage(p => Math.max(1, p - 1))} disabled={page <= 1} className="p-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white disabled:opacity-50 hover:bg-bambu-dark-secondary">
            <ChevronLeft className="w-4 h-4" />
          </button>
          <span className="text-sm text-bambu-gray">{paginationMeta.current_page} / {paginationMeta.last_page}</span>
          <button onClick={() => setPage(p => Math.min(paginationMeta.last_page, p + 1))} disabled={page >= paginationMeta.last_page} className="p-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white disabled:opacity-50 hover:bg-bambu-dark-secondary">
            <ChevronRight className="w-4 h-4" />
          </button>
          <button onClick={() => setPage(paginationMeta.last_page)} disabled={page >= paginationMeta.last_page} className="p-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white disabled:opacity-50 hover:bg-bambu-dark-secondary">
            <ChevronsRight className="w-4 h-4" />
          </button>
        </div>
      )}

      {/* Filters (hidden in log/calendar views) */}
      {(viewMode === 'grid' || viewMode === 'list') && (
        <div className="flex flex-col gap-2 mb-4 p-3 bg-bambu-dark-secondary rounded-lg border border-bambu-dark-tertiary">
          {/* Search - full width */}
          <div className="w-full relative h-9">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-bambu-gray/50" />
            <input
              ref={searchInputRef}
              type="text"
              placeholder={t('archives.searchPlaceholder')}
              className="w-full h-9 pl-10 pr-4 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm placeholder:text-bambu-gray/50 focus:border-bambu-green focus:outline-none"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          {/* Filters - horizontal scroll on mobile */}
          <div className="flex gap-2 md:gap-3 overflow-x-auto pb-1 md:pb-0 -mx-3 px-3 md:mx-0 md:px-0 md:flex-wrap scrollbar-hide w-full">
            {/* Collection filter */}
            <div className="flex items-center gap-2 flex-shrink-0 md:flex-shrink md:flex-1 md:min-w-0">
              <select
                className="px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none w-full"
                value={collection}
                onChange={(e) => { setCollection(e.target.value as Collection); setPage(1); }}
              >
                {collections.map((c) => (
                  <option key={c.id} value={c.id}>{t(c.labelKey)}</option>
                ))}
              </select>
            </div>
            <div className="flex items-center gap-2 flex-shrink-0 md:flex-shrink md:flex-1 md:min-w-0">
              <Filter className="w-4 h-4 text-bambu-gray hidden md:block flex-shrink-0" />
              <select
                className="px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none w-full"
                value={filterPrinter || ''}
                onChange={(e) => {
                  setFilterPrinter(e.target.value ? Number(e.target.value) : null);
                  setPage(1);
                }}
              >
                <option value="">{t('archives.page.allPrinters')}</option>
                {printers?.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="flex items-center gap-2 flex-shrink-0 md:flex-shrink md:flex-1 md:min-w-0">
              <Package className="w-4 h-4 text-bambu-gray hidden md:block flex-shrink-0" />
              <select
                className="px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none w-full"
                value={filterMaterial || ''}
                onChange={(e) => {
                  setFilterMaterial(e.target.value || null);
                  setPage(1);
                }}
              >
                <option value="">{t('archives.page.allMaterials')}</option>
                {uniqueMaterials.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </div>
            <div className="flex items-center gap-2 flex-shrink-0 md:flex-shrink md:flex-1 md:min-w-0">
              <FileCode className="w-4 h-4 text-bambu-gray hidden md:block flex-shrink-0" />
              <select
                className="px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none w-full"
                value={filterFileType}
                onChange={(e) => { setFilterFileType(e.target.value as 'all' | 'gcode' | 'source'); setPage(1); }}
              >
                <option value="all">{t('archives.page.allFiles')}</option>
                <option value="gcode">{t('archives.page.slicedGcode')}</option>
                <option value="source">{t('archives.page.sourceOnly')}</option>
              </select>
            </div>
            {collection !== 'favorites' && (
              <button
                onClick={() => { setFilterFavorites(!filterFavorites); setPage(1); }}
                className={`flex items-center gap-2 px-3 py-2 rounded-lg border transition-colors flex-shrink-0 ${
                  filterFavorites
                    ? 'bg-yellow-500/20 border-yellow-500 text-yellow-400'
                    : 'bg-bambu-dark border-bambu-dark-tertiary text-bambu-gray hover:text-white'
                }`}
                title={filterFavorites ? t('archives.showAll') : t('archives.showFavoritesOnly')}
              >
                <Star className={`w-4 h-4 ${filterFavorites ? 'fill-yellow-400' : ''}`} />
                <span className="text-sm hidden md:inline">{t('archives.page.favorites')}</span>
              </button>
            )}
            {collection !== 'failed' && (
              <button
                onClick={() => { setHideFailed(!hideFailed); setPage(1); }}
                className={`flex items-center gap-2 px-3 py-2 rounded-lg border transition-colors flex-shrink-0 ${
                  hideFailed
                    ? 'bg-red-500/20 border-red-500 text-red-400'
                    : 'bg-bambu-dark border-bambu-dark-tertiary text-bambu-gray hover:text-white'
                }`}
                title={hideFailed ? t('archives.showFailedPrints') : t('archives.hideFailedPrints')}
              >
                <AlertCircle className="w-4 h-4" />
                <span className="text-sm hidden md:inline">{t('archives.page.hideFailed')}</span>
              </button>
            )}
            {collection !== 'duplicates' && (
              <button
                onClick={() => { setHideDuplicates(!hideDuplicates); setPage(1); }}
                className={`flex items-center gap-2 px-3 py-2 rounded-lg border transition-colors flex-shrink-0 ${
                  hideDuplicates
                    ? 'bg-purple-500/20 border-purple-500 text-purple-400'
                    : 'bg-bambu-dark border-bambu-dark-tertiary text-bambu-gray hover:text-white'
                }`}
                title={t('archives.hideDuplicates')}
              >
                <Copy className="w-4 h-4" />
                <span className="text-sm hidden md:inline">{t('archives.hideDuplicates')}</span>
              </button>
            )}
            {uniqueTags.length > 0 && (
              <div className="flex items-center gap-2 flex-shrink-0">
                <Tag className="w-4 h-4 text-bambu-gray hidden md:block" />
                <select
                  className="px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                  value={filterTag || ''}
                  onChange={(e) => { setFilterTag(e.target.value || null); setPage(1); }}
                >
                  <option value="">{t('archives.page.allTags')}</option>
                  {uniqueTags.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
                <button
                  onClick={() => setShowTagManagement(true)}
                  className="p-2 rounded-lg bg-bambu-dark border border-bambu-dark-tertiary text-bambu-gray hover:text-white hover:border-bambu-green transition-colors"
                  title={t('archives.manageTags')}
                >
                  <Settings className="w-4 h-4" />
                </button>
              </div>
            )}
            {hasTopFilters && (
              <Button
                variant="ghost"
                size="sm"
                onClick={clearTopFilters}
                className="text-bambu-gray hover:text-white"
              >
                <X className="w-4 h-4" />
                {t('archives.page.reset')}
              </Button>
            )}
            </div>

            {/* Third row: per-page on the left, sort (field + direction) on the right */}
            <div className="flex items-center justify-between gap-2 mt-3">
              <div className="flex items-center gap-1.5">
                <span className="text-xs text-bambu-gray">{t('common.show')}</span>
                <select
                  className="h-9 px-3 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-bambu-gray focus:border-bambu-green focus:outline-none"
                  value={perPage}
                  onChange={(e) => { setPerPage(Number(e.target.value)); setPage(1); }}
                >
                  {[12, 24, 48, 96].map((n) => (
                    <option key={n} value={n}>{n}</option>
                  ))}
                </select>
              </div>
              <div className="flex items-center gap-1">
                <select
                  className="h-9 min-w-[7rem] px-3 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                  value={sortField}
                  onChange={(e) => setSortField(e.target.value as 'date' | 'name' | 'size')}
                >
                  <option value="date">{t('common.date')}</option>
                  <option value="name">{t('common.name')}</option>
                  <option value="size">{t('fileManager.size')}</option>
                </select>
                <button
                  onClick={toggleSortDir}
                  className="h-9 w-9 flex items-center justify-center bg-bambu-dark border border-bambu-dark-tertiary rounded-lg hover:border-bambu-green transition-colors"
                  title={sortDir === 'asc' ? t('fileManager.ascending') : t('fileManager.descending')}
                >
                  {sortDir === 'asc' ? (
                    <ArrowUpNarrowWide className="w-4 h-4 text-bambu-gray" />
                  ) : (
                    <ArrowDownWideNarrow className="w-4 h-4 text-bambu-gray" />
                  )}
                </button>
              </div>
            </div>
          {/* Color Filter */}
          {uniqueColors.length > 0 && (
            <div className="flex items-center gap-3 mt-4 pt-4 border-t border-bambu-dark-tertiary">
              <span className="text-xs text-bambu-gray">{t('archives.page.colors')}</span>
              {filterColors.size > 1 && (
                <button
                  onClick={() => { setColorFilterMode(m => m === 'or' ? 'and' : 'or'); setPage(1); }}
                  className={`px-2 py-0.5 text-xs rounded transition-colors ${
                    colorFilterMode === 'and'
                      ? 'bg-bambu-green text-white'
                      : 'bg-bambu-dark-tertiary text-bambu-gray hover:text-white'
                  }`}
                  title={colorFilterMode === 'or' ? t('archives.page.matchAnyColor') : t('archives.page.matchAllColors')}
                >
                  {colorFilterMode.toUpperCase()}
                </button>
              )}
              <div className="flex items-center gap-1.5 flex-wrap">
                {uniqueColors.map((color) => (
                  <button
                    key={color}
                    onClick={() => toggleColor(color)}
                    className={`w-6 h-6 rounded-full border-2 transition-all ${
                      filterColors.has(color)
                        ? 'border-bambu-green scale-110'
                        : 'border-white/20 hover:border-white/40'
                    }`}
                    style={{ backgroundColor: color }}
                    title={color}
                  />
                ))}
              </div>
              {filterColors.size > 0 && (
                <button
                  onClick={clearColorFilter}
                  className="text-xs text-bambu-gray hover:text-white flex items-center gap-1"
                >
                  <X className="w-3 h-3" />
                  {t('archives.page.clear')}
                </button>
              )}
            </div>
          )}
        </div>
      )}

      {/* Archives */}
      {isLoading ? (
        <div className="text-center py-12 text-bambu-gray">{t('archives.loadingArchives')}</div>
      ) : archives?.length === 0 ? (
        <Card>
          <CardContent className="text-center py-12">
            <p className="text-bambu-gray">
              {search ? t('archives.noArchivesSearch') : t('archives.noArchivesYet')}
            </p>
            <p className="text-sm text-bambu-gray mt-2">
              {t('archives.page.archivesAutoCreated')}
            </p>
          </CardContent>
        </Card>
      ) : viewMode === 'calendar' ? (
        <Card className="p-6">
          <CalendarView
            archives={calendarArchives || []}
            printerMap={printerMap}
          />
        </Card>
      ) : viewMode === 'grid' ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-6">
          {archives?.map((archive) => (
            <ArchiveCard
              key={archive.id}
              archive={archive}
              printerName={archive.printer_id ? printerMap.get(archive.printer_id) || t('archives.page.unknownPrinter') : (archive.sliced_for_model ? t('archives.page.slicedFor', { model: archive.sliced_for_model }) : t('archives.page.noPrinter'))}
              isSelected={selectedIds.has(archive.id)}
              onSelect={toggleSelect}
              selectionMode={selectionMode}
              projects={projects}
              isHighlighted={archive.id === highlightedArchiveId}
              timeFormat={timeFormat}
              dateFormat={dateFormat}
              preferredSlicer={preferredSlicer}
              currency={currency}
              t={t}
              onNavigateToArchive={handleNavigateToArchive}
            />
          ))}
        </div>
      ) : viewMode === 'list' ? (
        <Card>
          {/* One outer grid owns the column tracks; the header and every
              `ArchiveListRow` opt into them via `grid-cols-subgrid`. That's
              what makes the column widths agree across all rows — each row
              measuring its own `auto` columns independently is exactly the
              bug this replaces. `minmax(0, 1fr)` on the name column lets
              row-level `truncate` keep clipping when the print name is long
              (plain `1fr` resolves to `minmax(auto, 1fr)` and refuses to
              shrink below min-content). `divide-y` keeps the row separators
              now that we're not stacking divs anymore. */}
          <div className="grid grid-cols-[auto_minmax(0,1fr)_auto_auto_auto_auto] gap-x-4 divide-y divide-bambu-dark-tertiary">
            {/* List Header — centred per column (rows keep their own
                left/right alignment). */}
            <div className="col-span-full grid grid-cols-subgrid px-4 py-3 text-xs text-bambu-gray font-medium text-center">
              <div></div>
              <div>{t('archives.list.name')}</div>
              <div>{t('archives.list.printer')}</div>
              <div>{t('archives.list.date')}</div>
              <div>{t('archives.list.size')}</div>
              <div>{t('archives.list.actions')}</div>
            </div>
            {/* List Items */}
            {archives?.map((archive) => (
              <ArchiveListRow
                key={archive.id}
                archive={archive}
                printerName={archive.printer_id ? printerMap.get(archive.printer_id) || t('archives.page.unknownPrinter') : (archive.sliced_for_model ? t('archives.page.slicedFor', { model: archive.sliced_for_model }) : t('archives.page.noPrinter'))}
                isSelected={selectedIds.has(archive.id)}
                onSelect={toggleSelect}
                selectionMode={selectionMode}
                projects={projects}
                isHighlighted={archive.id === highlightedArchiveId}
                preferredSlicer={preferredSlicer}
                t={t}
                onNavigateToArchive={handleNavigateToArchive}
              />
            ))}
          </div>
        </Card>
      ) : null}

      {showPurgeModal && (
        <PurgeArchivesModal onClose={() => setShowPurgeModal(false)} />
      )}

      {/* Bulk Delete Confirmation */}
      {showBulkDeleteConfirm && (
        <ConfirmModal
          title={t('archives.modal.deleteArchives')}
          message={t('archives.modal.deleteArchivesConfirm', { count: selectedIds.size })}
          confirmText={t('archives.modal.deleteCount', { count: selectedIds.size })}
          variant="danger"
          onConfirm={() => {
            bulkDeleteMutation.mutate(Array.from(selectedIds));
            setShowBulkDeleteConfirm(false);
          }}
          onCancel={() => setShowBulkDeleteConfirm(false)}
        />
      )}

      {/* Batch Tag Modal */}
      {showBatchTag && (
        <BatchTagModal
          selectedIds={Array.from(selectedIds)}
          existingTags={uniqueTags}
          onClose={() => setShowBatchTag(false)}
        />
      )}

      {/* Batch Project Modal */}
      {showBatchProject && (
        <BatchProjectModal
          selectedIds={Array.from(selectedIds)}
          onClose={() => setShowBatchProject(false)}
        />
      )}

      {/* Compare Archives Modal */}
      {showCompareModal && selectedIds.size >= 2 && selectedIds.size <= 5 && (
        <CompareArchivesModal
          archiveIds={Array.from(selectedIds)}
          onClose={() => {
            setShowCompareModal(false);
            setSelectedIds(new Set());
            setIsSelectionMode(false);
          }}
        />
      )}

      {/* Tag Management Modal */}
      {showTagManagement && (
        <TagManagementModal onClose={() => setShowTagManagement(false)} />
      )}

    </div>
  );
}
