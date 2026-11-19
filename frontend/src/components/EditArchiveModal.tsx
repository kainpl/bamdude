import { useState, useEffect, useRef } from 'react';
import { NumberInput } from './NumberInput';
import { useMutation, useQueryClient, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { X, Save, Tag, Camera, Trash2, Loader2, Plus, FolderKanban, Hash, Link, PackagePlus, Weight } from 'lucide-react';
import { api } from '../api/client';
import type { Archive } from '../api/client';
import { Button } from './Button';
import { Modal } from './Modal';
import { DefectsFields } from './DefectsFields';
import { useAuth } from '../contexts/AuthContext';
import { canFileFuturePrint } from '../utils/workshopRights';
import { useToast } from '../contexts/ToastContext';
import { OrderChoice } from './pickers/OrderChoice';
import { OrderLinePicker } from './pickers/OrderLinePicker';
import { useOrderDetail } from '../hooks/useOrderDetail';
import { invalidateOrderViews, invalidateProductCatalog } from '../utils/queryInvalidation';
import { Select } from './Select';
import { HAND_SET_ARCHIVE_STATUSES, archiveStatusLabel } from '../utils/archiveStatus';
import { useMarkOutcomeChecked } from '../hooks/useMarkOutcomeChecked';

// Keys for failure reasons - translated at render time. The backend stores
// exactly these (utils/failure_reasons.py; a test pins the two lists).
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
  'noStatusUpdate',
  'other',
  'dispatchSourceUnavailable',
  'dispatchUploadFailed',
  'dispatchPrinterUnavailable',
  'dispatchStartRefused',
  'dispatchCommandRejected',
  'dispatchNoAcknowledgement',
  'dispatchAcceptedNotStarted',
  'dispatchPreparationError',
  'dispatchInternalError',
] as const;

interface EditArchiveModalProps {
  archive: Archive;
  onClose: () => void;
  existingTags?: string[];
}

// What the server accepts for a typed filament figure (`ArchiveUpdate`).
const MAX_FILAMENT_GRAMS = 100_000;

/**
 * Keep only a number while it is typed: digits and one decimal separator, a
 * comma as good as a point, clamped to what the server accepts.
 *
 * A text field rather than a number input (upstream d227d422): a number input
 * reports "" for anything the browser judges malformed — a decimal comma in a
 * locale that does not expect one included — and that would read as "cleared"
 * and wipe a good figure while the field still showed what was typed. This
 * modal has no error surface, so a refused save would look like nothing
 * happened; filtering here keeps what is shown and what is sent the same.
 */
function filamentGramsInput(raw: string): string {
  let out = '';
  let separator = false;
  for (const ch of raw) {
    if (ch >= '0' && ch <= '9') {
      out += ch;
    } else if ((ch === '.' || ch === ',') && !separator) {
      separator = true;
      out += ch;
    }
  }
  return Number(out.replace(',', '.')) > MAX_FILAMENT_GRAMS ? String(MAX_FILAMENT_GRAMS) : out;
}

