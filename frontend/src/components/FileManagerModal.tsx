import { useState, useEffect, useId, useMemo } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  Check,
  Folder,
  File,
  ChevronLeft,
  Download,
  Trash2,
  Loader2,
  HardDrive,
  RefreshCw,
  Film,
  FileBox,
  FileText,
  Image,
  Library,
  Search,
  ArrowUpDown,
  CheckSquare,
  MinusSquare,
  Box,
  Eraser,
} from 'lucide-react';
import { SelectionBox } from './SelectionBox';
import { DownloadCloseChoice } from './downloads/DownloadCloseChoice';
import { DownloadJobPanel } from './downloads/DownloadJobPanel';
import { useDownloadJob, type DownloadJobApi } from './downloads/useDownloadJob';
import { clickSelection } from '../utils/rangeSelection';
import {
  api,
  withMediaToken,
  type PrinterFileType,
  type PrinterListingStatus,
  type PrinterStorage,
} from '../api/client';
import { parseUTCDate } from '../utils/date';
import { Button } from './Button';
import { ConfirmModal } from './ConfirmModal';
import { Modal } from './Modal';
import { ModelViewer } from './ModelViewer';
import { GcodePreview } from './GcodePreview';
import type { PlateMetadata } from '../types/plates';
import { useToast } from '../contexts/ToastContext';
import { formatFileSize } from '../utils/file';
import { deletePrinterFilesOneByOne } from '../utils/printerFileBulkDelete';
import { FolderTreePicker } from './FolderTreePicker';
import { Select } from './Select';

// Depth-first flatten of the library folder tree for a single <select>.
// Mirror of the helper in VirtualPrinterCard / MakerworldPage — kept inline
// rather than promoted to a shared module since each consumer has subtly
// different filtering needs (read-only externals, project scope, etc.).

const LIBRARY_IMPORT_EXTS = new Set(['3mf', 'gcode']);
function isLibraryImportable(filename: string): boolean {
  const lower = filename.toLowerCase();
  if (lower.endsWith('.gcode.3mf')) return true;
  const ext = lower.split('.').pop() ?? '';
  return LIBRARY_IMPORT_EXTS.has(ext);
}

interface FileManagerModalProps {
  printerId: number;
  printerName: string;
  onClose: () => void;
}

type PrinterViewerTab = '3d' | 'gcode';

interface PrinterFileViewerModalProps {
  printerId: number;
  filePath: string;
  filename: string;
  onClose: () => void;
}

