import { useEffect, useMemo, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Loader2, Package, Search } from 'lucide-react';
import { api } from '../api/client';
import type { InventorySpool, SpoolAssignment } from '../api/client';
import { Button } from './Button';
import { ConfirmModal } from './ConfirmModal';
import { Modal } from './Modal';
import { useToast } from '../contexts/ToastContext';
import { useAuth } from '../contexts/AuthContext';
import { DEFAULT_SPOOL_DISPLAY_TEMPLATE, formatSpoolDisplayName } from '../utils/spoolName';
import { filterSpoolsByQuery } from '../utils/inventorySearch';
import { getSwatchStyle } from '../utils/colors';

interface AssignSpoolModalProps {
  isOpen: boolean;
  onClose: () => void;
  printerId: number;
  amsId: number;
  trayId: number;
  trayInfo?: {
    type: string;
    material?: string;
    profile?: string;
    color: string;
    location: string;
  };
  spoolmanEnabled?: boolean;
  /**
   * The spool currently on this slot, when the dialog was opened to REPLACE it
   * rather than to fill an empty slot. Set by the page from the same string the
   * hover card shows (`formatSpoolDisplayName`), so the dialog names the spool
   * the operator just read there.
   *
   * Its presence is the whole of replace mode: the title and the submit button
   * say Replace, the header names it, it is dropped from the list it belongs to
   * (`source`), and the success toast reports both names. The API call is
   * unchanged — an assign over an occupied slot has always replaced.
   */
  currentSpool?: { id: number; displayName: string; source: 'inventory' | 'spoolman' };
}

