import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Circle, Check, AlertTriangle, RefreshCw, ChevronDown, ChevronUp, Palette } from 'lucide-react';
import { api } from '../../api/client';
import { useFilamentMapping } from '../../hooks/useFilamentMapping';
import { filamentColorMatches, filamentRequirementMatches, filamentTypesCompatible, getGlobalTrayId } from '../../utils/amsHelpers';
import { getColorName } from '../../utils/colors';
import { useFilamentLabels } from './useFilamentLabels';
import type { FilamentMappingProps } from './types';

/**
 * Filament mapping UI for comparing required filaments with loaded AMS slots.
 * Shows auto-matched and manually overridden slot assignments.
 */
export function FilamentMapping({
  printerId,
  filamentReqs,
  manualMappings,
  onManualMappingChange,
  currencySymbol,
  defaultCostPerKg,
  defaultExpanded = false,
  forceColorMatch,
  onForceColorMatchChange,
  requireExactColor = false,
  plateLabel,
  resolvedMapping,
  routingReason,
}: FilamentMappingProps & { defaultExpanded?: boolean }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [isExpanded, setIsExpanded] = useState(defaultExpanded);

  // Fetch printer status
  const { data: printerStatus } = useQuery({
    queryKey: ['printer-status', printerId],
    queryFn: ({ signal }) => api.getPrinterStatus(printerId, signal),
    enabled: !!printerId,
  });

  const { data: assignments } = useQuery({
    queryKey: ['spool-assignments', printerId],
    queryFn: () => api.getAssignments(printerId),
    enabled: !!printerId,
  });

  const { loadedFilaments, filamentComparison: rawFilamentComparison, hasTypeMismatch: rawTypeMismatch, hasColorMismatch: rawColorMismatch } =
    useFilamentMapping(filamentReqs, printerStatus, manualMappings);
  // The global exact-colour rule is a routing rule, not merely a warning.  The
  // old panel still painted it yellow, which made the checkbox look inert even
  // though dispatch would subsequently reject that selection.
  const filamentComparison = useMemo(() => {
    const comparison = Array.isArray(resolvedMapping) ? rawFilamentComparison.map(item => {
      const loaded = loadedFilaments.find(source => source.globalTrayId === resolvedMapping[item.slot_id - 1]);
      const typeMatch = !!loaded && filamentRequirementMatches(item, loaded);
      const colorMatch = !!loaded && filamentColorMatches(item, loaded);
      return { ...item, loaded, hasFilament: !!loaded, typeMatch, colorMatch,
        status: !typeMatch ? 'mismatch' as const : colorMatch ? 'match' as const : 'type_only' as const };
    }) : rawFilamentComparison;
    return requireExactColor
      ? comparison.map(item => item.status === 'type_only' ? { ...item, status: 'mismatch' as const } : item)
      : comparison;
  }, [requireExactColor, rawFilamentComparison, resolvedMapping, loadedFilaments]);
  const profileMismatch = rawFilamentComparison.some(item => !item.typeMatch &&
    loadedFilaments.some(source => filamentTypesCompatible(source.type, item.type)) &&
    !loadedFilaments.some(source => filamentRequirementMatches(item, source)));
  const hasTypeMismatch = Array.isArray(resolvedMapping)
    ? filamentComparison.some(item => !item.hasFilament || !item.typeMatch || (requireExactColor && !item.colorMatch))
    : rawTypeMismatch || (requireExactColor && rawColorMismatch);
  const hasColorMismatch = !requireExactColor && filamentComparison.some(item => item.typeMatch && !item.colorMatch);

  // Per-slot sub-brand + material-disambiguated colour labels (#1718). Shared
  // hook, extracted back when a second (model-mode) panel consumed it, so the
  // sliced-3MF identity resolves in one place. Falls back to the raw type / generic colour
  // bucket when the SKU is unknown or the by-material lookup hasn't resolved —
  // never blanks out the required row.
  const filamentLabels = useFilamentLabels(filamentReqs?.filaments);

  const trayCostMap = useMemo(() => {
    const map = new Map<number, number | null>();
    for (const assignment of assignments || []) {
      const isExternal = assignment.ams_id === 255;
      const globalTrayId = getGlobalTrayId(assignment.ams_id, assignment.tray_id, isExternal);
      map.set(globalTrayId, assignment.spool?.cost_per_kg ?? null);
    }
    return map;
  }, [assignments]);

  const trayRemainingWeightMap = useMemo(() => {
    const map = new Map<number, number | null>();
    for (const assignment of assignments || []) {
      const isExternal = assignment.ams_id === 255;
      const globalTrayId = getGlobalTrayId(assignment.ams_id, assignment.tray_id, isExternal);
      const spool = assignment.spool;
      if (!spool) {
        map.set(globalTrayId, null);
        continue;
      }
      map.set(globalTrayId, Math.max(0, Math.round((spool.label_weight ?? 0) - (spool.weight_used ?? 0))));
    }
    return map;
  }, [assignments]);

  const totalCost = useMemo(() => {
    let total = 0;
    for (const item of filamentComparison) {
      const trayId = item.loaded?.globalTrayId;
      if (trayId == null) continue;
      const assignedCost = trayCostMap.get(trayId) ?? null;
      const costPerKg = assignedCost ?? defaultCostPerKg;
      if (costPerKg > 0) {
        total += (item.used_grams / 1000) * costPerKg;
      }
    }
    return total;
  }, [filamentComparison, trayCostMap, defaultCostPerKg]);

  const hasAnyCost = useMemo(
    () => Array.from(trayCostMap.values()).some((v) => v != null && v > 0),
    [trayCostMap]
  );
  const hasFilamentReqs = filamentReqs?.filaments && filamentReqs.filaments.length > 0;
  const isDualNozzle = filamentReqs?.filaments?.some((f) => f.nozzle_id != null) ?? false;

  // Filament Track Switch: when installed, AMS-to-extruder mapping is dynamic
  // (any slot can be routed to either extruder), so the per-nozzle dropdown
  // filter is suppressed. Upstream Bambuddy #1162.
  //
  // What a slot CAN be labelled with is the switch inlet its AMS is plumbed
  // into (ams_switch_inlet, from AMS info bits 24-27) — the relationship the
  // printer's own "Manual AMS Setup" screen sets. The live inlet-to-outlet
  // route is deliberately not shown: the firmware never reports which inlet is
  // currently paired with which outlet, so a left/right label on a slot would
  // be a guess (upstream 7a42e0a7 removed the per-slot nozzle hint for that).
  const ftsInstalled = printerStatus?.fila_switch?.installed === true;
  const amsSwitchInlet = printerStatus?.ams_switch_inlet;
  const ftsInletForAms = (amsId: number): 'A' | 'B' | null =>
    (ftsInstalled && amsSwitchInlet?.[String(amsId)]) || null;

  // Every filament of this print behind ONE inlet is worth a word. Bambu's own
  // guidance: a change between two filaments on the same inlet retracts the
  // old one all the way back to its AMS before the next can be fed up the
  // shared tube, where a change across the two inlets only retracts as far as
  // the switch. It advises; it never blocks.
  const sameInletWarning = useMemo(() => {
    if (!ftsInstalled || filamentComparison.length < 2) return null;
    const inlets = new Set<'A' | 'B'>();
    for (const item of filamentComparison) {
      if (!item.loaded || item.loaded.isExternal) return null;
      const inlet = (amsSwitchInlet?.[String(item.loaded.amsId)]) || null;
      if (!inlet) return null;
      inlets.add(inlet);
    }
    return inlets.size === 1 ? [...inlets][0] : null;
  }, [ftsInstalled, amsSwitchInlet, filamentComparison]);

  // Don't render if no filament requirements
  if (!hasFilamentReqs) {
    return null;
  }

  // Don't render until we have printer status to do the comparison
  if (!printerStatus) {
    return null;
  }

  // Determine status indicator color
  const statusColor = routingReason || hasTypeMismatch
    ? '#f97316' // orange
    : hasColorMismatch
    ? '#facc15' // yellow
    : '#00ae42'; // green

  const handleSlotChange = (slotId: number, value: string) => {
    if (slotId > 0) {
      if (value === '') {
        // Clear manual override
        const next = { ...manualMappings };
        delete next[slotId];
        onManualMappingChange(next);
      } else {
        onManualMappingChange({
          ...manualMappings,
          [slotId]: parseInt(value, 10),
        });
      }
    }
  };

  const handleRefresh = async () => {
    setIsRefreshing(true);
    try {
      // Request fresh data from printer via MQTT pushall command
      await api.refreshPrinterStatus(printerId);
      // Wait a moment for printer to respond, then refetch
      await new Promise((r) => setTimeout(r, 500));
      await queryClient.refetchQueries({ queryKey: ['printer-status', printerId] });
      await queryClient.invalidateQueries({ queryKey: ['printer-routing-preview'] });
    } finally {
      setIsRefreshing(false);
    }
  };

  return (
    <div className="mb-4">
      <button
        type="button"
        onClick={() => setIsExpanded(!isExpanded)}
        className="flex items-center gap-2 text-sm text-bambu-gray hover:text-white transition-colors w-full"
      >
        <Circle className="w-4 h-4" fill={statusColor} stroke="none" />
        <span>{plateLabel ? `${t('printModal.filamentMapping')} — ${plateLabel}` : t('printModal.filamentMapping')}</span>
        {routingReason || hasTypeMismatch ? (
          <span className="text-xs text-orange-700 dark:text-orange-400">({routingReason || t(profileMismatch ? 'filamentRouting.feasibility.reason.variant_mismatch' : requireExactColor && rawColorMismatch ? 'printModal.filamentColorMismatch' : 'printModal.filamentTypeNotFound')})</span>
        ) : hasColorMismatch ? (
          <span className="text-xs text-yellow-700 dark:text-yellow-400">({t('printModal.filamentColorMismatch')})</span>
        ) : (
          <span className="text-xs text-bambu-green">({t('printModal.filamentReady')})</span>
        )}
        {isExpanded ? (
          <ChevronUp className="w-4 h-4 ml-auto" />
        ) : (
          <ChevronDown className="w-4 h-4 ml-auto" />
        )}
      </button>

      {isExpanded && (
        <div className="mt-2 bg-bambu-dark rounded-lg p-3 space-y-2">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs text-bambu-gray">{t('printModal.clickToChangeSlot')}</span>
            <button
              type="button"
              onClick={handleRefresh}
              className="flex items-center gap-1 px-2 py-0.5 text-xs rounded border border-bambu-gray/30 hover:border-bambu-gray hover:bg-bambu-dark-tertiary transition-colors text-bambu-gray hover:text-white"
              disabled={isRefreshing}
            >
              <RefreshCw className={`w-3 h-3 ${isRefreshing ? 'animate-spin' : ''}`} />
              <span>{t('printModal.reRead')}</span>
            </button>
          </div>
          {sameInletWarning && (
            <div className="flex items-start gap-1.5 rounded border border-yellow-500/40 bg-yellow-500/10 px-2 py-1.5 text-xs text-yellow-700 dark:text-yellow-400">
              <AlertTriangle className="w-3 h-3 mt-0.5 shrink-0" />
              <span>{t('printModal.ftsSameInletHint', { inlet: sameInletWarning })}</span>
            </div>
          )}
          {filamentComparison.map((item, idx) => {
            // Per-slot strict colour is part of the routing policy, not a pin.
            // Show its control only when the caller can persist that choice.
            const slotId = item.slot_id ?? 0;
            const canForceMatch = slotId > 0 && onForceColorMatchChange != null;
            // #1718: sub-brand + colour resolution via the shared hook.
            // Indexing is safe because ``useFilamentLabels`` mirrors the input
            // array shape; defensive fallback covers the empty-reqs render path
            // that shouldn't reach here anyway.
            const { resolvedName, colorLabel } = filamentLabels[idx] ?? { resolvedName: item.type, colorLabel: getColorName(item.color) };
            return (
            <div key={idx} className="space-y-1">
              <div
                className="grid items-center gap-2 text-xs"
                style={{ gridTemplateColumns: '16px minmax(70px, 1fr) auto 2fr 16px' }}
              >
                {/* Required color */}
                <span title={t('printModal.requiredFilament', { type: resolvedName, color: colorLabel })}>
                  <Circle className="w-3 h-3" fill={item.color} stroke={item.color} />
                </span>
                {/* Required type + grams + nozzle badge. Only the name truncates:
                    the grams answer "does the spool have enough left?", so they
                    are the last thing that should be dropped, and sharing one
                    truncating span meant a long name pushed them off the end —
                    partly on a wide screen, entirely in mobile portrait (#2669). */}
                <span className="text-white flex items-center gap-1 min-w-0">
                  {isDualNozzle && item.nozzle_id != null && (
                    <span
                      className="inline-flex items-center justify-center w-3.5 h-3.5 rounded text-[9px] font-bold leading-none bg-bambu-gray/20 text-bambu-gray shrink-0"
                      title={item.nozzle_id === 1 ? t('printModal.leftNozzleTooltip') : t('printModal.rightNozzleTooltip')}
                    >
                      {item.nozzle_id === 1 ? t('printModal.leftNozzle') : t('printModal.rightNozzle')}
                    </span>
                  )}
                  <span className="truncate min-w-0" title={resolvedName}>{resolvedName}</span>
                  <span className="text-bambu-gray shrink-0 whitespace-nowrap">({item.used_grams}g)</span>
                </span>
                {/* Arrow */}
                <span className="text-bambu-gray">→</span>
                {/* Slot selector dropdown */}
                <select
                  value={item.loaded?.globalTrayId ?? ''}
                  onChange={(e) => handleSlotChange(slotId, e.target.value)}
                  className={`flex-1 px-2 py-1 rounded border text-xs bg-bambu-dark-secondary focus:outline-none focus:ring-1 focus:ring-bambu-green ${
                    item.status === 'match'
                      ? 'border-bambu-green/50 text-bambu-green'
                      : item.status === 'type_only'
                      ? 'border-yellow-500 dark:border-yellow-400/50 text-yellow-700 dark:text-yellow-400'
                      : 'border-orange-500 dark:border-orange-400/50 text-orange-700 dark:text-orange-400'
                  } ${item.isManual ? 'ring-1 ring-blue-400/50' : ''}`}
                  title={item.isManual ? t('printModal.manuallySelected') : t('printModal.autoMatched')}
                >
                  <option value="" className="bg-bambu-dark text-bambu-gray">
                    {t('printModal.selectSlot')}
                  </option>
                  {/*
                    #1722: every loaded slot is offered for every filament row,
                    regardless of which extruder the slot is wired to. Before this
                    a slot was only listed when its extruder matched the filament's
                    slicer-assigned nozzle (item.nozzle_id), which locked users out
                    of cross-extruder picks even when they'd intentionally loaded
                    the required filament into the "other" AMS (H2D: AMS A+C left,
                    B right). The L/R badge on the row still shows the slicer's
                    intent; the dropdown now trusts the user's physical setup.
                    Printer firmware accepts/rejects the ams_mapping at start-print
                    — failure is loud, not silent.
                  */}
                  {loadedFilaments
                    // An old saved external pin remains visible so it can be
                    // corrected, but external is never newly selectable while
                    // FTS is connected.
                    .filter((f) => !ftsInstalled || !f.isExternal || f.globalTrayId === item.loaded?.globalTrayId)
                    .map((f) => {
                      const remainingWeight = trayRemainingWeightMap.get(f.globalTrayId);
                      const remainingLabel = remainingWeight != null
                        ? t('printModal.slotRemainingShort', {
                            grams: remainingWeight,
                            defaultValue: ` - ${remainingWeight}g left`,
                          })
                        : '';
                      // FTS badge: which switch inlet this slot's AMS feeds. Not a
                      // nozzle — the slot reaches both through the switch — but it
                      // decides whether a change to the next filament is the fast
                      // cross-inlet one or the slow same-inlet one. The inlet's own
                      // name, as on the switch — never L / R, which would read as a
                      // nozzle. Not translated: IN-A / IN-B are printed on the machine.
                      const ftsInlet = f.isExternal ? null : ftsInletForAms(f.amsId);
                      const ftsBadge = ftsInlet == null ? '' : ` [IN-${ftsInlet}]`;
                      return (
                        <option
                          key={f.globalTrayId}
                          value={f.globalTrayId}
                          disabled={ftsInstalled && f.isExternal}
                          className="bg-bambu-dark text-white"
                        >
                          {f.label}: {f.traySubBrands || f.type} ({f.colorName}){remainingLabel}{ftsBadge}
                          {ftsInstalled && f.isExternal ? ` — ${t('filamentRouting.feasibility.reason.fts_external_unsupported')}` : ''}
                        </option>
                      );
                  })}
                </select>
                {/* Status icon */}
                {item.status === 'match' ? (
                  <Check className="w-3 h-3 text-bambu-green" />
                ) : item.status === 'type_only' ? (
                  <span title={t('printModal.sameTypeDifferentColor')}>
                    <AlertTriangle className="w-3 h-3 text-yellow-600 dark:text-yellow-400" />
                  </span>
                ) : (
                  <span title={routingReason || t(profileMismatch ? 'filamentRouting.feasibility.reason.variant_mismatch' : requireExactColor && rawColorMismatch ? 'printModal.filamentColorMismatch' : 'printModal.filamentTypeNotLoaded')}>
                    <AlertTriangle className="w-3 h-3 text-orange-600 dark:text-orange-400" />
                  </span>
                )}
              </div>
              {/* Force Color Match checkbox. */}
              {canForceMatch && (
                <label className="inline-flex items-center gap-1.5 text-xs text-bambu-gray cursor-pointer select-none pl-5">
                  <input
                    type="checkbox"
                    checked={forceColorMatch?.[slotId] ?? false}
                    onChange={(e) => onForceColorMatchChange(slotId, e.target.checked)}
                    className="accent-bambu-green w-3 h-3"
                  />
                  <Palette className="w-3 h-3" />
                  {t('printModal.forceColorMatch')}
                </label>
              )}
            </div>
            );
          })}
          <div className="text-xs text-bambu-gray">
            {t('printModal.totalCost')}{' '}
            <span className="text-white">
              {totalCost > 0 || hasAnyCost ? `${currencySymbol}${totalCost.toFixed(2)}` : 'N/A'}
            </span>
          </div>
          {(routingReason || hasTypeMismatch) && (
            <p className="text-xs text-orange-700 dark:text-orange-400 mt-2">
              {routingReason || t(profileMismatch ? 'filamentRouting.feasibility.reason.variant_mismatch'
                : requireExactColor && rawColorMismatch ? 'printModal.filamentColorMismatch' : 'printModal.filamentTypeMismatch')}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