export function EditArchiveModal({ archive, onClose, existingTags = [] }: EditArchiveModalProps) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const { showToast } = useToast();

  const queryClient = useQueryClient();
  const [printName, setPrintName] = useState(archive.print_name || '');
  const [projectId, setProjectId] = useState<number | null>(archive.project_id ?? null);
  // Pass 2: which LINE of the order the print counts against. ⚠️ Reset whenever
  // the order changes — the server rejects (400) a line belonging to another
  // order, so the mismatch must never be submittable from here.
  const [projectLineId, setProjectLineId] = useState<number | null>(archive.project_line_id ?? null);
  // The chosen order's status (the line picker's own query): nothing new is filed into a
  // closed order, so its line stays as it is.
  const chosenOrder = useOrderDetail(projectId);
  const chosenOrderClosed = chosenOrder.data != null && chosenOrder.data.status !== 'active';
  // WS-13 E13 B06 / O21: the order and the line move with the right to file the print — the
  // dialog is opened with the archive's own right, so Fф (`orders:update` or
  // `orders:file_prints`) is what the server asks of a changed binding; without it they are
  // shown, not offered.
  const canChangeOrder = canFileFuturePrint(hasPermission);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [notes, setNotes] = useState(archive.notes || '');
  const [tags, setTags] = useState(archive.tags || '');
  // Failure reason is stored as a camelCase key (`filamentRunout`); m186 folded
  // the historical labels onto the keys. Reverse-lookup any legacy text still
  // spelled as a label in the current locale, and otherwise KEEP the value as
  // its own option: falling back to '' used to save the empty selection over
  // the stored text, so opening the editor and pressing Save erased it (#2974).
  const [failureReason, setFailureReason] = useState(() => {
    const raw = archive.failure_reason || '';
    if (!raw) return '';
    if ((FAILURE_REASON_KEYS as readonly string[]).includes(raw)) return raw;
    const match = FAILURE_REASON_KEYS.find(
      (k) => t(`editArchive.failureReasons.${k}`) === raw,
    );
    return match || raw;
  });
  // A stored value outside the vocabulary: shown as itself, so it can be kept.
  const legacyFailureReason =
    archive.failure_reason && !FAILURE_REASON_KEYS.some(
      (k) => k === archive.failure_reason || t(`editArchive.failureReasons.${k}`) === archive.failure_reason,
    )
      ? archive.failure_reason
      : null;
  const [errorMessage, setErrorMessage] = useState(archive.error_message || '');
  const [status, setStatus] = useState(archive.status);
  // A running print's status follows the printer (the server answers 409): shown, never offered.
  const statusLocked = archive.status === 'printing';
  // A stored status the editor does not offer (`printing`, the legacy `stopped` / `archived`,
  // anything unknown) is still the print's status: shown as itself, so the dialog never opens
  // on some other option and a save without touching it sends nothing.
  const storedStatusIsOffered = (HAND_SET_ARCHIVE_STATUSES as readonly string[]).includes(archive.status);
  const [quantity, setQuantity] = useState(archive.quantity ?? 1);
  // Filament used, typed by hand (audit D6 part 2) — above all for a print whose
  // 3MF never arrived, which has no figure and no file to rescan. Sent only
  // when changed, so an untouched save never rewrites the stored figure.
  const initialFilamentGrams = archive.filament_used_grams == null ? '' : String(archive.filament_used_grams);
  const [filamentGrams, setFilamentGrams] = useState(initialFilamentGrams);
  const [defectiveCount, setDefectiveCount] = useState(archive.defective_count ?? 0);
  const markChecked = useMarkOutcomeChecked(archive.id);
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

  // One synchronous flag for the save and for every way out (WS-13 E13 V02): a second press
  // in the same frame sends nothing, and Cancel, Escape or the × close nothing while the PATCH
  // is held. A refusal frees it for a retry; a success closes from here.
  const sent = useRef(false);
  const leave = () => {
    if (sent.current) return;
    onClose();
  };

  const updateMutation = useMutation({
    mutationFn: (data: Parameters<typeof api.updateArchive>[1]) =>
      api.updateArchive(archive.id, data),
    onMutate: () => setSaveError(null),
    onSuccess: (saved) => {
      if (saved?.stock_not_followed) showToast(t('archives.stockNotFollowed'), 'warning');
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      // Re-filing a print moves it between orders and between customers, so
      // both pages the edit could have touched are stale — hence prefixes,
      // decided once in `utils/queryInvalidation.ts`.
      invalidateOrderViews(queryClient);
      onClose();
    },
    // A refusal (403 rights, 409 a print its order received) stays here, in the
    // server's words — the edit is not lost (WS-13 E13 D03).
    onError: (e: Error) => {
      sent.current = false;
      setSaveError(e.message);
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
      // The catalog's figures through their one helper (WS-13 E13 D04).
      invalidateProductCatalog(queryClient);
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
    if (sent.current) return;
    // Build update data
    const updateData: Parameters<typeof api.updateArchive>[1] = {
      print_name: printName || undefined,
      notes: notes || undefined,
      tags: tags || undefined,
      quantity: quantity,
      external_url: externalUrl || null,
    };

    // The binding goes only when it changed, and then whole (WS-13 E13 D02): a save of
    // another field never writes the order or the line, so a picker that has not read
    // — or could not — cannot clear a binding nobody touched.
    if (projectId !== (archive.project_id ?? null) || projectLineId !== (archive.project_line_id ?? null)) {
      updateData.project_id = projectId;
      updateData.project_line_id = projectLineId;
    }

    if (filamentGrams !== initialFilamentGrams) {
      const grams = Number(filamentGrams.replace(',', '.'));
      updateData.filament_used_grams = filamentGrams === '' || !Number.isFinite(grams) ? null : grams;
    }

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
    } else if (defectiveCount !== (archive.defective_count ?? 0)) {
      // Only a count that was changed: every defects write marks the result as
      // checked, and saving a note must not say somebody graded the plate. An
      // unchanged zero is confirmed with «No defects» below.
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
    } else if (status === 'completed' && archive.status !== 'completed') {
      // A print that becomes completed carries no reason — «cancelled by user» on a
      // completed print included. Moving to cancelled keeps what was recorded.
      updateData.failure_reason = null;
      updateData.error_message = null;
    }

    sent.current = true;
    updateMutation.mutate(updateData);
  };

  return (
    <Modal
      onClose={leave}
      closeDisabled={updateMutation.isPending}
      title={t('editArchive.title')}
      size="md"
      bodyClassName="flex flex-col"
    >
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

        {/* Order */}
        <div>
          <label htmlFor="edit-archive-order" className="block text-sm text-bambu-gray mb-1">
            <FolderKanban className="w-4 h-4 inline mr-1" />
            {t('editArchive.order')}
          </label>
          <OrderChoice
            id="edit-archive-order"
            stacked
            allowNone
            value={projectId}
            onChange={(next) => {
              setProjectId(next?.id ?? null);
              setProjectLineId(null);
            }}
            disabled={!canChangeOrder}
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
            // A closed order's line is not changed into another of its lines (D01).
            disabled={!canChangeOrder || chosenOrderClosed}
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
        {hasPermission('stock:adjust') &&
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
          <NumberInput
            min={1}
            value={quantity}
            onChange={(v) => setQuantity(v)}
            integer
            fallback={1}
            className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
            placeholder="1"
          />
          <p className="text-xs text-bambu-gray mt-1">
            {t('editArchive.itemsPrintedHelp')}
          </p>
        </div>

        {/* Filament used — the archive's figure only; no spool is debited. */}
        <div>
          <label htmlFor="edit-archive-filament-grams" className="block text-sm text-bambu-gray mb-1">
            <Weight className="w-4 h-4 inline mr-1" />
            {t('editArchive.filamentUsed')}
          </label>
          <input
            id="edit-archive-filament-grams"
            type="text"
            inputMode="decimal"
            autoComplete="off"
            value={filamentGrams}
            onChange={(e) => setFilamentGrams(filamentGramsInput(e.target.value))}
            className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
          />
          <p className="text-xs text-bambu-gray mt-1">
            {t('editArchive.filamentUsedHelp')}
          </p>
        </div>

        {/* Defective parts — scrap out of the plate above. One form, shared
            with the order page and the printer card (`DefectsFields`). */}
        <DefectsFields
          parts={parts}
          values={partsDefective}
          onChange={(id, next) => {
            setPartsDefective((prev) => ({ ...prev, [id]: next }));
            setPartsDirty(true);
          }}
          quantity={quantity}
          flat={defectiveCount}
          onFlatChange={setDefectiveCount}
        />

        {/* Nobody has looked at this print's result yet: defects above, or «none». */}
        {archive.outcome_unchecked && !markChecked.isSuccess && (
          <div className="flex items-center justify-between gap-3 rounded-lg border border-amber-400/40 bg-amber-400/5 px-3 py-2">
            <p className="text-xs text-bambu-gray">{t('archives.outcome.noDefectsHelp')}</p>
            <Button
              type="button"
              variant="secondary"
              size="sm"
              className="flex-shrink-0"
              onClick={() => markChecked.mutate()}
              disabled={markChecked.isPending}
            >
              {t('archives.outcome.noDefects')}
            </Button>
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
          <label htmlFor="edit-archive-status" className="block text-sm text-bambu-gray mb-1">{t('editArchive.status')}</label>
          <Select
            className="w-full"
            id="edit-archive-status"
            value={status}
            disabled={statusLocked}
            aria-describedby={statusLocked ? 'edit-archive-status-locked' : undefined}
            onChange={(e) => {
              setStatus(e.target.value);
              // Clear failure reason + error details when changing to completed
              if (e.target.value === 'completed') {
                setFailureReason('');
                setErrorMessage('');
              }
            }}
          >
            {!storedStatusIsOffered && (
              <option value={archive.status} disabled>
                {archiveStatusLabel(t, archive.status)}
              </option>
            )}
            {HAND_SET_ARCHIVE_STATUSES.map((statusKey) => (
              <option key={statusKey} value={statusKey}>
                {archiveStatusLabel(t, statusKey)}
              </option>
            ))}
          </Select>
          {statusLocked && (
            <p id="edit-archive-status-locked" className="text-xs text-bambu-gray mt-1">
              {t('editArchive.statusLocked')}
            </p>
          )}
        </div>

        {/* Failure Reason - only show for failed/aborted prints */}
        {(status === 'failed' || status === 'aborted') && (
          <div>
            <label htmlFor="failure-reason-select" className="block text-sm text-bambu-gray mb-1">{t('editArchive.failureReason')}</label>
            <Select
              className="w-full"
              id="failure-reason-select"
              value={failureReason}
              onChange={(e) => setFailureReason(e.target.value)}
            >
              <option value="">{t('editArchive.selectReason')}</option>
              {legacyFailureReason && <option value={legacyFailureReason}>{legacyFailureReason}</option>}
              {FAILURE_REASON_KEYS.map((reasonKey) => (
                <option key={reasonKey} value={reasonKey}>
                  {t(`editArchive.failureReasons.${reasonKey}`)}
                </option>
              ))}
            </Select>
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
                  className="absolute -top-1 -right-1 p-1 bg-red-500 rounded-full can-hover:opacity-0 group-hover:opacity-100 focus-visible:opacity-100 transition-opacity"
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

        {saveError && (
          <p role="alert" className="text-sm text-red-600 dark:text-red-400 [overflow-wrap:anywhere]">
            {saveError}
          </p>
        )}

        {/* Actions */}
        <div className="flex gap-3 pt-2">
          <Button
            type="button"
            variant="secondary"
            onClick={leave}
            disabled={updateMutation.isPending}
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
    </Modal>
  );
}