function PrinterFileViewerModal({ printerId, filePath, filename, onClose }: PrinterFileViewerModalProps) {
  const { t } = useTranslation();
  const headingId = useId();
  const [activeTab, setActiveTab] = useState<PrinterViewerTab | null>(null);
  const [plates, setPlates] = useState<PlateMetadata[]>([]);
  const [platesLoading, setPlatesLoading] = useState(false);
  const [selectedPlateId, setSelectedPlateId] = useState<number | null>(null);

  const ext = filename.toLowerCase().split('.').pop() || '';
  const hasModel = ext === '3mf' || ext === 'stl';
  const hasGcode = ext === 'gcode' || ext === '3mf';

  useEffect(() => {
    setActiveTab(hasModel ? '3d' : hasGcode ? 'gcode' : null);
  }, [hasModel, hasGcode]);

  useEffect(() => {
    setPlates([]);
    setSelectedPlateId(null);

    if (!hasModel) return;

    setPlatesLoading(true);
    api.getPrinterFilePlates(printerId, filePath)
      .then((data) => setPlates(data.plates || []))
      .catch(() => setPlates([]))
      .finally(() => setPlatesLoading(false));
  }, [filePath, hasModel, printerId]);

  const hasMultiplePlates = plates.length > 1;
  const selectedPlate = selectedPlateId == null
    ? null
    : plates.find((plate) => plate.index === selectedPlateId) ?? null;

  return (
    <Modal
      onClose={onClose}
      labelledBy={headingId}
      size="4xl"
      panelClassName="h-[80vh]"
      bodyClassName="flex flex-col"
      header={
        <h2 id={headingId} className="text-lg font-semibold text-white truncate flex-1 mr-4">{filename}</h2>
      }
    >
      <div className="flex border-b border-bambu-dark-tertiary">
        <button
          onClick={() => hasModel && setActiveTab('3d')}
          disabled={!hasModel}
          className={`flex items-center gap-2 px-4 py-3 text-sm font-medium transition-colors ${
            activeTab === '3d'
              ? 'text-bambu-green border-b-2 border-bambu-green'
              : hasModel
                ? 'text-bambu-gray hover:text-white'
                : 'text-bambu-gray/30 cursor-not-allowed'
          }`}
        >
          <Box className="w-4 h-4" />
          {t('modelViewer.tabs.model')}
          {!hasModel && <span className="text-xs">{t('modelViewer.tabs.modelUnavailable')}</span>}
        </button>
        <button
          onClick={() => hasGcode && setActiveTab('gcode')}
          disabled={!hasGcode}
          className={`flex items-center gap-2 px-4 py-3 text-sm font-medium transition-colors ${
            activeTab === 'gcode'
              ? 'text-bambu-green border-b-2 border-bambu-green'
              : hasGcode
                ? 'text-bambu-gray hover:text-white'
                : 'text-bambu-gray/30 cursor-not-allowed'
          }`}
        >
          <FileText className="w-4 h-4" />
          {t('modelViewer.tabs.gcode')}
          {!hasGcode && <span className="text-xs">{t('modelViewer.tabs.gcodeUnavailable')}</span>}
        </button>
      </div>

      <div className="flex-1 overflow-hidden p-4">
        {activeTab === '3d' && hasModel ? (
          <div className="w-full h-full flex flex-col gap-3">
            {hasMultiplePlates && (
              <div className="rounded-lg border border-bambu-dark-tertiary bg-bambu-dark p-3">
                <div className="flex items-center gap-2 text-sm text-bambu-gray mb-2">
                  <Box className="w-4 h-4" />
                  {t('modelViewer.plates')}
                  {platesLoading && <Loader2 className="w-3 h-3 animate-spin" />}
                </div>
                <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
                  <button
                    type="button"
                    onClick={() => setSelectedPlateId(null)}
                    className={`flex items-center gap-2 rounded-lg border p-2 text-left transition-colors ${
                      selectedPlateId == null
                        ? 'border-bambu-green bg-bambu-green/10'
                        : 'border-bambu-dark-tertiary bg-bambu-dark-secondary hover:border-bambu-gray'
                    }`}
                  >
                    <div className="w-10 h-10 rounded bg-bambu-dark-tertiary flex items-center justify-center">
                      <Box className="w-5 h-5 text-bambu-gray" />
                    </div>
                    <div className="min-w-0 flex-1">
                      <p className="text-sm text-white font-medium truncate">{t('modelViewer.allPlates')}</p>
                      <p className="text-xs text-bambu-gray truncate">
                        {t('modelViewer.plateCount', { count: plates.length })}
                      </p>
                    </div>
                    {selectedPlateId == null && (
                      <Check className="w-4 h-4 text-bambu-green flex-shrink-0" />
                    )}
                  </button>
                  {plates.map((plate) => (
                    <button
                      key={plate.index}
                      type="button"
                      onClick={() => setSelectedPlateId(plate.index)}
                      className={`flex items-center gap-2 rounded-lg border p-2 text-left transition-colors ${
                        selectedPlateId === plate.index
                          ? 'border-bambu-green bg-bambu-green/10'
                          : 'border-bambu-dark-tertiary bg-bambu-dark-secondary hover:border-bambu-gray'
                      }`}
                    >
                      {/* The plates answer carries the URL itself — an anonymous
                          archive route or an inline data URL. There is no
                          per-plate image endpoint on the printer routes: an
                          <img> cannot send the Authorization header, so the one
                          that used to exist answered 401 for every plate. */}
                      {plate.thumbnail_url ? (
                        <img
                          src={withMediaToken(plate.thumbnail_url)}
                          alt={t('modelViewer.plateNumber', { number: plate.index })}
                          className="w-10 h-10 rounded object-cover bg-bambu-dark-tertiary"
                        />
                      ) : (
                        <div className="w-10 h-10 rounded bg-bambu-dark-tertiary flex items-center justify-center">
                          <Box className="w-5 h-5 text-bambu-gray" />
                        </div>
                      )}
                      <div className="min-w-0 flex-1">
                        <p className="text-sm text-white font-medium truncate">
                          {plate.name || t('modelViewer.plateNumber', { number: plate.index })}
                        </p>
                        <p className="text-xs text-bambu-gray truncate">
                          {plate.objects.length > 0
                            ? plate.objects.slice(0, 2).join(', ') + (plate.objects.length > 2 ? '…' : '')
                            : t('modelViewer.filamentCount', { count: plate.filaments.length })}
                        </p>
                      </div>
                      {selectedPlateId === plate.index && (
                        <Check className="w-4 h-4 text-bambu-green flex-shrink-0" />
                      )}
                    </button>
                  ))}
                </div>
                {selectedPlate && (
                  <div className="mt-3 text-xs text-bambu-gray flex flex-wrap gap-x-4 gap-y-1">
                    <span>{t('modelViewer.plateNumber', { number: selectedPlate.index })}</span>
                    {selectedPlate.print_time_seconds != null && (
                      <span>{t('modelViewer.eta', { minutes: Math.round(selectedPlate.print_time_seconds / 60) })}</span>
                    )}
                    {selectedPlate.filament_used_grams != null && (
                      <span>{selectedPlate.filament_used_grams.toFixed(1)} g</span>
                    )}
                    {selectedPlate.filaments.length > 0 && (
                      <span>{t('modelViewer.filamentCount', { count: selectedPlate.filaments.length })}</span>
                    )}
                  </div>
                )}
              </div>
            )}
            <div className="flex-1">
              <ModelViewer
                url={api.getPrinterFileDownloadUrl(printerId, filePath)}
                fileType={ext}
                selectedPlateId={selectedPlateId}
                className="w-full h-full"
              />
            </div>
          </div>
        ) : activeTab === 'gcode' && hasGcode ? (
          <GcodePreview
            gcodeUrl={api.getPrinterFileGcodeUrl(printerId, filePath)}
            className="w-full h-full"
          />
        ) : (
          <div className="w-full h-full flex items-center justify-center text-bambu-gray">
            {t('modelViewer.noPreview')}
          </div>
        )}
      </div>
    </Modal>
  );
}

function formatStorageSize(bytes: number): string {
  if (bytes === 0) return '0 GB';
  const gb = bytes / (1024 * 1024 * 1024);
  if (gb >= 1) {
    return `${gb.toFixed(1)} GB`;
  }
  const mb = bytes / (1024 * 1024);
  return `${mb.toFixed(0)} MB`;
}

function getFileIcon(filename: string, isDirectory: boolean) {
  if (isDirectory) return Folder;

  const ext = filename.toLowerCase().split('.').pop() || '';
  switch (ext) {
    case '3mf':
      return FileBox;
    case 'gcode':
      return FileText;
    case 'mp4':
    case 'avi':
      return Film;
    case 'png':
    case 'jpg':
    case 'jpeg':
      return Image;
    default:
      return File;
  }
}

