import { useMemo } from 'react';
import { useQueries, useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import type { PrinterStatus, Printer } from '../api/client';
import {
  buildLoadedFilaments,
  buildFilamentComparison,
  buildAmsMapping,
  computeAmsMapping,
  type LoadedFilament,
  type FilamentRequirement,
} from './useFilamentMapping';

/**
 * Match status for a single printer's filament configuration.
 */
export type PrinterMatchStatus = 'full' | 'partial' | 'missing';

/**
 * Per-printer configuration for AMS mapping.
 */
export interface PerPrinterConfig {
  /** Whether this printer uses the default mapping or has custom config */
  useDefault: boolean;
  /** Manual slot overrides for this printer (slot_id -> globalTrayId) */
  manualMappings: Record<number, number>;
  /** Whether this mapping was auto-configured */
  autoConfigured: boolean;
}

/**
 * Result of filament mapping for a single printer.
 */
export interface PrinterMappingResult {
  printerId: number;
  printerName: string;
  /** Printer status data */
  status: PrinterStatus | undefined;
  /** Whether status is still loading */
  isLoading: boolean;
  /** List of loaded filaments in this printer */
  loadedFilaments: LoadedFilament[];
  /** Auto-computed AMS mapping for this printer */
  autoMapping: number[] | undefined;
  /** Final AMS mapping (considering manual overrides) */
  finalMapping: number[] | undefined;
  /** Match status: full (all exact), partial (some mismatches), missing (type not found) */
  matchStatus: PrinterMatchStatus;
  /** Authoritative full-assignment refusal, when supplied by the dialog. */
  routingReason?: string;
  /** Number of slots with exact match (type + color) */
  exactMatches: number;
  /** Number of slots with type-only match */
  typeOnlyMatches: number;
  /** Number of slots with missing type */
  missingTypes: number;
  /** Total required slots */
  totalSlots: number;
  /** Per-printer config */
  config: PerPrinterConfig;
}

/**
 * Result of the useMultiPrinterFilamentMapping hook.
 */
export interface UseMultiPrinterFilamentMappingResult {
  /** Results for each selected printer */
  printerResults: PrinterMappingResult[];
  /** Whether any printer data is still loading */
  isLoading: boolean;
  /** Per-printer configurations */
  perPrinterConfigs: Record<number, PerPrinterConfig>;
  /** Update config for a specific printer */
  updatePrinterConfig: (printerId: number, config: Partial<PerPrinterConfig>) => void;
  /** Auto-configure all printers based on their loaded filaments */
  autoConfigureAll: () => void;
  /** Auto-configure a specific printer */
  autoConfigurePrinter: (printerId: number) => void;
  /** Get final mapping for a specific printer (for submission) */
  getFinalMapping: (printerId: number) => number[] | undefined;
  /** Check if all printers have acceptable mappings */
  allPrintersReady: boolean;
}

/**
 * Compute match details for a printer given filament requirements and loaded filaments.
 */
function computeMatchDetails(
  filamentReqs: FilamentRequirement[] | undefined, loadedFilaments: LoadedFilament[],
  manualMappings: Record<number, number>, trayNow: number | null | undefined,
  preferLowest = false, ftsActive = false,
): { exactMatches: number; typeOnlyMatches: number; missingTypes: number; totalSlots: number; status: PrinterMatchStatus } {
  const rows = buildFilamentComparison({ filaments: filamentReqs ?? [] }, loadedFilaments,
    manualMappings, ftsActive, trayNow ?? undefined, preferLowest);
  const exactMatches = rows.filter(row => row.hasFilament && row.typeMatch && row.colorMatch).length;
  const typeOnlyMatches = rows.filter(row => row.hasFilament && row.typeMatch && !row.colorMatch).length;
  const missingTypes = rows.length - exactMatches - typeOnlyMatches;
  return { exactMatches, typeOnlyMatches, missingTypes, totalSlots: rows.length,
    status: missingTypes ? 'missing' : typeOnlyMatches ? 'partial' : 'full' };
}

function computeMappingWithOverrides(
  filamentReqs: { filaments: FilamentRequirement[] } | undefined,
  printerStatus: PrinterStatus | undefined, manualMappings: Record<number, number>, preferLowest = false,
): number[] | undefined {
  return buildAmsMapping(buildFilamentComparison(filamentReqs, buildLoadedFilaments(printerStatus),
    manualMappings, printerStatus?.fila_switch?.installed === true, printerStatus?.tray_now, preferLowest));
}

/**
 * Default per-printer config (use default mapping).
 */
const DEFAULT_PRINTER_CONFIG: PerPrinterConfig = {
  useDefault: true,
  manualMappings: {},
  autoConfigured: false,
};

/**
 * Hook to manage filament mapping for multiple printers.
 * Fetches printer status for all selected printers and computes per-printer mappings.
 */
export function useMultiPrinterFilamentMapping(
  selectedPrinterIds: number[],
  printers: Printer[] | undefined,
  filamentReqs: { filaments: FilamentRequirement[] } | undefined,
  defaultMappings: Record<number, number>,
  perPrinterConfigs: Record<number, PerPrinterConfig>,
  setPerPrinterConfigs: React.Dispatch<React.SetStateAction<Record<number, PerPrinterConfig>>>
): UseMultiPrinterFilamentMappingResult {
  // Local provisional display uses the same preference. The dialog replaces it
  // with the server assignment, which also has inventory remaining weights.
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings });
  const preferLowest = settings?.prefer_lowest_filament ?? true;

  // Fetch printer status for all selected printers in parallel
  const statusQueries = useQueries({
    queries: selectedPrinterIds.map((printerId) => ({
      queryKey: ['printer-status', printerId],
      queryFn: () => api.getPrinterStatus(printerId),
      enabled: selectedPrinterIds.length > 0,
      staleTime: 5000, // Consider data fresh for 5 seconds
    })),
  });

  // Build results for each printer
  const printerResults = useMemo((): PrinterMappingResult[] => {
    return selectedPrinterIds.map((printerId, index) => {
      const query = statusQueries[index];
      const printerStatus = query?.data;
      const printer = printers?.find((p) => p.id === printerId);
      const printerName = printer?.name || `Printer ${printerId}`;

      const loadedFilaments = buildLoadedFilaments(printerStatus);
      const config = perPrinterConfigs[printerId] || DEFAULT_PRINTER_CONFIG;

      // Compute auto mapping for this printer
      const autoMapping = computeAmsMapping(filamentReqs, printerStatus);

      // Determine which mappings to use:
      // If printer has override (useDefault=false), use its custom mappings
      // Otherwise use the default mappings
      const effectiveMappings = !config.useDefault
        ? config.manualMappings
        : defaultMappings;

      // Compute final mapping with overrides
      const finalMapping = computeMappingWithOverrides(
        filamentReqs,
        printerStatus,
        effectiveMappings,
        preferLowest,
      );

      // Compute match details
      const matchDetails = computeMatchDetails(
        filamentReqs?.filaments,
        loadedFilaments,
        effectiveMappings,
        printerStatus?.tray_now,
        preferLowest,
        printerStatus?.fila_switch?.installed === true,
      );

      return {
        printerId,
        printerName,
        status: printerStatus,
        isLoading: query?.isLoading ?? false,
        loadedFilaments,
        autoMapping,
        finalMapping,
        matchStatus: matchDetails.status,
        exactMatches: matchDetails.exactMatches,
        typeOnlyMatches: matchDetails.typeOnlyMatches,
        missingTypes: matchDetails.missingTypes,
        totalSlots: matchDetails.totalSlots,
        config,
      };
    });
  }, [selectedPrinterIds, statusQueries, printers, filamentReqs, perPrinterConfigs, defaultMappings, preferLowest]);

  const isLoading = statusQueries.some((q) => q.isLoading);

  // Update config for a specific printer
  const updatePrinterConfig = (printerId: number, updates: Partial<PerPrinterConfig>) => {
    setPerPrinterConfigs((prev) => ({
      ...prev,
      [printerId]: {
        ...(prev[printerId] || DEFAULT_PRINTER_CONFIG),
        ...updates,
      },
    }));
  };

  // Auto-configure a specific printer based on its loaded filaments
  const autoConfigurePrinter = (printerId: number) => {
    const result = printerResults.find((r) => r.printerId === printerId);
    if (!result || !result.status || !filamentReqs?.filaments) return;

    // Compute optimal mapping for this printer
    const autoMapping = computeAmsMapping(filamentReqs, result.status);
    if (!autoMapping) return;

    // Convert autoMapping array to manualMappings record
    const manualMappings: Record<number, number> = {};
    autoMapping.forEach((globalTrayId, index) => {
      if (globalTrayId !== -1) {
        manualMappings[index + 1] = globalTrayId;
      }
    });

    updatePrinterConfig(printerId, {
      useDefault: false,
      manualMappings,
      autoConfigured: true,
    });
  };

  // Auto-configure all printers
  const autoConfigureAll = () => {
    for (const printerId of selectedPrinterIds) {
      autoConfigurePrinter(printerId);
    }
  };

  // Get final mapping for a specific printer (for submission)
  const getFinalMapping = (printerId: number): number[] | undefined => {
    const result = printerResults.find((r) => r.printerId === printerId);
    return result?.finalMapping;
  };

  // Check if all printers have acceptable mappings (no missing types)
  const allPrintersReady = printerResults.every((r) => r.matchStatus !== 'missing');

  return {
    printerResults,
    isLoading,
    perPrinterConfigs,
    updatePrinterConfig,
    autoConfigureAll,
    autoConfigurePrinter,
    getFinalMapping,
    allPrintersReady,
  };
}