export function AssignSpoolModal({ isOpen, onClose, printerId, amsId, trayId, trayInfo, spoolmanEnabled, currentSpool }: AssignSpoolModalProps) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const [selectedSpoolId, setSelectedSpoolId] = useState<number | null>(null);
  const [selectedSpoolmanSpoolId, setSelectedSpoolmanSpoolId] = useState<number | null>(null);
  const [searchFilter, setSearchFilter] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [pickerPage, setPickerPage] = useState(1);
  const [disableFiltering, setDisableFiltering] = useState(false);
  const [pendingAssignId, setPendingAssignId] = useState<number | null>(null);
  const [showMismatchConfirm, setShowMismatchConfirm] = useState(false);
  const [mismatchDetails, setMismatchDetails] = useState<{
    type: 'material' | 'partial' | 'material_profile' | 'partial_profile';
    spoolMaterial: string;
    trayMaterial: string;
    spoolProfile?: string;
    trayProfile?: string;
  } | null>(null);
  // Mid-print the same gesture means two opposite things: a physical spool
  // replacement (usage must split at a pause layer) or a wrong-link
  // correction (the new spool owns the whole print). Both replacement
  // windows — paused right now, or running with a pause behind the print —
  // ask through the same modal; only the wording differs.
  const [replacementPrompt, setReplacementPrompt] = useState<{ spoolId?: number; spoolmanId?: number } | null>(null);

  // Reset selected spool(s) when filtering mode changes
  useEffect(() => {
    setSelectedSpoolId(null);
    setSelectedSpoolmanSpoolId(null);
  }, [disableFiltering]);

  useEffect(() => {
    setSelectedSpoolId(null);
    setSelectedSpoolmanSpoolId(null);
    setPickerPage(1);
  }, [spoolmanEnabled, printerId, amsId, trayId, currentSpool?.id]);

  // Reset filtering when modal opens
  useEffect(() => {
    if (isOpen) {
      setDisableFiltering(false);
      setReplacementPrompt(null);
      setPickerPage(1);
    }
  }, [isOpen]);

  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedSearch(searchFilter);
      setPickerPage(1);
    }, 250);
    return () => clearTimeout(timer);
  }, [searchFilter]);

  useEffect(() => setPickerPage(1), [disableFiltering, trayInfo?.type, trayInfo?.profile]);

  // The server applies the assignment/tray/search predicates before paging.
  const { data: spoolPage, isLoading } = useQuery({
    queryKey: ['inventory-spools', 'assign-modal', 'local', printerId, amsId, trayId,
      trayInfo?.type, trayInfo?.profile, disableFiltering,
      currentSpool?.source === 'inventory' ? currentSpool.id : null, debouncedSearch, pickerPage],
    queryFn: ({ signal }) => api.getSpoolPicker({
      printer_id: printerId,
      ams_id: amsId,
      tray_id: trayId,
      tray_material: trayInfo?.type || '',
      tray_profile: trayInfo?.profile || '',
      q: debouncedSearch,
      show_all: disableFiltering,
      replacing_spool_id: currentSpool?.source === 'inventory' ? currentSpool.id : undefined,
      page: pickerPage,
    }, signal),
    enabled: isOpen && !spoolmanEnabled && hasPermission('inventory:read'),
  });
  const spools = spoolPage?.items;
  const { data: selectedSpoolRecord } = useQuery({
    queryKey: ['inventory-spools', 'selected', selectedSpoolId],
    queryFn: () => api.getSpool(selectedSpoolId!),
    enabled: isOpen && !spoolmanEnabled && selectedSpoolId !== null && hasPermission('inventory:read'),
    retry: false,
  });

  const { data: assignments } = useQuery({
    queryKey: ['spool-assignments'],
    queryFn: () => api.getAssignments(),
    enabled: isOpen,
  });

  const { data: settings } = useQuery({
    queryKey: ['settings'],
    queryFn: () => api.getSettings(),
    enabled: isOpen,
  });

  const { data: spoolmanSpools, isLoading: spoolmanLoading } = useQuery({
    queryKey: ['spoolman-inventory-spools', 'assign-modal'],
    queryFn: () => api.getSpoolmanInventorySpools(false),
    enabled: isOpen && !!spoolmanEnabled,
  });

  // Spoolman SlotAssignments across all printers — used to filter out spools
  // already bound to another slot. Without this filter the modal offers spools
  // that are already in use elsewhere (e.g. an h2d-1 slot's spool appearing
  // in the x1c-2 assign list), and assigning would silently steal it from
  // the other printer's slot.
  const { data: allSpoolmanAssignments } = useQuery({
    queryKey: ['spoolman-slot-assignments-all'],
    queryFn: () => api.getSpoolmanSlotAssignments(),
    enabled: isOpen && !!spoolmanEnabled,
  });

  // ids of spools already in some Spoolman slot — excluding the current slot
  // (so a user could in theory re-pick the same spool, though the modal is
  // typically only opened from empty slots).
  const assignedSpoolmanSpoolIds = useMemo(() => {
    if (!allSpoolmanAssignments) return new Set<number>();
    return new Set(
      allSpoolmanAssignments
        .filter(a => !(a.printer_id === printerId && a.ams_id === amsId && a.tray_id === trayId))
        .map(a => a.spoolman_spool_id),
    );
  }, [allSpoolmanAssignments, printerId, amsId, trayId]);

  const { data: replacementWindow } = useQuery({
    queryKey: ['replacement-window', printerId, amsId, trayId],
    queryFn: () => api.getReplacementWindow(printerId, amsId, trayId),
    enabled: isOpen,
  });
  // 'prompt': paused — a swap is likely happening right now.
  // 'optin': running after a pause — the swap, if any, happened back then.
  // Both ask through the same modal (one question in one place — the inline
  // toggle this window used to get was routinely missed); 'none' assigns
  // straight away, a physical swap being impossible.
  //
  // ⚠️ Asked about THIS slot, not just this printer. Filling an empty slot
  // mid-print replaces nothing, and the question has no answer there — a
  // replacement charges what printed so far to the spool that came out.
  const windowMode = replacementWindow?.mode ?? 'none';

  // Replace mode: the dialog was opened over a slot that already holds a spool.
  const replacing = !!currentSpool;
  // Hoisted above the mutations because the success toast needs it too — the
  // name it reports must be the name the list showed, from the same template.
  const spoolDisplayTemplate = settings?.spool_display_template || DEFAULT_SPOOL_DISPLAY_TEMPLATE;
  const pickedDisplayName = (id: number, list: InventorySpool[] | undefined) => {
    const picked = list?.find((spool: InventorySpool) => spool.id === id)
      ?? (selectedSpoolRecord?.id === id ? selectedSpoolRecord : undefined);
    return picked ? formatSpoolDisplayName(picked, spoolDisplayTemplate) : `#${id}`;
  };

  const assignMutation = useMutation({
    mutationFn: ({ spoolId, midPrintReplacement }: { spoolId: number; midPrintReplacement: boolean }) =>
      api.assignSpool({
        spool_id: spoolId,
        printer_id: printerId,
        ams_id: amsId,
        tray_id: trayId,
        mid_print_replacement: midPrintReplacement,
      }),
    onSuccess: (newAssignment, variables) => {
      // Immediately update cache so UI reflects the new assignment without waiting for refetch
      queryClient.setQueryData<SpoolAssignment[]>(['spool-assignments'], (old) => {
        const filtered = (old || []).filter(a =>
          !(a.printer_id === printerId && a.ams_id === amsId && a.tray_id === trayId)
        );
        filtered.push(newAssignment);
        return filtered;
      });
      queryClient.invalidateQueries({ queryKey: ['spool-assignments'] });
      queryClient.invalidateQueries({ queryKey: ['inventory-spools', 'assign-modal'] });
      showToast(
        currentSpool
          ? // A replace over a slot whose filament is not loaded is still a
            // pending assignment, so replace mode keeps that hint — as its own
            // string, not `replaceSuccess` + `assignPendingInsert`, which opens
            // with "Spool assigned." and would contradict the sentence before
            // it.
            t(newAssignment.pending_config ? 'inventory.replacePendingInsert' : 'inventory.replaceSuccess', {
              old: currentSpool.displayName,
              new: pickedDisplayName(variables.spoolId, spools),
            })
          : t(newAssignment.pending_config ? 'inventory.assignPendingInsert' : 'inventory.assignSuccess'),
        'success',
      );
      setShowMismatchConfirm(false);
      setPendingAssignId(null);
      setMismatchDetails(null);
      onClose();
    },
    onError: (error: Error) => {
      showToast(`${t('inventory.assignFailed')}: ${error.message}`, 'error');
    },
  });

  const assignSpoolmanMutation = useMutation({
    mutationFn: ({ spoolmanSpoolId, midPrintReplacement }: { spoolmanSpoolId: number; midPrintReplacement: boolean }) =>
      api.assignSpoolmanSlot({
        spoolman_spool_id: spoolmanSpoolId,
        printer_id: printerId,
        ams_id: amsId,
        tray_id: trayId,
        mid_print_replacement: midPrintReplacement,
      }),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ['spoolman-inventory-spools'] });
      queryClient.invalidateQueries({ queryKey: ['spoolman-slot-assignments'] });
      showToast(
        currentSpool
          ? t('inventory.replaceSuccess', {
              old: currentSpool.displayName,
              new: pickedDisplayName(variables.spoolmanSpoolId, spoolmanSpools),
            })
          : t('inventory.assignSuccess'),
        'success',
      );
      onClose();
    },
    onError: (error: Error) => {
      showToast(`${t('inventory.assignFailed')}: ${error.message}`, 'error');
    },
  });

  // --- Material/profile mismatch logic ---
  const normalizeValue = (value: string | undefined | null) =>
    (value ?? '').trim().toUpperCase();

  const checkMaterialMatch = (
    spoolMaterial: string | undefined | null,
    trayMaterial: string | undefined | null
  ): 'exact' | 'partial' | 'none' => {
    const normalizedSpool = normalizeValue(spoolMaterial);
    const normalizedTray = normalizeValue(trayMaterial);

    if (!normalizedSpool || !normalizedTray) return 'none';
    if (normalizedSpool === normalizedTray) return 'exact';
    if (normalizedTray.includes(normalizedSpool) || normalizedSpool.includes(normalizedTray)) {
      return 'partial';
    }

    return 'none';
  };

  // Bambu Studio / OrcaSlicer profile names carry a printer/nozzle/variant qualifier
  // after `@` (e.g. "Devil Design PLA Basic @Bambu Lab H2D 0.4 nozzle (Custom)"),
  // while the tray's profile is typically the bare base name. Strip the qualifier
  // before comparing so identical base profiles don't trigger a mismatch warning
  // (upstream #1047).
  const stripProfileQualifier = (value: string) => value.split('@')[0].trim();

  const checkProfileMatch = (
    spoolProfile: string | undefined | null,
    trayProfile: string | undefined | null
  ): boolean => {
    const normalizedSpoolProfile = stripProfileQualifier(normalizeValue(spoolProfile));
    const normalizedTrayProfile = stripProfileQualifier(normalizeValue(trayProfile));

    if (!normalizedSpoolProfile || !normalizedTrayProfile) return false;

    return normalizedSpoolProfile === normalizedTrayProfile;
  };

  if (!isOpen) return null;

  // Filter out spools already assigned to other slots
  const assignedSpoolIds = new Set(
    (assignments || [])
      .filter(a => !(a.printer_id === printerId && a.ams_id === amsId && a.tray_id === trayId))
      .map(a => a.spool_id)
  );
  // Show every spool that isn't already taken by another slot — including
  // RFID-tagged Bambu Lab spools (#1133). The earlier "manual spools only"
  // gate (tag_uid && tray_uuid both null) blocked the workflow where a
  // user has a Bambu Lab spool in inventory and just wants to pick it
  // from the list instead of physically rescanning RFID. External slots
  // (amsId 254/255) have always been allowed to pick any spool because
  // the slot itself has no RFID reader; that distinction collapses now
  // that AMS slots also accept any spool.
  //
  // The "Show all spools" toggle (disableFiltering) bypasses BOTH this
  // gate and the material/profile filter below, making it a real escape
  // hatch — without this, the toggle's label would be a lie ("Show all"
  // but actually still filters by assignment).
  //
  // ⚠️ The spool being REPLACED is excluded separately, outside that bypass:
  // it is the one spool "Show all" must not bring back, because re-picking it
  // is a no-op the operator cannot mean. (Without replace mode the slot's own
  // spool deliberately stays in the list — an idempotent re-assign.)
  const availableSpools = spools?.filter((spool: InventorySpool) =>
    !spool.archived_at
    && (disableFiltering || !assignedSpoolIds.has(spool.id))
    && !(currentSpool?.source === 'inventory' && spool.id === currentSpool.id)
  );

  // The local predicate and tokenised search ran before pagination on the
  // server. Reapply only safety checks against assignments changed while open.
  const filteredSpools = availableSpools;

  // The Spoolman list, filtered in ONE place (it is read twice below — for the
  // "is there anything to show" gate and for the rows): archived spools are
  // never assignable, a spool bound to another slot is never offered (picking
  // it here would pull it out of another printer), and in replace mode the
  // spool being replaced goes too.
  const availableSpoolmanSpools = (spoolmanSpools || []).filter((spool: InventorySpool) =>
    !spool.archived_at
    && !assignedSpoolmanSpoolIds.has(spool.id)
    && !(currentSpool?.source === 'spoolman' && spool.id === currentSpool.id)
  );

  // The single funnel every assignment goes through. Inside either
  // replacement window with no answer yet, ask first; the answer re-enters
  // with the flag decided.
  const fireAssign = (target: { spoolId?: number; spoolmanId?: number }, midPrintReplacement?: boolean) => {
    if (midPrintReplacement === undefined && (windowMode === 'prompt' || windowMode === 'optin')) {
      setReplacementPrompt(target);
      return;
    }
    if (midPrintReplacement === undefined) {
      midPrintReplacement = false;
    }
    if (target.spoolmanId !== undefined) {
      assignSpoolmanMutation.mutate({ spoolmanSpoolId: target.spoolmanId, midPrintReplacement: !!midPrintReplacement });
    } else if (target.spoolId !== undefined) {
      assignMutation.mutate({ spoolId: target.spoolId, midPrintReplacement: !!midPrintReplacement });
    }
  };

  const handleAssign = async () => {
    if (selectedSpoolmanSpoolId !== null) {
      fireAssign({ spoolmanId: selectedSpoolmanSpoolId });
      return;
    }
    if (!selectedSpoolId) return;
    let selectedSpool: InventorySpool;
    try {
      selectedSpool = await queryClient.fetchQuery({
        queryKey: ['inventory-spools', 'selected', selectedSpoolId],
        queryFn: () => api.getSpool(selectedSpoolId),
        staleTime: 0,
      });
    } catch {
      showToast(t('inventory.assignFailed'), 'error');
      return;
    }
    if (!selectedSpool || selectedSpool.archived_at) {
      showToast(t('inventory.assignFailed'), 'error');
      return;
    }

    if (!settings?.disable_filament_warnings && trayInfo) {
      const trayMaterial = trayInfo.material || trayInfo.type;
      const materialMatchResult = checkMaterialMatch(selectedSpool.material, trayMaterial);
      const spoolProfile = selectedSpool.slicer_filament_name || selectedSpool.slicer_filament;
      const trayProfile = trayInfo.profile || trayInfo.type;
      const profileMatches = checkProfileMatch(spoolProfile, trayProfile);

      // Only material-bearing mismatches warn — profile-only deltas are
      // silently resolved by the backend's AMS reconfigure on every assign
      // (#1552), so warning then "fixing" in the same action was friction
      // without benefit. Combined material+profile mismatches keep the profile
      // detail in the same popup as the material warning.
      if (materialMatchResult !== 'exact') {
        let mismatchType: 'material' | 'partial' | 'material_profile' | 'partial_profile';

        if (materialMatchResult === 'none' && !profileMatches) {
          mismatchType = 'material_profile';
        } else if (materialMatchResult === 'partial' && !profileMatches) {
          mismatchType = 'partial_profile';
        } else if (materialMatchResult === 'none') {
          mismatchType = 'material';
        } else {
          mismatchType = 'partial';
        }

        setPendingAssignId(selectedSpoolId);
        setMismatchDetails({
          type: mismatchType,
          spoolMaterial: selectedSpool.material || '',
          trayMaterial: trayMaterial || '',
          spoolProfile: spoolProfile || undefined,
          trayProfile: trayProfile || undefined,
        });
        setShowMismatchConfirm(true);
        return;
      }
    }
    fireAssign({ spoolId: selectedSpoolId });
  };

  const handleConfirmMismatch = async () => {
    if (!pendingAssignId) return;
    try {
      const current = await queryClient.fetchQuery({
        queryKey: ['inventory-spools', 'selected', pendingAssignId],
        queryFn: () => api.getSpool(pendingAssignId),
        staleTime: 0,
      });
      if (current.archived_at) throw new Error('archived');
    } catch {
      showToast(t('inventory.assignFailed'), 'error');
      setShowMismatchConfirm(false);
      return;
    }
    fireAssign({ spoolId: pendingAssignId });
    setShowMismatchConfirm(false);
    setPendingAssignId(null);
  };

  return (
    <>
      <Modal
        onClose={onClose}
        title={replacing ? t('inventory.replaceSpool') : t('inventory.assignSpool')}
        icon={<Package className="w-5 h-5 text-bambu-green" />}
        size="2xl"
      >
        {/* Content */}
        <div className="p-4 space-y-4 overflow-y-auto">
          {/* What is being replaced. The name comes from the page, so it is the
              same string the hover card the operator came from showed. */}
          {currentSpool && (
            <p className="text-xs text-bambu-gray">
              {t('inventory.currentlyAssigned')}: <span className="text-white">{currentSpool.displayName}</span>
            </p>
          )}

          {/* Tray info */}
          {trayInfo && (
            <div className="p-3 bg-bambu-dark rounded-lg border border-bambu-dark-tertiary">
              <p className="text-xs text-bambu-gray mb-1">{t('inventory.selectSpool')}:</p>
              <div className="flex items-center gap-2">
                {trayInfo.color && (
                  <span
                    className="w-4 h-4 rounded-full border border-black/20"
                    style={getSwatchStyle(trayInfo.color)}
                  />
                )}
                <span className="text-white font-medium">{trayInfo.type || t('ams.emptySlot')}</span>
                <span className="text-bambu-gray">({trayInfo.location})</span>
              </div>
            </div>
          )}

          {/* Search filter */}
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-bambu-gray" />
            <input
              type="text"
              value={searchFilter}
              onChange={(e) => setSearchFilter(e.target.value)}
              placeholder={t('inventory.searchSpools')}
              className="w-full pl-9 pr-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm placeholder:text-bambu-gray focus:outline-none focus:border-bambu-green"
            />
          </div>

          {/* Spool list */}
          <div className="space-y-3">
            {!spoolmanEnabled && (isLoading ? (
              <div className="flex justify-center py-8">
                <Loader2 className="w-6 h-6 text-bambu-green animate-spin" />
              </div>
            ) : filteredSpools && filteredSpools.length > 0 ? (
              <div className="max-h-96 overflow-y-auto grid grid-cols-2 sm:grid-cols-3 gap-2">
                {filteredSpools.map((spool: InventorySpool) => (
                  <button
                    key={spool.id}
                    onClick={() => { setSelectedSpoolId(spool.id); setSelectedSpoolmanSpoolId(null); }}
                    title={spool.note || undefined}
                    className={`p-2.5 rounded-lg border text-left transition-colors ${
                      selectedSpoolId === spool.id
                        ? 'bg-bambu-green/20 border-bambu-green'
                        : 'bg-bambu-dark border-bambu-dark-tertiary hover:border-bambu-gray'
                    }`}
                  >
                    <p className="text-white text-sm font-medium truncate">
                      {formatSpoolDisplayName(spool, spoolDisplayTemplate)}
                    </p>
                    <div className="flex items-center gap-1.5 mt-1">
                      {spool.rgba && (
                        <span
                          className="w-3 h-3 rounded-full border border-black/20 flex-shrink-0"
                          style={getSwatchStyle(spool.rgba)}
                        />
                      )}
                      <span className="text-xs text-bambu-gray truncate">{spool.color_name || ''}</span>
                    </div>
                    {spool.label_weight && (
                      <p className="text-xs text-bambu-gray mt-1">
                        {Math.max(0, Math.round(spool.label_weight - spool.weight_used))} / {spool.label_weight}g
                      </p>
                    )}
                    {spool.note && (
                      <p className="text-[10px] text-bambu-gray/70 mt-1 truncate" title={spool.note}>
                        {spool.note}
                      </p>
                    )}
                  </button>
                ))}
              </div>
            ) : availableSpools && availableSpools.length === 0 ? (
              <div className="text-center py-8 text-bambu-gray">
                <p>{t(searchFilter.trim() ? 'inventory.noSpoolsMatch' : 'inventory.noAvailableSpools')}</p>
                {spoolPage && (
                  <p className="text-[10px] mt-2 opacity-60">
                    {t('common.pageOf', { page: pickerPage, total: spoolPage.meta.last_page })}
                    {' · '}{spoolPage.meta.total} {t('common.total')}
                  </p>
                )}
              </div>
            ) : (
              <div className="text-center py-8 text-bambu-gray">
                <p>{t('inventory.noSpoolsMatch')}</p>
                {spoolPage && (
                  <p className="text-[10px] mt-2 opacity-60">
                    {t('common.pageOf', { page: pickerPage, total: spoolPage.meta.last_page })}
                  </p>
                )}
              </div>
            ))}

            {!spoolmanEnabled && spoolPage && spoolPage.meta.last_page > 1 && (
              <div className="flex items-center justify-between gap-3 text-xs text-bambu-gray">
                <button type="button" disabled={pickerPage <= 1} onClick={() => setPickerPage(page => page - 1)}
                  className="disabled:opacity-40 hover:text-white">
                  {t('common.previousPage')}
                </button>
                <span>{t('common.pageOf', { page: pickerPage, total: spoolPage.meta.last_page })}</span>
                <button type="button" disabled={pickerPage >= spoolPage.meta.last_page}
                  onClick={() => setPickerPage(page => page + 1)} className="disabled:opacity-40 hover:text-white">
                  {t('common.nextPage')}
                </button>
              </div>
            )}

            {spoolmanEnabled && (
              <>
                {spoolmanLoading ? (
                  <div className="flex justify-center py-4">
                    <Loader2 className="w-5 h-5 text-bambu-green animate-spin" />
                  </div>
                ) : availableSpoolmanSpools.length > 0 ? (
                  <>
                    <p className="text-xs font-medium text-bambu-gray uppercase tracking-wide pt-1">
                      {t('inventory.spoolmanSpools')}
                    </p>
                    <div className="max-h-64 overflow-y-auto grid grid-cols-2 sm:grid-cols-3 gap-2">
                      {filterSpoolsByQuery(availableSpoolmanSpools, searchFilter)
                        .map((spool: InventorySpool) => (
                          <button
                            key={`spoolman-${spool.id}`}
                            onClick={() => {
                              setSelectedSpoolmanSpoolId(spool.id);
                              setSelectedSpoolId(null);
                            }}
                            title={spool.note || undefined}
                            className={`p-2.5 rounded-lg border text-left transition-colors ${
                              selectedSpoolmanSpoolId === spool.id
                                ? 'bg-bambu-green/20 border-bambu-green'
                                : 'bg-bambu-dark border-bambu-dark-tertiary hover:border-bambu-gray'
                            }`}
                          >
                            <p className="text-white text-sm font-medium truncate">
                              {formatSpoolDisplayName(spool, spoolDisplayTemplate)}
                            </p>
                            <div className="flex items-center gap-1.5 mt-1">
                              {spool.rgba && (
                                <span
                                  className="w-3 h-3 rounded-full border border-black/20 flex-shrink-0"
                                  style={getSwatchStyle(spool.rgba)}
                                />
                              )}
                              <span className="text-xs text-bambu-gray truncate">{spool.color_name || ''}</span>
                            </div>
                            {spool.label_weight && (
                              <p className="text-xs text-bambu-gray mt-1">
                                {Math.max(0, Math.round(spool.label_weight - spool.weight_used))} / {spool.label_weight}g
                              </p>
                            )}
                            {spool.note && (
                              <p className="text-[10px] text-bambu-gray/70 mt-1 truncate" title={spool.note}>
                                {spool.note}
                              </p>
                            )}
                          </button>
                        ))}
                    </div>
                  </>
                ) : null}
              </>
            )}
          </div>
        </div>

        {/* Footer */}
        <div className="flex justify-between items-center p-4 border-t border-bambu-dark-tertiary">
          <label className="flex items-center gap-2 text-sm text-bambu-gray cursor-pointer select-none">
            <input
              type="checkbox"
              checked={disableFiltering}
              onChange={(e) => setDisableFiltering(e.target.checked)}
              className="accent-bambu-green rounded border-bambu-dark-tertiary bg-bambu-dark text-bambu-green focus:ring-bambu-green"
            />
            {t('inventory.showAllSpools')}
          </label>
          <div className="flex gap-2">
            <Button variant="secondary" onClick={onClose}>
              {t('common.cancel')}
            </Button>
            <Button
              onClick={handleAssign}
              disabled={(!selectedSpoolId && selectedSpoolmanSpoolId === null) || assignMutation.isPending || assignSpoolmanMutation.isPending}
            >
              {(assignMutation.isPending || assignSpoolmanMutation.isPending) ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  {t('inventory.assigning')}
                </>
              ) : (
                <>
                  <Package className="w-4 h-4" />
                  {replacing ? t('inventory.replaceSpool') : t('inventory.assignSpool')}
                </>
              )}
            </Button>
          </div>
        </div>


        {assignMutation.isError && (
          <div className="mx-4 mb-4 p-2 bg-red-100 dark:bg-red-500/20 border border-red-300 dark:border-red-500/50 rounded text-sm text-red-700 dark:text-red-400">
            {(assignMutation.error as Error).message}
          </div>
        )}

      </Modal>

      {showMismatchConfirm && trayInfo && selectedSpoolId && mismatchDetails && (() => {
        let message = '';

        if (mismatchDetails.type === 'material') {
          message = t('inventory.assignMismatchMessage', {
            spoolMaterial: mismatchDetails.spoolMaterial,
            trayMaterial: mismatchDetails.trayMaterial,
            location: trayInfo.location,
          });
        } else if (mismatchDetails.type === 'partial') {
          message = t('inventory.assignPartialMismatchMessage', {
            spoolMaterial: mismatchDetails.spoolMaterial,
            trayMaterial: mismatchDetails.trayMaterial,
            location: trayInfo.location,
          });
        } else if (mismatchDetails.type === 'material_profile') {
          message = `${t('inventory.assignMismatchMessage', {
            spoolMaterial: mismatchDetails.spoolMaterial,
            trayMaterial: mismatchDetails.trayMaterial,
            location: trayInfo.location,
          })}\n\n${t('inventory.assignProfileMismatchMessage', {
            spoolProfile: mismatchDetails.spoolProfile || t('common.unknown'),
            trayProfile: mismatchDetails.trayProfile || t('common.unknown'),
            location: trayInfo.location,
          })}`;
        } else if (mismatchDetails.type === 'partial_profile') {
          message = `${t('inventory.assignPartialMismatchMessage', {
            spoolMaterial: mismatchDetails.spoolMaterial,
            trayMaterial: mismatchDetails.trayMaterial,
            location: trayInfo.location,
          })}\n\n${t('inventory.assignProfileMismatchMessage', {
            spoolProfile: mismatchDetails.spoolProfile || t('common.unknown'),
            trayProfile: mismatchDetails.trayProfile || t('common.unknown'),
            location: trayInfo.location,
          })}`;
        }

        // Always tell the user the AMS slot will be reconfigured — the old
        // wording made "Assign Anyway" read like a no-op confirmation, when the
        // backend in fact pushes the spool's profile to the slot on every
        // assign (#1552).
        message = `${message}\n\n${t('inventory.assignReconfigureNote')}`;

        return (
          <ConfirmModal
            title={t('inventory.assignMismatchTitle')}
            message={message}
            confirmText={t('inventory.assignMismatchConfirm')}
            variant="warning"
            isLoading={assignMutation.isPending}
            onConfirm={handleConfirmMismatch}
            onCancel={() => {
              if (!assignMutation.isPending) {
                setShowMismatchConfirm(false);
                setPendingAssignId(null);
                setMismatchDetails(null);
              }
            }}
          />
        );
      })()}

      {replacementPrompt && (
        <Modal
          onClose={() => setReplacementPrompt(null)}
          hideClose
          ariaLabel={
            windowMode === 'optin'
              ? t('inventory.midPrintReplacement.titleOptin')
              : t('inventory.midPrintReplacement.title')
          }
          size="md"
        >
          <div className="p-4">
            <h3 className="text-lg font-semibold text-white">
              {windowMode === 'optin'
                ? t('inventory.midPrintReplacement.titleOptin')
                : t('inventory.midPrintReplacement.title')}
            </h3>
            <p className="mt-3 text-sm text-bambu-gray whitespace-pre-line">
              {windowMode === 'optin'
                ? t('inventory.midPrintReplacement.bodyOptin', { layer: replacementWindow?.pause_layer ?? 0 })
                : t('inventory.midPrintReplacement.body')}
            </p>
            <div className="mt-4 flex flex-col gap-2">
              <Button
                variant="primary"
                disabled={assignMutation.isPending || assignSpoolmanMutation.isPending}
                onClick={() => {
                  const target = replacementPrompt;
                  setReplacementPrompt(null);
                  fireAssign(target, true);
                }}
              >
                {t('inventory.midPrintReplacement.replace')}
              </Button>
              <Button
                variant="secondary"
                disabled={assignMutation.isPending || assignSpoolmanMutation.isPending}
                onClick={() => {
                  const target = replacementPrompt;
                  setReplacementPrompt(null);
                  fireAssign(target, false);
                }}
              >
                {t('inventory.midPrintReplacement.correct')}
              </Button>
              <Button variant="ghost" onClick={() => setReplacementPrompt(null)}>
                {t('common.cancel')}
              </Button>
            </div>
          </div>
        </Modal>
      )}
    </>
  );
}