type SortOption = 'name-asc' | 'name-desc' | 'size-asc' | 'size-desc' | 'date-asc' | 'date-desc';

const SORT_OPTIONS: { value: SortOption; label: string }[] = [
  { value: 'name-asc', label: 'Name (A-Z)' },
  { value: 'name-desc', label: 'Name (Z-A)' },
  { value: 'size-asc', label: 'Size (smallest)' },
  { value: 'size-desc', label: 'Size (largest)' },
  { value: 'date-asc', label: 'Date (oldest)' },
  { value: 'date-desc', label: 'Date (newest)' },
];

function listingWarningState(isError: boolean, status: PrinterListingStatus | undefined) {
  return isError || !status || status === 'complete' ? 'unavailable' : status;
}

export function FileManagerModal({ printerId, printerName, onClose }: FileManagerModalProps) {
  const { t } = useTranslation();
  const headingId = useId();
  const importHeadingId = useId();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const [currentPath, setCurrentPath] = useState('/');
  const [selectedFiles, setSelectedFiles] = useState<Set<string>>(new Set());
  const [searchQuery, setSearchQuery] = useState('');
  const [filesToDelete, setFilesToDelete] = useState<string[]>([]);
  const [sortBy, setSortBy] = useState<SortOption>('name-asc');
  // The last file clicked without Shift: where a Shift-click's range starts.
  const [anchorPath, setAnchorPath] = useState<string | null>(null);
  const [downloadStarting, setDownloadStarting] = useState(false);
  const [closeChoice, setCloseChoice] = useState(false);
  const downloadApi = useMemo<DownloadJobApi>(
    () => ({
      get: (jobId) => api.getPrinterDownloadJob(printerId, jobId),
      cancel: (jobId) => api.cancelPrinterDownloadJob(printerId, jobId),
      mint: (jobId, acceptPartial) => api.mintPrinterDownloadUrl(printerId, jobId, acceptPartial),
    }),
    [printerId],
  );
  const download = useDownloadJob(downloadApi);
  const adoptDownload = download.adopt;
  // A download started earlier goes on while this dialog is closed: pick it up again.
  useEffect(() => {
    let ignore = false;
    api
      .listPrinterDownloadJobs(printerId)
      .then(({ jobs }) => {
        if (ignore) return;
        const current = [...jobs]
          .reverse()
          .find((job) => job.phase === 'queued' || job.phase === 'preparing' || job.phase === 'cancelling' || job.phase === 'ready');
        if (current) adoptDownload(current);
      })
      .catch(() => {
        // Nothing to pick up, or the server could not be asked: the dialog works without it.
      });
    return () => {
      ignore = true;
    };
  }, [printerId, adoptDownload]);
  const downloadPhase = download.lost ? null : (download.job?.phase ?? null);
  const downloadActive = downloadPhase === 'queued' || downloadPhase === 'preparing' || downloadPhase === 'cancelling';
  const [viewerFile, setViewerFile] = useState<{ path: string; name: string } | null>(null);
  const [importDialogOpen, setImportDialogOpen] = useState(false);
  const [importFolderId, setImportFolderId] = useState<number | null>(null);
  const [showClearConfirm, setShowClearConfirm] = useState(false);
  // null until the printer's capability answers — then the default medium,
  // which is internal storage when no card is inserted. Opening on external and
  // painting an empty grid (what BambuStudio does) reads as a malfunction.
  const [storage, setStorage] = useState<PrinterStorage | null>(null);

  // The capability comes from the printer's status, not from the file listing:
  // a dead tunnel must not be able to remove the switcher that lets the
  // operator get back to the card.
  const { data: printerStatus, isFetched: statusSettled } = useQuery({
    queryKey: ['printerStatus', printerId],
    queryFn: () => api.getPrinterStatus(printerId),
    staleTime: 30000,
    retry: false,
  });
  const capability = printerStatus?.storage_capability;
  const canSwitchStorage = capability?.can_browse_internal ?? false;
  const isInternal = storage === 'internal';
  // ⚠️ A DIFFERENT flag from the one that gates models: BambuStudio reads
  // `fun` bit 28 for timelapses and `fun2` bit 17 for models, and a machine can
  // have one without the other. Reusing can_browse_internal here would offer a
  // catalogue the printer may not keep.
  const [fileType, setFileType] = useState<PrinterFileType>('model');
  const hasInternalTimelapses = printerStatus?.timelapse_capability?.supports_internal ?? false;

  // ⚠️ Settles on failure too. Gating on "we know the capability" would leave
  // the browser permanently empty whenever the status call fails — a hard hang
  // instead of the momentary flicker the gate exists to avoid. An unreachable
  // printer falls back to the card, which is what every printer has.
  useEffect(() => {
    if (storage !== null || !statusSettled) return;
    setStorage(capability?.default_storage ?? 'external');
  }, [capability, statusSettled, storage]);

  // Library folder tree — fetched lazily, only when the import dialog opens.
  // Same shape the VP card / Makerworld page consume.
  const { data: libraryFolders } = useQuery({
    queryKey: ['library-folders'],
    queryFn: () => api.getLibraryFolders(),
    enabled: importDialogOpen,
  });

  const importableSelectedPaths = useMemo(() => {
    return Array.from(selectedFiles).filter((p) => isLibraryImportable(p.split('/').pop() ?? ''));
  }, [selectedFiles]);

  const importMutation = useMutation({
    mutationFn: (params: { paths: string[]; folderId: number | null }) =>
      api.importPrinterFilesToLibrary(printerId, params.paths, params.folderId, storage ?? undefined),
    onSuccess: (result) => {
      const total = result.imported.length + result.skipped.length;
      const importedCount = result.imported.length;
      const existing = result.imported.filter((r) => r.was_existing).length;
      if (existing > 0) {
        showToast(
          t('printerFiles.toast.importDone', {
            imported: importedCount,
            total,
            existing,
          }),
        );
      } else {
        showToast(
          t('printerFiles.toast.importDoneSimple', { imported: importedCount, total }),
        );
      }
      // Refresh library views in case the user opens the file manager next.
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      queryClient.invalidateQueries({ queryKey: ['library-folders'] });
      setImportDialogOpen(false);
      setSelectedFiles(new Set());
    },
    onError: (error: Error) => {
      showToast(t('printerFiles.toast.importFailed', { error: error.message }), 'error');
    },
  });

  const handleOpenImportDialog = () => {
    if (importableSelectedPaths.length === 0) {
      showToast(t('printerFiles.toast.importNothingEligible'), 'error');
      return;
    }
    setImportFolderId(null);
    setImportDialogOpen(true);
  };

  const handleConfirmImport = () => {
    if (importableSelectedPaths.length === 0) return;
    showToast(
      t('printerFiles.toast.importStarted', { count: importableSelectedPaths.length }),
    );
    importMutation.mutate({ paths: importableSelectedPaths, folderId: importFolderId });
  };

  // No auto-poll: every refetch opens a fresh FTPS connection (TLS handshake
  // and all) to the printer, and a 30s interval saturated fragile printer
  // controllers like the P1S — MQTT, FTP and the camera all timed out
  // together while this modal sat open (#1480). A printer's file list only
  // changes on upload / delete (the mutations below invalidate the query)
  // or when a print finishes; the manual Refresh button covers the rest.
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['printerFiles', printerId, storage, fileType, currentPath],
    queryFn: () =>
      api.getPrinterFiles(printerId, currentPath, storage ?? undefined, isInternal ? fileType : undefined),
    // Wait for the capability rather than fetching the wrong medium first and
    // correcting it — that showed the card's files for a moment on a printer
    // that has none.
    enabled: storage !== null,
  });

  // The list as it is shown: filtered, directories first, then the chosen sort. Shift-click
  // ranges, "select all" and the rows below all read this one order.
  const visibleFiles = useMemo(() => {
    const query = searchQuery.toLowerCase();
    const time = (value?: string) => (value ? (parseUTCDate(value)?.getTime() ?? 0) : 0);
    return [...(data?.files ?? [])]
      .filter((file) => !query || file.name.toLowerCase().includes(query))
      .sort((a, b) => {
        // Directories always first
        if (a.is_directory && !b.is_directory) return -1;
        if (!a.is_directory && b.is_directory) return 1;
        switch (sortBy) {
          case 'name-desc':
            return b.name.localeCompare(a.name);
          case 'size-asc':
            return a.size - b.size;
          case 'size-desc':
            return b.size - a.size;
          case 'date-asc':
            return time(a.mtime) - time(b.mtime);
          case 'date-desc':
            return time(b.mtime) - time(a.mtime);
          default:
            return a.name.localeCompare(b.name);
        }
      });
  }, [data, searchQuery, sortBy]);
  const visibleFilePaths = useMemo(
    () => visibleFiles.filter((file) => !file.is_directory).map((file) => file.path),
    [visibleFiles],
  );

  // Only a complete, current listing says what is on the printer. A slow or failed one —
  // or the last good list shown in its place — proves nothing about a file being gone.
  const listingComplete = data !== undefined && (data.status === undefined || data.status === 'complete');
  const listingAuthoritative = !isError && listingComplete && !data.stale;
  const showingLastListing = data?.stale === true || (isError && listingComplete);
  useEffect(() => {
    if (!listingAuthoritative || !data) return;
    const present = new Set(data.files.filter((file) => !file.is_directory).map((file) => file.path));
    setSelectedFiles((prev) => {
      const kept = [...prev].filter((path) => present.has(path));
      return kept.length === prev.size ? prev : new Set(kept);
    });
  }, [data, listingAuthoritative]);
  const selectionStale = selectedFiles.size > 0 && !isLoading && !listingAuthoritative;

  const switchFileType = (next: PrinterFileType) => {
    if (next === fileType) return;
    setFileType(next);
    setSelectedFiles(new Set());
    setSearchQuery('');
  };

  const switchStorage = (next: PrinterStorage) => {
    if (next === storage) return;
    setStorage(next);
    setCurrentPath('/');
    // Back to models: the card's view has no catalogue concept, and coming
    // back to internal on "timelapses" would be a state the operator never set.
    setFileType('model');
    // ⚠️ A selection that outlives the list it was made from is a second source
    // of truth about what the operator picked — the same reason the inventory
    // selection clears on any filter change.
    setSelectedFiles(new Set());
    setSearchQuery('');
  };

  const { data: storageData } = useQuery({
    queryKey: ['printerStorage', printerId],
    queryFn: () => api.getPrinterStorage(printerId),
    staleTime: 30000, // Cache for 30 seconds
  });

  const deleteMutation = useMutation({
    // One request per file, and every file answered on its own: the file a job
    // prints from is refused while the others go (`printerFileBulkDelete`).
    mutationFn: (paths: string[]) =>
      deletePrinterFilesOneByOne(paths, (path) => api.deletePrinterFile(printerId, path, storage ?? undefined)),
    onSuccess: (report) => {
      const total = report.deleted.length + report.kept.length + report.notAttempted.length;
      if (report.stopMessage !== undefined) {
        showToast(
          t('printerFiles.toast.deleteStopped', {
            deleted: report.deleted.length,
            total,
            notAttempted: report.notAttempted.length,
            reason: report.stopMessage,
          }),
          'error',
        );
      } else if (report.kept.length > 0) {
        showToast(
          report.deleted.length === 0 && report.kept.length === 1
            ? t('printerFiles.toast.deleteFailed', { error: report.kept[0].message })
            : t('printerFiles.toast.deletePartial', {
                deleted: report.deleted.length,
                total,
                kept: report.kept.length,
                reason: report.kept[0].message,
              }),
          'error',
        );
      } else {
        showToast(t('printerFiles.toast.filesDeleted', { count: report.deleted.length }));
      }
      queryClient.invalidateQueries({ queryKey: ['printerFiles', printerId] });
      queryClient.invalidateQueries({ queryKey: ['printerStorage', printerId] });
      // What is still on the printer stays selected, so the operator sees which files those are.
      setSelectedFiles(new Set([...report.kept.map((file) => file.path), ...report.notAttempted]));
      setFilesToDelete([]);
    },
    onError: (error: Error) => {
      showToast(t('printerFiles.toast.deleteFailed', { error: error.message }), 'error');
    },
  });

  const clearSdCardMutation = useMutation({
    mutationFn: () => api.clearPrinterSdCard(printerId),
    onSuccess: (result) => {
      if (result.stopped) {
        // A print started, or became expected, during the walk: the rest stayed.
        showToast(t('printerFiles.toast.sdCardClearStopped', { deleted: result.deleted }), 'error');
      } else if (result.failed > 0) {
        showToast(
          t('printerFiles.toast.sdCardClearedPartial', { deleted: result.deleted, failed: result.failed }),
          'error',
        );
      } else {
        showToast(t('printerFiles.toast.sdCardCleared', { count: result.deleted }));
      }
      queryClient.invalidateQueries({ queryKey: ['printerFiles', printerId] });
      queryClient.invalidateQueries({ queryKey: ['printerStorage', printerId] });
      setSelectedFiles(new Set());
      setShowClearConfirm(false);
    },
    onError: (error: Error) => {
      showToast(t('printerFiles.toast.clearFailed', { error: error.message }), 'error');
      setShowClearConfirm(false);
    },
  });

  const navigateToFolder = (path: string) => {
    setCurrentPath(path);
    setSelectedFiles(new Set());
  };

  const navigateUp = () => {
    if (currentPath === '/') return;
    const parts = currentPath.split('/').filter(Boolean);
    parts.pop();
    setCurrentPath(parts.length ? '/' + parts.join('/') : '/');
    setSelectedFiles(new Set());
  };

  // A plain click toggles one file; Shift-click selects everything shown between the last
  // plain click and this one. "Select all" and "Clear" below do the same without a keyboard.
  const toggleFileSelection = (path: string, e: React.MouseEvent) => {
    e.stopPropagation();
    const next = clickSelection(selectedFiles, visibleFilePaths, anchorPath, path, e.shiftKey);
    setSelectedFiles(next.selected);
    setAnchorPath(next.anchor);
  };

  const selectAllFiles = () => {
    setSelectedFiles(new Set(visibleFilePaths));
  };

  const deselectAllFiles = () => {
    setSelectedFiles(new Set());
  };

  // A download is a job on the server: prepared on its disk, then saved by the browser
  // itself. One file or many, nothing is read into this page.
  const handleDownload = async () => {
    if (selectedFiles.size === 0 || downloadStarting || downloadActive) return;
    if (!listingAuthoritative) {
      // The list on screen is not a current answer from the printer: ask again before acting on it.
      showToast(t('printerFiles.selectionStale'), 'error');
      void refetch();
      return;
    }
    const paths = Array.from(selectedFiles);
    const sizes: Record<string, number> = {};
    for (const file of data?.files ?? []) {
      if (selectedFiles.has(file.path)) sizes[file.path] = file.size;
    }
    setDownloadStarting(true);
    try {
      const job = await api.createPrinterDownloadJob(printerId, {
        paths,
        storage: storage ?? undefined,
        sizes,
        as_zip: paths.length > 1,
        filename: paths.length > 1 ? `${printerName}-files.zip` : undefined,
      });
      download.adopt(job, { autoSave: true });
      setSelectedFiles(new Set());
    } catch (error) {
      showToast(
        t('printerFiles.toast.downloadFailed', { error: error instanceof Error ? error.message : String(error) }),
        'error',
      );
    } finally {
      setDownloadStarting(false);
    }
  };

  const dismissDownload = () => {
    const job = download.job;
    if (job && job.phase === 'ready' && !download.lost) {
      // Dropping a prepared result frees its space on the server.
      void api.cancelPrinterDownloadJob(printerId, job.job_id).catch(() => {});
    }
    download.adopt(null);
  };

  // Closing the dialog is not a hidden cancel: with a download in progress the operator chooses.
  const requestClose = () => {
    if (downloadActive) setCloseChoice(true);
    else onClose();
  };

  const handleDelete = () => {
    if (selectedFiles.size === 0) return;
    setFilesToDelete(Array.from(selectedFiles));
  };

  // Quick navigation buttons for common directories. ⚠️ Empty on internal
  // storage: the tunnel serves one flat catalogue of models, so there are no
  // directories to jump between and inventing some would be a lie the first
  // error message exposes.
  const quickDirs = isInternal
    ? []
    : [
        { path: '/', label: 'Root' },
        { path: '/cache', label: 'Cache' },
        { path: '/model', label: 'Models' },
        { path: '/timelapse', label: 'Timelapse' },
      ];

  return (
    <>
      <Modal
        onClose={requestClose}
        labelledBy={headingId}
        size="3xl"
        bodyClassName="flex flex-col"
        header={
          <div className="flex flex-1 items-center justify-between min-w-0">
            <div className="flex items-center gap-3">
              <HardDrive className="w-5 h-5 text-bambu-green" />
              <div>
                <h2 id={headingId} className="text-lg font-semibold text-white">{t('printerFiles.title')}</h2>
                <p className="text-sm text-bambu-gray">{printerName}</p>
              </div>
            </div>
            <div className="flex items-center gap-4">
              {/* Storage switcher — only where the printer reports two media.
                  ⚠️ Driven by fun2 bit 17, which is a different question from
                  "may this printer print without a card" (bit 0). */}
              {canSwitchStorage && (
                <div className="flex items-center gap-1 bg-bambu-dark rounded-lg p-1" role="tablist">
                  {(['external', 'internal'] as PrinterStorage[]).map((option) => (
                    <button
                      key={option}
                      role="tab"
                      aria-selected={storage === option}
                      onClick={() => switchStorage(option)}
                      className={`px-3 py-1 text-sm rounded-md transition-colors ${
                        storage === option
                          ? 'bg-bambu-green text-white'
                          : 'text-bambu-gray hover:text-white'
                      }`}
                    >
                      {option === 'external'
                        ? t('printerFiles.storageExternal')
                        : t('printerFiles.storageInternal')}
                    </button>
                  ))}
                </div>
              )}
              {/* Storage info. ⚠️ External only — the tunnel reports no free
                  space at all, and Studio never asks it for any. */}
              {!isInternal && storageData && (storageData.used_bytes != null || storageData.free_bytes != null) && (
                <div className="text-sm text-bambu-gray flex items-center gap-2">
                  {storageData.used_bytes != null && (
                    <span>{t('printerFiles.storageUsed')} {formatStorageSize(storageData.used_bytes)}</span>
                  )}
                  {storageData.used_bytes != null && storageData.free_bytes != null && (
                    <span className="text-bambu-dark-tertiary">|</span>
                  )}
                  {storageData.free_bytes != null && (
                    <span>{t('printerFiles.storageFree')} {formatStorageSize(storageData.free_bytes)}</span>
                  )}
                </div>
              )}
            </div>
          </div>
        }
      >
        {/* Quick Navigation */}
        <div className="flex items-center gap-2 p-3 border-b border-bambu-dark-tertiary bg-bambu-dark/50 flex-shrink-0">
          {quickDirs.map((dir) => (
            <button
              key={dir.path}
              onClick={() => {
                navigateToFolder(dir.path);
                setSearchQuery('');
              }}
              className={`px-3 py-1 text-sm rounded-full transition-colors ${
                currentPath === dir.path
                  ? 'bg-bambu-green text-white'
                  : 'bg-bambu-dark-tertiary text-bambu-gray hover:text-white'
              }`}
            >
              {dir.label}
            </button>
          ))}
          {/* On internal storage the folder shortcuts are replaced by the two
              catalogues the tunnel actually serves. ⚠️ The timelapse one is
              offered only when the printer keeps timelapses internally — a
              different flag from the one that gave us this tab at all. */}
          {isInternal && hasInternalTimelapses &&
            (['model', 'timelapse'] as PrinterFileType[]).map((option) => (
              <button
                key={option}
                role="tab"
                aria-selected={fileType === option}
                onClick={() => switchFileType(option)}
                className={`px-3 py-1 text-sm rounded-full transition-colors ${
                  fileType === option
                    ? 'bg-bambu-green text-white'
                    : 'bg-bambu-dark-tertiary text-bambu-gray hover:text-white'
                }`}
              >
                {option === 'model'
                  ? t('printerFiles.catalogueModels')
                  : t('printerFiles.catalogueTimelapses')}
              </button>
            ))}
          <div className="flex-1" />
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-bambu-gray" />
            <input
              type="text"
              placeholder={t('printerFiles.filterPlaceholder')}
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-40 pl-8 pr-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:outline-none"
            />
          </div>
          <div className="relative flex items-center gap-1">
            <ArrowUpDown className="w-4 h-4 text-bambu-gray" />
            <Select
              size="sm"
              value={sortBy}
              onChange={(e) => setSortBy(e.target.value as SortOption)}
              title="Sort files"
              aria-label="Sort files"
            >
              {SORT_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </Select>
          </div>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => refetch()}
            disabled={isLoading}
            title={t('printerFiles.refresh')}
            aria-label={t('printerFiles.refresh')}
          >
            <RefreshCw className={`w-4 h-4 ${isLoading ? 'animate-spin' : ''}`} />
          </Button>
        </div>

        {/* Path breadcrumb. ⚠️ Absent on internal storage: there is no tree to
            be anywhere in, so a path and an "up" button would both be fiction. */}
        {!isInternal && (
          <div className="flex items-center gap-2 px-4 py-2 bg-bambu-dark text-sm flex-shrink-0">
            <button
              onClick={navigateUp}
              disabled={currentPath === '/'}
              className="p-1 rounded hover:bg-bambu-dark-tertiary disabled:opacity-50 disabled:cursor-not-allowed"
              title="Go to parent folder"
              aria-label="Go to parent folder"
            >
              <ChevronLeft className="w-4 h-4" />
            </button>
            <span className="text-bambu-gray font-mono">{currentPath}</span>
          </div>
        )}

        {/* File list */}
        <div className="flex-1 overflow-y-auto p-2 min-h-0">
            {/* Still spinning while the storage is being resolved: the files
                query is disabled until then, so `isLoading` alone would leave a
                blank panel where the operator expects something to happen. */}
            {isLoading || storage === null ? (
              <div className="flex items-center justify-center py-12">
                <Loader2 className="w-8 h-8 text-bambu-green animate-spin" />
              </div>
            ) : (
              <div className="space-y-1">
                {!listingAuthoritative && (
                  <div
                    className="mb-2 rounded-md border border-yellow-500/40 bg-yellow-500/10 px-3 py-2 text-xs text-yellow-200"
                    role="status"
                    data-testid="printer-files-listing-state"
                  >
                    {t(`printerFiles.listing.${listingWarningState(isError, data?.status)}`)}
                    {showingLastListing && <> {t('printerFiles.listing.stale')}</>}
                    {!showingLastListing && !isError && data?.status === 'slow' && (
                      <p className="mt-2">{t('printerFiles.listing.channelBusy')}</p>
                    )}
                  </div>
                )}
                {listingAuthoritative && !data.files.length && (
                  <div className="text-center py-12 text-bambu-gray">{t('printerFiles.listing.empty')}</div>
                )}
                {/* Filter and sort: directories first, then files with selected sort */}
                {visibleFiles.map((file) => {
                    const FileIcon = getFileIcon(file.name, file.is_directory);
                    const isSelected = selectedFiles.has(file.path);

                    return (
                      <div
                        key={file.path}
                        className={`flex items-center gap-3 p-2 rounded-lg cursor-pointer transition-colors ${
                          isSelected
                            ? 'bg-bambu-green/20 border border-bambu-green/50'
                            : 'hover:bg-bambu-dark-tertiary'
                        }`}
                        onClick={() => {
                          if (file.is_directory) {
                            navigateToFolder(file.path);
                          }
                        }}
                      >
                        {/* Checkbox for files only */}
                        {!file.is_directory ? (
                          <button
                            onClick={(e) => toggleFileSelection(file.path, e)}
                            className="flex-shrink-0 text-bambu-gray hover:text-white"
                          >
                            {isSelected ? (
                              <SelectionBox checked={true} className="w-5 h-5" />
                            ) : (
                              <SelectionBox checked={false} className="w-5 h-5" />
                            )}
                          </button>
                        ) : null}
                        <FileIcon
                          className={`w-5 h-5 flex-shrink-0 ${
                            file.is_directory ? 'text-bambu-green' : 'text-bambu-gray'
                          }`}
                        />
                        <span className="flex-1 text-white truncate">{file.name}</span>
                        {!file.is_directory && (
                          <div className="flex items-center gap-3">
                            <span className="text-sm text-bambu-gray">
                              {formatFileSize(file.size)}
                            </span>
                            {(file.name.toLowerCase().endsWith('.3mf') || file.name.toLowerCase().endsWith('.gcode') || file.name.toLowerCase().endsWith('.stl')) && (
                              <button
                                onClick={(e) => {
                                  e.stopPropagation();
                                  setViewerFile({ path: file.path, name: file.name });
                                }}
                                className="p-1 rounded hover:bg-bambu-dark text-bambu-gray hover:text-bambu-green"
                                title={t('fileManagerModal.threeView')}
                              >
                                <Box className="w-4 h-4" />
                              </button>
                            )}
                          </div>
                        )}
                        {file.is_directory && (
                          <ChevronLeft className="w-4 h-4 text-bambu-gray rotate-180" />
                        )}
                      </div>
                    );
                  })}
              </div>
            )}
          </div>

        {download.job && (
          <DownloadJobPanel
            job={download.job}
            lost={download.lost}
            saving={download.saving}
            saveError={download.saveError}
            onCancel={() => void download.cancel()}
            onSave={(acceptPartial) => void download.save(acceptPartial)}
            onDismiss={dismissDownload}
          />
        )}

        {/* Action bar */}
        <div className="flex items-center justify-between p-4 border-t border-bambu-dark-tertiary bg-bambu-dark/50 flex-shrink-0">
          <div className="flex items-center gap-4">
            {(selectedFiles.size > 0 || listingAuthoritative || showingLastListing) && (
              <div className="text-sm text-bambu-gray">
                {selectedFiles.size > 0
                  ? `${selectedFiles.size} selected${selectionStale ? ` · ${t('printerFiles.selectionUnconfirmed')}` : ''}`
                  : searchQuery
                    ? `${visibleFiles.length} of ${data?.files.length ?? 0} items`
                    : `${data?.files.length ?? 0} items`
                }
              </div>
            )}
            {/* Select All / Deselect All */}
            {data?.files?.some(f => !f.is_directory) && (
              <div className="flex items-center gap-2">
                {selectedFiles.size > 0 ? (
                  <button
                    onClick={deselectAllFiles}
                    className="flex items-center gap-1 text-xs text-bambu-gray hover:text-white transition-colors"
                  >
                    <MinusSquare className="w-4 h-4" />
                    Deselect All
                  </button>
                ) : (
                  <button
                    onClick={selectAllFiles}
                    className="flex items-center gap-1 text-xs text-bambu-gray hover:text-white transition-colors"
                  >
                    <CheckSquare className="w-4 h-4" />
                    Select All
                  </button>
                )}
              </div>
            )}
            {/* ⚠️ Clearing is an operation on the removable medium — it wipes
                the card. There is no counterpart on internal storage, and
                offering one here would promise something no transport does. */}
            {!isInternal && (
              <button
                onClick={() => setShowClearConfirm(true)}
                disabled={clearSdCardMutation.isPending}
                className="flex items-center gap-1 text-xs text-red-700 dark:text-red-400 hover:text-red-800 dark:hover:text-red-300 transition-colors disabled:opacity-50"
                title={t('printerFiles.clearSdCardHint')}
              >
                {clearSdCardMutation.isPending ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <Eraser className="w-4 h-4" />
                )}
                {t('printerFiles.clearSdCard')}
              </button>
            )}
          </div>
          <div className="flex gap-2">
            <Button
              variant="secondary"
              disabled={selectedFiles.size === 0 || downloadStarting || downloadActive}
              onClick={handleDownload}
            >
              {downloadStarting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
              {t('printerFiles.download')}
              {selectedFiles.size > 1 ? ` (${selectedFiles.size})` : ''}
            </Button>
            <Button
              variant="secondary"
              disabled={importableSelectedPaths.length === 0 || importMutation.isPending}
              onClick={handleOpenImportDialog}
              title={t('printerFiles.importToLibrary')}
            >
              {importMutation.isPending ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Library className="w-4 h-4" />
              )}
              {t('printerFiles.importToLibrary')}
              {importableSelectedPaths.length > 1 ? ` (${importableSelectedPaths.length})` : ''}
            </Button>
            <Button
              variant="secondary"
              disabled={selectedFiles.size === 0 || deleteMutation.isPending}
              onClick={handleDelete}
              className="text-red-700 hover:text-red-800 dark:text-red-400 dark:hover:text-red-300"
            >
              {deleteMutation.isPending ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Trash2 className="w-4 h-4" />
              )}
              {t('printerFiles.deleteButton')}{selectedFiles.size > 1 ? ` (${selectedFiles.size})` : ''}
            </Button>
          </div>
        </div>
      </Modal>

      {closeChoice && (
        <DownloadCloseChoice
          onStay={() => setCloseChoice(false)}
          onKeep={onClose}
          onCancelDownload={() => {
            void download.cancel().finally(onClose);
          }}
        />
      )}

      {/* Delete Confirmation Modal */}
      {filesToDelete.length > 0 && (
        <ConfirmModal
          title={filesToDelete.length > 1 ? t('printerFiles.deleteFiles', { count: filesToDelete.length }) : t('fileManager.deleteFile')}
          message={
            filesToDelete.length > 1
              ? t('printerFiles.deleteFilesConfirm', { count: filesToDelete.length })
              : t('printerFiles.deleteFileConfirm', { name: filesToDelete[0].split('/').pop() })
          }
          confirmText={t('common.delete')}
          variant="danger"
          onConfirm={() => {
            deleteMutation.mutate(filesToDelete);
          }}
          onCancel={() => setFilesToDelete([])}
        />
      )}

      {showClearConfirm && (
        <ConfirmModal
          title={t('printerFiles.clearSdCardTitle')}
          message={t('printerFiles.clearSdCardConfirm')}
          confirmText={t('printerFiles.clearSdCard')}
          variant="danger"
          isLoading={clearSdCardMutation.isPending}
          loadingText={t('printerFiles.toast.clearing')}
          onConfirm={() => clearSdCardMutation.mutate()}
          onCancel={() => setShowClearConfirm(false)}
        />
      )}

      {viewerFile && (
        <PrinterFileViewerModal
          printerId={printerId}
          filePath={viewerFile.path}
          filename={viewerFile.name}
          onClose={() => setViewerFile(null)}
        />
      )}

      {importDialogOpen && (
        <Modal
          onClose={() => setImportDialogOpen(false)}
          closeDisabled={importMutation.isPending}
          labelledBy={importHeadingId}
          size="md"
          header={
            <>
              <Library className="w-5 h-5 text-bambu-green" />
              <div className="min-w-0 flex-1">
                <h3 id={importHeadingId} className="text-base font-semibold text-white">
                  {t('printerFiles.importDialog.title')}
                </h3>
                <p className="text-xs text-bambu-gray truncate">
                  {t('printerFiles.importDialog.subtitle', {
                    count: importableSelectedPaths.length,
                    printer: printerName,
                  })}
                </p>
              </div>
            </>
          }
        >
          <div className="p-4 space-y-3">
            <div>
              <div className="text-white text-sm font-medium block mb-1">
                {t('printerFiles.importDialog.folderLabel')}
              </div>
              {/* The same picker the file manager's Move dialog uses — a
                  dropdown collapsed the nesting into em-dashes, which is
                  most of what you are choosing between. */}
              <div
                className={`bg-bambu-dark border border-bambu-dark-tertiary rounded-md p-2 ${
                  importMutation.isPending ? 'opacity-50 pointer-events-none' : ''
                }`}
              >
                <FolderTreePicker
                  folders={libraryFolders}
                  value={importFolderId}
                  onChange={setImportFolderId}
                  rootLabel={t('printerFiles.importDialog.rootFolder')}
                  className="max-h-52"
                />
              </div>
            </div>
            <p className="text-xs text-bambu-gray">{t('printerFiles.importDialog.hint')}</p>
          </div>
          <div className="flex justify-end gap-2 px-4 py-3 border-t border-bambu-dark-tertiary bg-bambu-dark/50">
            <Button
              variant="secondary"
              onClick={() => setImportDialogOpen(false)}
              disabled={importMutation.isPending}
            >
              {t('printerFiles.importDialog.cancel')}
            </Button>
            <Button
              variant="primary"
              onClick={handleConfirmImport}
              disabled={importMutation.isPending}
            >
              {importMutation.isPending ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Library className="w-4 h-4" />
              )}
              {t('printerFiles.importDialog.confirm')}
            </Button>
          </div>
        </Modal>
      )}
    </>
  );
}
