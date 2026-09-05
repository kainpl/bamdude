import { useState, useEffect, useRef } from 'react';
import { useMutation, useQueryClient, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { X, Save, Tag, Camera, Trash2, Loader2, Plus, FolderKanban, Hash, Link, PackagePlus, PackageX } from 'lucide-react';
import { api } from '../api/client';
import type { Archive } from '../api/client';
import { Button } from './Button';
import { useAuth } from '../contexts/AuthContext';
import { useToast } from '../contexts/ToastContext';
import { OrderPicker } from './pickers/OrderPicker';
import { OrderLinePicker } from './pickers/OrderLinePicker';
import { invalidateOrderViews } from '../utils/queryInvalidation';

// Keys for failure reasons - translated at render time
const FAILURE_REASON_KEYS = [
  'adhesionFailure',
  'spaghettiDetached',
  'layerShift',
  'cloggedNozzle',
  'filamentRunout',
  'warping',
  'stringing',
  'underExtrusion',
  'powerFailure',
  'swapModeFailure',
  'printerError',
  'userCancelled',
  'other',
] as const;

// Keys for archive statuses - translated at render time
const ARCHIVE_STATUS_KEYS = ['completed', 'failed', 'aborted', 'printing'] as const;

interface EditArchiveModalProps {
  archive: Archive;
  onClose: () => void;
  existingTags?: string[];
}

export function EditArchiveModal({ archive, onClose, existingTags = [] }: EditArchiveModalProps) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const { showToast } = useToast();

  // Close on Escape key
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);
  const queryClient = useQueryClient();
  const [printName, setPrintName] = useState(archive.print_name || '');
  const [printerId, setPrinterId] = useState<number | null>(archive.printer_id);
  const [projectId, setProjectId] = useState<number | null>(archive.project_id ?? null);
  // Pass 2: which LINE of the order the print counts against. ⚠️ Reset whenever
  // the order changes — the server rejects (400) a line belonging to another
  // order, so the mismatch must never be submittable from here.
  const [projectLineId, setProjectLineId] = useState<number | null>(archive.project_line_id ?? null);
  const [notes, setNotes] = useState(archive.notes || '');
  const [tags, setTags] = useState(archive.tags || '');
  // Failure reason is stored as a camelCase key (`filamentRunout`), but earlier
  // versions of this modal saved the translated label as the value. Reverse-
  // lookup any legacy translated text against the current locale so the
  // dropdown pre-selects the right option, then any save converts it forward.
  const [failureReason, setFailureReason] = useState(() => {
    const raw = archive.failure_reason || '';
    if (!raw) return '';
    if ((FAILURE_REASON_KEYS as readonly string[]).includes(raw)) return raw;
    const match = FAILURE_REASON_KEYS.find(
      (k) => t(`editArchive.failureReasons.${k}`) === raw,
    );
    return match || '';
  });
  const [errorMessage, setErrorMessage] = useState(archive.error_message || '');
  const [status, setStatus] = useState(archive.status);
  const [quantity, setQuantity] = useState(archive.quantity ?? 1);
  const [defectiveCount, setDefectiveCount] = useState(archive.defective_count ?? 0);
  // Per-part defective counts, keyed by part id. Only meaningful when
  // archive.parts is non-empty — see the render block below.
  const [partsDefective, setPartsDefective] = useState<Record<number, number>>(
    Object.fromEntries((archive.parts ?? []).map((p) => [p.id, p.defective])),
  );
  // Whether the user has touched any per-part stepper this session. A
  // backfilled multi-part archive can legitimately hold a flat, unattributed
  // legacy defective_count with every part row at defective=0 (the backfill's
  // mono-plate rule). Sending parts_defective + its sum unconditionally would
  // silently zero that real historical count on a save that only touched,
  // say, notes — so parts/defective_count are only included when dirty.
  const [partsDirty, setPartsDirty] = useState(false);
  // ⚠️ The archive prop usually arrives from the LIST endpoint, which returns
  // parts: [] by design (no per-row query on list screens) — the per-part rows
  // live only on the DETAIL response. Fetch it when the prop carries none;
  // without this the steppers below are unreachable from the Archives page.
  const { data: archiveDetail } = useQuery({
    queryKey: ['archive-detail', archive.id],
    queryFn: () => api.getArchive(archive.id),
    enabled: (archive.parts?.length ?? 0) === 0,
  });
  const parts = (archive.parts?.length ? archive.parts : archiveDetail?.parts) ?? [];
  const hasParts = parts.length > 0;
  // Late-arriving detail: seed the stepper state once the rows land — unless
  // the user already touched a stepper (their keystrokes win over a refetch).
  useEffect(() => {
    if (!partsDirty && parts.length) {
      setPartsDefective(Object.fromEntries(parts.map((p) => [p.id, p.defective])));
    }
    // `parts` derives from archive.parts / archiveDetail, both stable per fetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [archive.parts, archiveDetail, partsDirty]);
  const partsDefectiveSum = Object.values(partsDefective).reduce((sum, n) => sum + n, 0);
  const [photos, setPhotos] = useState<string[]>(archive.photos || []);
  const [externalUrl, setExternalUrl] = useState(archive.external_url || '');
  const [uploadingPhoto, setUploadingPhoto] = useState(false);
  const [showTagSuggestions, setShowTagSuggestions] = useState(false);
  const tagInputRef = useRef<HTMLInputElement>(null);
  const photoInputRef = useRef<HTMLInputElement>(null);
  const blurTimeoutRef = useRef<number | null>(null);

  const { data: printers } = useQuery({
    queryKey: ['printers'],
    queryFn: api.getPrinters,
  });

  // Fetch all tags using the dedicated API
  const { data: tagsData } = useQuery({
    queryKey: ['tags'],
    queryFn: api.getTags,
    enabled: existingTags.length === 0,
  });

  // Use existing tags prop if provided, otherwise use fetched tags
  const allTags = existingTags.length > 0
    ? existingTags
    : (tagsData?.map(t => t.name) || []);

  // Get current tags as array
  const currentTags = tags.split(',').map(t => t.trim()).filter(Boolean);

  // Get the text being typed after the last comma (for autocomplete filtering)
  const currentInput = tags.includes(',')
    ? tags.substring(tags.lastIndexOf(',') + 1).trim().toLowerCase()
    : tags.trim().toLowerCase();

  // Filter suggestions: not already added AND matches current input (if any)
  const tagSuggestions = allTags.filter(t =>
    !currentTags.includes(t) &&
    (currentInput === '' || t.toLowerCase().includes(currentInput))
  );

  // Add a tag (replaces any partial input with the selected tag)
  const addTag = (tag: string) => {
    // If there's partial input being typed, replace it with the selected tag
    // Otherwise, just append the tag
    let baseTags: string[];
    if (currentInput && !allTags.includes(currentInput)) {
      // User is typing a partial tag - replace it with the selected one
      baseTags = tags.includes(',')
        ? tags.substring(0, tags.lastIndexOf(',')).split(',').map(t => t.trim()).filter(Boolean)
        : [];
    } else {
      // No partial input or input is already a complete tag - append
      baseTags = currentTags;
    }

    if (!baseTags.includes(tag)) {
      const newTags = [...baseTags, tag].join(', ');
      setTags(newTags);
    }
    // Clear any pending blur timeout to prevent hiding suggestions
    if (blurTimeoutRef.current !== null) {
      clearTimeout(blurTimeoutRef.current);
    }
    tagInputRef.current?.focus();
  };

  // Remove a tag
  const removeTag = (tagToRemove: string) => {
    const newTags = currentTags.filter(t => t !== tagToRemove).join(', ');
    setTags(newTags);
  };

  const updateMutation = useMutation({
    mutationFn: (data: Parameters<typeof api.updateArchive>[1]) =>
      api.updateArchive(archive.id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      // Re-filing a print moves it between orders and between customers, so
      // both pages the edit could have touched are stale — hence prefixes,
      // decided once in `utils/queryInvalidation.ts`.
      invalidateOrderViews(queryClient);
      onClose();
    },
  });

  /**
   * "Count this print into the product's free stock" (pass 8, Decision 3).
   *
   * New order-less prints are credited automatically by the completion handler;
   * HISTORY deliberately is not, because nobody knows which of last year's
   * order-less prints were shipped, scrapped or are still in a drawer. This
   * button is the operator vouching for one of them, and it is the only way a
   * pre-pass-8 print reaches a shelf.
   *
   * ⚠️ **An empty list is a legitimate answer, not a failure** — the print may
   * have finished nothing good, or its plate may belong to no product. Saying
   * so plainly is better than a green toast listing nothing.
   */
  const countIntoStock = useMutation({
    mutationFn: () => api.countArchiveIntoStock(archive.id),
    onSuccess: (moved) => {
      // The archive rows this modal reads, plus every product view that shows
      // `kits_available` — the shelf just moved for a product this modal never
      // names.
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      queryClient.invalidateQueries({ queryKey: ['archive-detail', archive.id] });
      queryClient.invalidateQueries({ queryKey: ['product-stock'] });
      queryClient.invalidateQueries({ queryKey: ['product'] });
      queryClient.invalidateQueries({ queryKey: ['products'] });
      if (moved.length === 0) {
        showToast(t('stock.archive.nothing'), 'info');
        return;
      }
      showToast(t('stock.archive.done', { moved: moved.map((m) => `${m.delta} ${m.name}`).join(', ') }));
    },
    // 409 — the print is filed under an order, or its parts are already on the
    // shelf. The server's own sentence is what `ApiError.message` carries.
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const handlePhotoUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    setUploadingPhoto(true);
    try {
      const result = await api.uploadArchivePhoto(archive.id, file);
      setPhotos(result.photos);
      queryClient.invalidateQueries({ queryKey: ['archives'] });
    } catch (error) {
      console.error('Failed to upload photo:', error);
    } finally {
      setUploadingPhoto(false);
      if (photoInputRef.current) {
        photoInputRef.current.value = '';
      }
    }
  };

  const handlePhotoDelete = async (filename: string) => {
    try {
      const result = await api.deleteArchivePhoto(archive.id, filename);
      setPhotos(result.photos || []);
      queryClient.invalidateQueries({ queryKey: ['archives'] });
    } catch (error) {
      console.error('Failed to delete photo:', error);
    }
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    // Build update data
    const updateData: Parameters<typeof api.updateArchive>[1] = {
      print_name: printName || undefined,
      printer_id: printerId,
      project_id: projectId,
      project_line_id: projectLineId,
      notes: notes || undefined,
      tags: tags || undefined,
      quantity: quantity,
      external_url: externalUrl || null,
    };

    if (hasParts) {
      // Only send defect data when a stepper was actually touched — see the
      // partsDirty declaration above for why an untouched save must omit
      // both fields rather than resend the (unattributed) zeroed sum.
      if (partsDirty) {
        updateData.parts_defective = Object.entries(partsDefective).map(([id, defective]) => ({
          id: Number(id),
          defective,
        }));
        updateData.defective_count = partsDefectiveSum;
      }
    } else {
      updateData.defective_count = defectiveCount;
    }

    // Only include status if changed
    if (status !== archive.status) {
      updateData.status = status;
    }

    // Handle failure_reason + error_message based on status
    if (status === 'failed' || status === 'aborted') {
      updateData.failure_reason = failureReason || undefined;
      updateData.error_message = errorMessage || null;
    } else if (archive.status === 'failed' || archive.status === 'aborted') {
      // Clear failure_reason + error_message when leaving failed/aborted
      updateData.failure_reason = null;
      updateData.error_message = null;
    }

    updateMutation.mutate(updateData);
  };

  return (
    <div
      className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-50 p-4"
      onClick={onClose}
    >
      <div
        className="bg-bambu-dark-secondary rounded-xl border border-bambu-dark-tertiary w-full max-w-md max-h-[90vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-4 border-b border-bambu-dark-tertiary">
          <h2 className="text-lg font-semibold text-white">{t('editArchive.title')}</h2>
          <button
            onClick={onClose}
            className="text-bambu-gray hover:text-white transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} className="p-4 space-y-4 overflow-y-auto flex-1">
          {/* Print Name */}
          <div>
            <label className="block text-sm text-bambu-gray mb-1">{t('editArchive.name')}</label>
            <input
              type="text"
              value={printName}
              onChange={(e) => setPrintName(e.target.value)}
              className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
              placeholder={t('editArchive.namePlaceholder')}
            />
          </div>

          {/* Printer */}
          <div>
            <label className="block text-sm text-bambu-gray mb-1">{t('editArchive.printer')}</label>
            <select
              value={printerId ?? ''}
              onChange={(e) => setPrinterId(e.target.value ? Number(e.target.value) : null)}
              className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
            >
              <option value="">{t('editArchive.noPrinter')}</option>
              {printers?.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </div>

          {/* Order */}
          <div>
            <label htmlFor="edit-archive-order" className="block text-sm text-bambu-gray mb-1">
              <FolderKanban className="w-4 h-4 inline mr-1" />
              {t('editArchive.order')}
            </label>
            <OrderPicker
              id="edit-archive-order"
              value={projectId}
              onChange={(next) => {
                setProjectId(next);
                setProjectLineId(null);
              }}
            />
          </div>

          {/* Order line */}
          <div>
            <label htmlFor="edit-archive-line" className="block text-sm text-bambu-gray mb-1">
              <FolderKanban className="w-4 h-4 inline mr-1" />
              {t('editArchive.line')}
            </label>
            <OrderLinePicker
              id="edit-archive-line"
              orderId={projectId}
              value={projectLineId}
              onChange={setProjectLineId}
            />
          </div>

          {/* Count this print into free stock — for a COMPLETED, ORDER-LESS
              print only.
              ⚠️ Both the saved value and the draft are asked. The endpoint
              judges what is stored (it 409s a print filed under an order), so
              `archive.project_id` is the real gate; `projectId` is added
              because an operator who has just picked an order in the box above
              is about to file this print there, and offering to shelve it in
              the same breath is offering two contradictory things.
              ⚠️ And `status` (finding I5): only a finished print put anything
              on a shelf — `credit_unfiled_print` refuses every other status —
              so on a failed or cancelled one this button can do nothing but
              answer "this print counted nothing into stock", which reads as a
              bug in the button rather than as the rule it is. */}
          {hasPermission('projects:update') &&
            archive.status === 'completed' &&
            archive.project_id == null &&
            projectId == null && (
              <div className="rounded-lg border border-bambu-dark-tertiary p-3 space-y-2">
                <p className="text-xs text-bambu-gray">{t('stock.archive.hint')}</p>
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  data-testid="archive-count-into-stock"
                  onClick={() => countIntoStock.mutate()}
                  disabled={countIntoStock.isPending}
                >
                  <PackagePlus className="w-4 h-4" />
                  {t('stock.archive.count')}
                </Button>
              </div>
            )}

          {/* Quantity - number of items printed */}
          <div>
            <label className="block text-sm text-bambu-gray mb-1">
              <Hash className="w-4 h-4 inline mr-1" />
              {t('editArchive.itemsPrinted')}
            </label>
            <input
              type="number"
              min={1}
              value={quantity}
              onChange={(e) => setQuantity(Math.max(1, parseInt(e.target.value) || 1))}
              className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
              placeholder="1"
            />
            <p className="text-xs text-bambu-gray mt-1">
              {t('editArchive.itemsPrintedHelp')}
            </p>
          </div>

          {/* Defective parts — scrap out of the plate above. When the archive
              has parts-ledger rows, scrap is entered per part (each capped at
              that part's own quantity) instead of as one flat total. */}
          {hasParts ? (
            <div>
              <label className="block text-sm text-bambu-gray mb-1">
                <PackageX className="w-4 h-4 inline mr-1" />
                {t('editArchive.partsDefectiveTitle')}
              </label>
              <div className="space-y-2">
                {parts.map((part) => (
                  <div key={part.id} className="flex items-center justify-between gap-3">
                    <span className="text-sm text-white truncate flex-1">{part.name}</span>
                    <span className="text-xs text-bambu-gray whitespace-nowrap">&times; {part.quantity}</span>
                    <input
                      type="number"
                      min={0}
                      max={part.quantity}
                      value={partsDefective[part.id] ?? 0}
                      onChange={(e) => {
                        const raw = parseInt(e.target.value) || 0;
                        const clamped = Math.min(part.quantity, Math.max(0, raw));
                        setPartsDefective((prev) => ({ ...prev, [part.id]: clamped }));
                        setPartsDirty(true);
                      }}
                      data-testid={`part-defective-${part.id}`}
                      className="w-20 px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                    />
                  </div>
                ))}
              </div>
              <p className="text-sm text-white mt-2" data-testid="parts-defective-total">
                {t('editArchive.partsDefectiveTotal')}: {partsDefectiveSum}
              </p>
              <p className="text-xs text-bambu-gray mt-1">
                {t('editArchive.partsDefectiveHelp')}
              </p>
            </div>
          ) : (
            <div>
              <label className="block text-sm text-bambu-gray mb-1">
                <PackageX className="w-4 h-4 inline mr-1" />
                {t('editArchive.defectiveParts')}
              </label>
              <input
                type="number"
                min={0}
                max={quantity}
                value={defectiveCount}
                onChange={(e) =>
                  setDefectiveCount(Math.min(quantity, Math.max(0, parseInt(e.target.value) || 0)))
                }
                data-testid="defective-count-input"
                className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                placeholder="0"
              />
              <p className="text-xs text-bambu-gray mt-1">
                {t('editArchive.defectivePartsHelp')}
              </p>
            </div>
          )}

          {/* Notes */}
          <div>
            <label className="block text-sm text-bambu-gray mb-1">{t('editArchive.notes')}</label>
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={3}
              className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none resize-none"
              placeholder={t('editArchive.notesPlaceholder')}
            />
          </div>

          {/* External Link */}
          <div>
            <label className="block text-sm text-bambu-gray mb-1">
              <Link className="w-4 h-4 inline mr-1" />
              {t('editArchive.externalLink')}
            </label>
            <input
              type="url"
              value={externalUrl}
              onChange={(e) => setExternalUrl(e.target.value)}
              className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
              placeholder="https://printables.com/model/..."
            />
            <p className="text-xs text-bambu-gray mt-1">
              {t('editArchive.externalLinkHelp')}
            </p>
          </div>

          {/* Tags */}
          <div>
            <label className="block text-sm text-bambu-gray mb-1">{t('editArchive.tags')}</label>
            {/* Current tags as chips */}
            {currentTags.length > 0 && (
              <div className="flex flex-wrap gap-1.5 mb-2">
                {currentTags.map((tag) => (
                  <span
                    key={tag}
                    className="inline-flex items-center gap-1 px-2 py-0.5 bg-bambu-dark-tertiary rounded text-sm text-white"
                  >
                    <Tag className="w-3 h-3" />
                    {tag}
                    <button
                      type="button"
                      onClick={() => removeTag(tag)}
                      className="ml-0.5 text-bambu-gray hover:text-white"
                    >
                      <X className="w-3 h-3" />
                    </button>
                  </span>
                ))}
              </div>
            )}
            {/* Tag input with suggestions */}
            <div className="relative">
              <input
                ref={tagInputRef}
                type="text"
                value={tags}
                onChange={(e) => setTags(e.target.value)}
                onFocus={() => {
                  if (blurTimeoutRef.current !== null) {
                    clearTimeout(blurTimeoutRef.current);
                  }
                  setShowTagSuggestions(true);
                }}
                onBlur={() => {
                  blurTimeoutRef.current = window.setTimeout(() => setShowTagSuggestions(false), 200);
                }}
                className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                placeholder={currentTags.length > 0 ? t('editArchive.addMoreTags') : t('editArchive.tagsPlaceholder')}
              />
              {/* Suggestions dropdown */}
              {showTagSuggestions && tagSuggestions.length > 0 && (
                <div className="absolute top-full left-0 right-0 mt-1 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-lg z-10 max-h-40 overflow-y-auto">
                  <div className="p-2 text-xs text-bambu-gray border-b border-bambu-dark-tertiary">
                    {currentInput ? t('editArchive.matchingTags', { query: currentInput }) : t('editArchive.existingTags')} {t('editArchive.clickToAdd')}
                  </div>
                  <div className="p-2 flex flex-wrap gap-1.5">
                    {tagSuggestions.map((tag) => (
                      <button
                        key={tag}
                        type="button"
                        onClick={() => addTag(tag)}
                        className="px-2 py-0.5 bg-bambu-dark-tertiary hover:bg-bambu-green/20 rounded text-sm text-bambu-gray hover:text-white transition-colors"
                      >
                        {tag}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* Status */}
          <div>
            <label className="block text-sm text-bambu-gray mb-1">{t('editArchive.status')}</label>
            <select
              value={status}
              onChange={(e) => {
                setStatus(e.target.value);
                // Clear failure reason + error details when changing to completed
                if (e.target.value === 'completed') {
                  setFailureReason('');
                  setErrorMessage('');
                }
              }}
              className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
            >
              {ARCHIVE_STATUS_KEYS.map((statusKey) => (
                <option key={statusKey} value={statusKey}>
                  {t(`editArchive.statuses.${statusKey}`)}
                </option>
              ))}
            </select>
          </div>

          {/* Failure Reason - only show for failed/aborted prints */}
          {(status === 'failed' || status === 'aborted') && (
            <div>
              <label htmlFor="failure-reason-select" className="block text-sm text-bambu-gray mb-1">{t('editArchive.failureReason')}</label>
              <select
                id="failure-reason-select"
                value={failureReason}
                onChange={(e) => setFailureReason(e.target.value)}
                className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
              >
                <option value="">{t('editArchive.selectReason')}</option>
                {FAILURE_REASON_KEYS.map((reasonKey) => (
                  <option key={reasonKey} value={reasonKey}>
                    {t(`editArchive.failureReasons.${reasonKey}`)}
                  </option>
                ))}
              </select>
            </div>
          )}

          {/* Error details (verbose error_message) - only for failed/aborted prints */}
          {(status === 'failed' || status === 'aborted') && (
            <div>
              <label className="block text-sm text-bambu-gray mb-1">{t('editArchive.errorMessage')}</label>
              <textarea
                value={errorMessage}
                onChange={(e) => setErrorMessage(e.target.value)}
                rows={2}
                className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none resize-none"
                placeholder={t('editArchive.errorMessagePlaceholder')}
              />
            </div>
          )}

          {/* Photos */}
          <div>
            <label className="block text-sm text-bambu-gray mb-1">
              <Camera className="w-4 h-4 inline mr-1" />
              {t('editArchive.photos')}
            </label>
            {/* Photo grid */}
            <div className="flex flex-wrap gap-2 mb-2">
              {photos.map((filename) => (
                <div key={filename} className="relative group">
                  <img
                    src={api.getArchivePhotoUrl(archive.id, filename)}
                    alt={t('editArchive.printResult')}
                    className="w-20 h-20 object-cover rounded-lg border border-bambu-dark-tertiary"
                  />
                  <button
                    type="button"
                    onClick={() => handlePhotoDelete(filename)}
                    className="absolute -top-1 -right-1 p-1 bg-red-500 rounded-full opacity-0 group-hover:opacity-100 transition-opacity"
                  >
                    <Trash2 className="w-3 h-3 text-white" />
                  </button>
                </div>
              ))}
              {/* Upload button */}
              <label className="w-20 h-20 flex items-center justify-center border-2 border-dashed border-bambu-dark-tertiary rounded-lg cursor-pointer hover:border-bambu-green transition-colors">
                <input
                  ref={photoInputRef}
                  type="file"
                  accept="image/jpeg,image/png,image/webp"
                  onChange={handlePhotoUpload}
                  className="hidden"
                  disabled={uploadingPhoto}
                />
                {uploadingPhoto ? (
                  <Loader2 className="w-6 h-6 text-bambu-gray animate-spin" />
                ) : (
                  <Plus className="w-6 h-6 text-bambu-gray" />
                )}
              </label>
            </div>
            <p className="text-xs text-bambu-gray">{t('editArchive.photosHelp')}</p>
          </div>

          {/* Actions */}
          <div className="flex gap-3 pt-2">
            <Button
              type="button"
              variant="secondary"
              onClick={onClose}
              className="flex-1"
            >
              {t('common.cancel')}
            </Button>
            <Button
              type="submit"
              disabled={updateMutation.isPending}
              className="flex-1"
            >
              <Save className="w-4 h-4" />
              {updateMutation.isPending ? t('common.saving') : t('common.save')}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
