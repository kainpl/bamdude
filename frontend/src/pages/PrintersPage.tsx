import { useState, useEffect, useLayoutEffect, useMemo, useRef, useCallback } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useTheme } from '../contexts/ThemeContext';
import { useAuth } from '../contexts/AuthContext';
import {
  Plus,
  Link,
  Unlink,
  Signal,
  Clock,
  MoreVertical,
  Trash2,
  RefreshCw,
  Box,
  HardDrive,
  DoorOpen,
  DoorClosed,
  AlertTriangle,
  AlertCircle,
  Terminal,
  Power,
  PowerOff,
  Zap,
  Wrench,
  ChevronDown,
  Filter,
  MoreHorizontal,
  SlidersHorizontal,
  Pencil,
  ArrowUpNarrowWide,
  ArrowDownWideNarrow,
  ArrowUp,
  ArrowDown,
  MoveVertical,
  Layers,
  Video,
  Search,
  Loader2,
  Square,
  CheckSquare,
  Maximize2,
  Pause,
  Play,
  X,
  Fan,
  Wind,
  AirVent,
  Download,
  ScanSearch,
  CheckCircle,
  XCircle,
  User,
  Home,
  Printer as PrinterIcon,
  Info,
  Cable,
  Flame,
  Gauge,
  ArrowLeftRight,
  ArrowDownToLine,
  ArrowUpFromLine,
  Eye,
  EyeOff,
} from 'lucide-react';

import { Link as RouterLink, useNavigate } from 'react-router-dom';
import { api, discoveryApi, firmwareApi, macrosApi, withStreamToken } from '../api/client';
import { BulkPrinterToolbar } from '../components/BulkPrinterToolbar';
import { PauseChip } from '../components/PauseChip';
import { formatDateOnly, formatETA, formatDuration } from '../utils/date';
import type { Printer, PrinterCreate, PrinterStatus, AMSUnit, DiscoveredPrinter, FirmwareUpdateInfo, FirmwareUploadStatus, LinkedSpoolInfo, SpoolAssignment, HMSError, Macro, InventorySpool, SmartPlug } from '../api/client';

// Source of truth for Spoolman ↔ AMS slot binding (upstream PR #1241).
// Mirrors backend `spoolman_slot_assignments` rows; PrintersPage subscribes to
// the bulk endpoint and drops a row into PrinterCard's prop bag.
export interface SpoolmanSlotAssignmentRow {
  printer_id: number;
  ams_id: number;
  tray_id: number;
  spoolman_spool_id: number;
}
import { Card, CardContent } from '../components/Card';
import { Button } from '../components/Button';
import { ConfirmModal } from '../components/ConfirmModal';
import { FileManagerModal } from '../components/FileManagerModal';
import { EmbeddedCameraViewer } from '../components/EmbeddedCameraViewer';
import { MQTTDebugModal } from '../components/MQTTDebugModal';
import { CalibrationModal } from '../components/CalibrationModal';
import { HMSErrorModal, filterKnownHMSErrors } from '../components/HMSErrorModal';
import { PrinterQueueWidget } from '../components/PrinterQueueWidget';
import { AMSHistoryModal } from '../components/AMSHistoryModal';
import { FilamentHoverCard, EmptySlotHoverCard } from '../components/FilamentHoverCard';
import { LinkSpoolModal } from '../components/LinkSpoolModal';
import { AssignSpoolModal } from '../components/AssignSpoolModal';
import { ConfigureAmsSlotModal } from '../components/ConfigureAmsSlotModal';
import { useToast } from '../contexts/ToastContext';
import { ChamberLight } from '../components/icons/ChamberLight';
import { PlateClearedIcon } from '../components/icons/PlateClearedIcon';
import { SkipObjectsModal, SkipObjectsIcon } from '../components/SkipObjectsModal';
import { FileUploadModal } from '../components/FileUploadModal';
import { PrintModal } from '../components/PrintModal';
import { PrinterInfoModal } from '../components/PrinterInfoModal';
import { getGlobalTrayId, getFillBarColor, getSpoolmanFillLevel, getFallbackSpoolTag, isBambuLabSpool } from '../utils/amsHelpers';
import { getPrinterImage, getWifiStrength, hasDoorSensor, mapModelCode } from '../utils/printer';
import { formatPrintName } from '../utils/printName';
import { compareFwVersions } from '../utils/firmwareVersion';
import { FilamentSlotCircle } from '../components/FilamentSlotCircle';
import { getColorName, parseFilamentColor, isLightColor } from '../utils/colors';
import { formatSpoolDisplayName, DEFAULT_SPOOL_DISPLAY_TEMPLATE } from '../utils/spoolName';

// Color names resolve via getColorName() which reads the backend color_catalog
// (loaded once at app startup by ColorCatalogProvider). Hardcoded hex/code tables
// were removed in 0.3.2 - they were structurally guaranteed to produce wrong
// names for any color the hand-maintained list didn't cover (upstream #857).


// formatPrintName extracted to utils/printName.ts so PrintersPage.tsx can
// keep react-refresh/only-export-components happy when the helper is imported
// by tests — upstream move for upstream #881 / #730 follow-ups.

// Format K value with 3 decimal places, default to 0.020 if null
function formatKValue(k: number | null | undefined): string {
  const value = k ?? 0.020;
  return value.toFixed(3);
}

// Nozzle side indicators (Bambu Lab style - square badge with L/R)
function NozzleBadge({ side }: { side: 'L' | 'R' }) {
  const { mode } = useTheme();
  // Light mode: #e7f5e9 (light green), Dark mode: #1a4d2e (dark green)
  const bgColor = mode === 'dark' ? '#1a4d2e' : '#e7f5e9';
  return (
    <span
      className="inline-flex items-center justify-center w-4 h-4 text-[10px] font-bold rounded"
      style={{ backgroundColor: bgColor, color: '#00ae42' }}
    >
      {side}
    </span>
  );
}

// Expand nozzle type codes to material names
// Handles full text ("hardened_steel"), 2-char codes ("HS"/"HH"), and 4-char codes ("HS01")
// Material mapping: 00=stainless steel, 01=hardened steel, 05=tungsten carbide
function nozzleTypeName(type: string, t: (key: string) => string): string {
  if (!type) return '';
  // Full text names (from main nozzle info)
  if (type.includes('hardened')) return t('printers.nozzleHardenedSteel');
  if (type.includes('stainless')) return t('printers.nozzleStainlessSteel');
  if (type.includes('tungsten')) return t('printers.nozzleTungstenCarbide');
  // 4-char codes (e.g. "HS01"): last 2 digits = material
  if (type.length >= 4) {
    const material = type.slice(2, 4);
    if (material === '00') return t('printers.nozzleStainlessSteel');
    if (material === '01') return t('printers.nozzleHardenedSteel');
    if (material === '05') return t('printers.nozzleTungstenCarbide');
  }
  // 2-digit numeric codes
  if (type === '00') return t('printers.nozzleStainlessSteel');
  if (type === '01') return t('printers.nozzleHardenedSteel');
  if (type === '05') return t('printers.nozzleTungstenCarbide');
  // 2-char alpha codes: H prefix = hardened steel
  if (type.startsWith('H')) return t('printers.nozzleHardenedSteel');
  return type;
}

// Parse flow type from nozzle type code
// HH = high flow, HS = standard/normal
function nozzleFlowName(type: string, t: (key: string) => string): string {
  if (!type) return '';
  if (type.startsWith('HH')) return t('printers.nozzleHighFlow');
  if (type.startsWith('HS')) return t('printers.nozzleStandardFlow');
  return '';
}

// Per-slot hover card for nozzle rack
// activeStatus: when true, show "Active" instead of "Mounted"/"Docked" (for hotend nozzles)
function NozzleSlotHoverCard({ slot, index, activeStatus, filamentName, children }: {
  slot: import('../api/client').NozzleRackSlot;
  index: number;
  activeStatus?: boolean;
  filamentName?: string;
  children: React.ReactNode;
}) {
  const { t } = useTranslation();
  const [isVisible, setIsVisible] = useState(false);
  const [position, setPosition] = useState<'top' | 'bottom'>('top');
  const triggerRef = useRef<HTMLDivElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const isEmpty = !slot.nozzle_diameter && !slot.nozzle_type;
  const isMounted = slot.stat === 1;

  useEffect(() => {
    if (isVisible && triggerRef.current && cardRef.current) {
      const triggerRect = triggerRef.current.getBoundingClientRect();
      const cardHeight = cardRef.current.offsetHeight;
      const headerHeight = 56;
      const spaceAbove = triggerRect.top - headerHeight;
      const spaceBelow = window.innerHeight - triggerRect.bottom;
      if (spaceAbove < cardHeight + 12 && spaceBelow > spaceAbove) {
        setPosition('bottom');
      } else {
        setPosition('top');
      }
    }
  }, [isVisible]);

  const handleMouseEnter = () => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(() => setIsVisible(true), 80);
  };

  const handleMouseLeave = () => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(() => setIsVisible(false), 100);
  };

  useEffect(() => {
    return () => {
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
    };
  }, []);

  const filamentCss = parseFilamentColor(slot.filament_color);
  const typeFull = nozzleTypeName(slot.nozzle_type, t);
  const flowFull = nozzleFlowName(slot.nozzle_type, t);

  return (
    <div
      ref={triggerRef}
      className="relative"
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
    >
      {children}

      {isVisible && (
        <div
          ref={cardRef}
          className={`
            absolute left-1/2 -translate-x-1/2 z-50
            ${position === 'top' ? 'bottom-full mb-2' : 'top-full mt-2'}
            animate-in fade-in-0 zoom-in-95 duration-150
          `}
          style={{ maxWidth: 'calc(100vw - 24px)' }}
        >
          <div className="w-44 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl overflow-hidden backdrop-blur-sm">
            {isEmpty ? (
              <div className="px-3 py-2 text-xs text-bambu-gray text-center whitespace-nowrap">
                Slot {index + 1} - Empty
              </div>
            ) : (
              <div className="p-2.5 space-y-1.5">
                {/* Diameter */}
                <div className="flex items-center justify-between">
                  <span className="text-[10px] uppercase tracking-wider text-bambu-gray font-medium">{t('printers.nozzleDiameter')}</span>
                  <span className="text-xs text-white font-semibold">{slot.nozzle_diameter} mm</span>
                </div>

                {/* Type */}
                {typeFull && (
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] uppercase tracking-wider text-bambu-gray font-medium">{t('printers.nozzleType')}</span>
                    <span className="text-xs text-white font-semibold truncate max-w-[100px]">{typeFull}</span>
                  </div>
                )}

                {/* Flow (hide if empty) */}
                {flowFull && (
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] uppercase tracking-wider text-bambu-gray font-medium">{t('printers.nozzleFlow')}</span>
                    <span className="text-xs text-white font-semibold">{flowFull}</span>
                  </div>
                )}

                {/* Status badge */}
                <div className="flex items-center justify-between">
                  <span className="text-[10px] uppercase tracking-wider text-bambu-gray font-medium">{t('printers.nozzleStatus')}</span>
                  <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${
                    activeStatus || isMounted
                      ? 'bg-green-900/50 text-green-400'
                      : 'bg-bambu-dark-tertiary text-bambu-gray'
                  }`}>
                    {activeStatus ? t('printers.nozzleActive') : isMounted ? t('printers.nozzleMounted') : t('printers.nozzleDocked')}
                  </span>
                </div>

                {/* Wear (hide if null) */}
                {slot.wear != null && (
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] uppercase tracking-wider text-bambu-gray font-medium">{t('printers.nozzleWear')}</span>
                    <span className="text-xs text-white font-semibold">{slot.wear}%</span>
                  </div>
                )}

                {/* Max Temp (hide if 0) */}
                {slot.max_temp > 0 && (
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] uppercase tracking-wider text-bambu-gray font-medium">{t('printers.nozzleMaxTemp')}</span>
                    <span className="text-xs text-white font-semibold">{slot.max_temp}°C</span>
                  </div>
                )}

                {/* Serial (hide if empty) */}
                {slot.serial_number && (
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] uppercase tracking-wider text-bambu-gray font-medium">{t('printers.nozzleSerial')}</span>
                    <span className="text-[10px] text-white font-mono truncate max-w-[80px]">{slot.serial_number}</span>
                  </div>
                )}

                {/* Filament: material type + color swatch (hide if no color) */}
                {(filamentCss || slot.filament_type) && (
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] uppercase tracking-wider text-bambu-gray font-medium">{t('printers.nozzleFilament')}</span>
                    <div className="flex items-center gap-1">
                      {filamentCss && (
                        <div className="w-3 h-3 rounded-sm border border-white/20" style={{ backgroundColor: filamentCss }} />
                      )}
                      <span className="text-[10px] text-white font-semibold truncate max-w-[100px]">{filamentName || slot.filament_type || slot.filament_id || ''}</span>
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Arrow pointer */}
          <div
            className={`
              absolute left-1/2 -translate-x-1/2 w-0 h-0
              border-l-[6px] border-l-transparent
              border-r-[6px] border-r-transparent
              ${position === 'top'
                ? 'top-full border-t-[6px] border-t-bambu-dark-tertiary'
                : 'bottom-full border-b-[6px] border-b-bambu-dark-tertiary'}
            `}
          />
        </div>
      )}
    </div>
  );
}

// Dual-nozzle hover card showing L and R nozzle details side by side
function DualNozzleHoverCard({ leftSlot, rightSlot, activeNozzle, filamentInfo, children }: {
  leftSlot?: import('../api/client').NozzleRackSlot;
  rightSlot?: import('../api/client').NozzleRackSlot;
  activeNozzle: 'L' | 'R';
  filamentInfo?: Record<string, { name: string; k: number | null }>;
  children: React.ReactNode;
}) {
  const { t } = useTranslation();
  const [isVisible, setIsVisible] = useState(false);
  const [position, setPosition] = useState<'top' | 'bottom'>('top');
  const triggerRef = useRef<HTMLDivElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (isVisible && triggerRef.current && cardRef.current) {
      const triggerRect = triggerRef.current.getBoundingClientRect();
      const cardHeight = cardRef.current.offsetHeight;
      const headerHeight = 56;
      const spaceAbove = triggerRect.top - headerHeight;
      const spaceBelow = window.innerHeight - triggerRect.bottom;
      if (spaceAbove < cardHeight + 12 && spaceBelow > spaceAbove) {
        setPosition('bottom');
      } else {
        setPosition('top');
      }
    }
  }, [isVisible]);

  const handleMouseEnter = () => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(() => setIsVisible(true), 80);
  };

  const handleMouseLeave = () => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(() => setIsVisible(false), 100);
  };

  useEffect(() => {
    return () => { if (timeoutRef.current) clearTimeout(timeoutRef.current); };
  }, []);

  if (!leftSlot && !rightSlot) return <>{children}</>;

  const renderColumn = (slot: import('../api/client').NozzleRackSlot, side: 'L' | 'R') => {
    const isActive = activeNozzle === side;
    const typeFull = nozzleTypeName(slot.nozzle_type, t);
    const flowFull = nozzleFlowName(slot.nozzle_type, t);
    const filamentCss = parseFilamentColor(slot.filament_color);
    const filamentName = slot.filament_id ? filamentInfo?.[slot.filament_id]?.name : undefined;
    return (
      <div className="flex-1 space-y-1.5">
        <div className={`text-[10px] font-bold pb-1 border-b border-bambu-dark-tertiary/50 ${isActive ? 'text-amber-400' : 'text-bambu-gray'}`}>
          {side === 'L' ? t('common.left') : t('common.right')}
        </div>
        {slot.nozzle_diameter && (
          <div className="flex items-center justify-between">
            <span className="text-[10px] text-bambu-gray">{t('printers.nozzleDiameter')}</span>
            <span className="text-xs text-white font-semibold">{slot.nozzle_diameter} mm</span>
          </div>
        )}
        {typeFull && (
          <div className="flex items-center justify-between">
            <span className="text-[10px] text-bambu-gray">{t('printers.nozzleType')}</span>
            <span className="text-[10px] text-white font-semibold">{typeFull}</span>
          </div>
        )}
        {flowFull && (
          <div className="flex items-center justify-between">
            <span className="text-[10px] text-bambu-gray">{t('printers.nozzleFlow')}</span>
            <span className="text-[10px] text-white font-semibold">{flowFull}</span>
          </div>
        )}
        <div className="flex items-center justify-between">
          <span className="text-[10px] text-bambu-gray">{t('printers.nozzleStatus')}</span>
          <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${
            isActive
              ? 'bg-green-900/50 text-green-400'
              : 'bg-bambu-dark-tertiary text-bambu-gray'
          }`}>
            {isActive ? t('printers.nozzleActive') : t('printers.nozzleIdle')}
          </span>
        </div>
        {slot.wear != null && (
          <div className="flex items-center justify-between">
            <span className="text-[10px] text-bambu-gray">{t('printers.nozzleWear')}</span>
            <span className="text-xs text-white font-semibold">{slot.wear}%</span>
          </div>
        )}
        {/* Serial and max temp only available on the right (removable) nozzle */}
        {side === 'R' && slot.max_temp > 0 && (
          <div className="flex items-center justify-between">
            <span className="text-[10px] text-bambu-gray">{t('printers.nozzleMaxTemp')}</span>
            <span className="text-xs text-white font-semibold">{slot.max_temp}°C</span>
          </div>
        )}
        {side === 'R' && slot.serial_number && (
          <div className="flex items-center justify-between">
            <span className="text-[10px] text-bambu-gray">{t('printers.nozzleSerial')}</span>
            <span className="text-[10px] text-white font-mono">{slot.serial_number}</span>
          </div>
        )}
        {(filamentCss || slot.filament_type || slot.filament_id) && (
          <div className="flex items-center justify-between">
            <span className="text-[10px] text-bambu-gray">{t('printers.nozzleFilament')}</span>
            <div className="flex items-center gap-1">
              {filamentCss && (
                <div className="w-3 h-3 rounded-sm border border-white/20" style={{ backgroundColor: filamentCss }} />
              )}
              <span className="text-[10px] text-white font-semibold truncate max-w-[100px]">
                {filamentName || slot.filament_type || slot.filament_id || ''}
              </span>
            </div>
          </div>
        )}
      </div>
    );
  };

  return (
    <div
      ref={triggerRef}
      className="relative flex-1"
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
    >
      {children}

      {isVisible && (
        <div
          ref={cardRef}
          className={`
            absolute left-1/2 -translate-x-1/2 z-50
            ${position === 'top' ? 'bottom-full mb-2' : 'top-full mt-2'}
            animate-in fade-in-0 zoom-in-95 duration-150
          `}
          style={{ maxWidth: 'calc(100vw - 24px)' }}
        >
          <div className="w-96 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl overflow-hidden backdrop-blur-sm">
            <div className="p-2.5 flex gap-3">
              {leftSlot && renderColumn(leftSlot, 'L')}
              {leftSlot && rightSlot && <div className="w-px bg-bambu-dark-tertiary/50" />}
              {rightSlot && renderColumn(rightSlot, 'R')}
            </div>
          </div>

          {/* Arrow pointer */}
          <div
            className={`
              absolute left-1/2 -translate-x-1/2 w-0 h-0
              border-l-[6px] border-l-transparent
              border-r-[6px] border-r-transparent
              ${position === 'top'
                ? 'top-full border-t-[6px] border-t-bambu-dark-tertiary'
                : 'bottom-full border-b-[6px] border-b-bambu-dark-tertiary'}
            `}
          />
        </div>
      )}
    </div>
  );
}

// H2C Nozzle Rack Card - compact single row showing 6-position tool-changer dock
function NozzleRackCard({ slots, filamentInfo }: { slots: import('../api/client').NozzleRackSlot[]; filamentInfo?: Record<string, { name: string; k: number | null }> }) {
  const { t } = useTranslation();
  // Rack nozzles only (IDs >= 2) - excludes L/R hotend nozzles (IDs 0, 1).
  // H2C rack slot IDs are fixed at 16..21. When a nozzle is picked up into the
  // hotend the firmware omits that rack ID entirely, so we must map by the fixed
  // base - computing it from min(present IDs) shifts everything left when slot 16
  // is the one currently mounted (upstream #943).
  const rackNozzles = slots.filter(s => s.id >= 2);
  const RACK_SIZE = 6;
  const RACK_BASE_ID = 16;
  const rackSlots: (import('../api/client').NozzleRackSlot)[] = Array.from(
    { length: RACK_SIZE },
    (_, i) => rackNozzles.find(s => s.id === RACK_BASE_ID + i) ?? {
      id: -(i + 1), nozzle_type: '', nozzle_diameter: '', wear: null, stat: null,
      max_temp: 0, serial_number: '', filament_color: '', filament_id: '', filament_type: '',
    },
  );

  return (
    <div className="text-center px-2.5 py-1.5 bg-bambu-dark rounded-lg flex-[2_1_190px] flex flex-col justify-center">
      <p className="text-[9px] text-bambu-gray mb-1">{t('printers.nozzleRack')}</p>
      <div className="flex gap-[3px] justify-center">
        {rackSlots.map((slot, i) => {
          const isEmpty = !slot.nozzle_diameter && !slot.nozzle_type;
          const filamentBg = !isEmpty ? parseFilamentColor(slot.filament_color) : null;
          const lightBg = filamentBg ? isLightColor(slot.filament_color) : false;

          return (
            <NozzleSlotHoverCard key={slot.id >= 0 ? slot.id : `empty-${i}`} slot={slot} index={i} filamentName={slot.filament_id ? filamentInfo?.[slot.filament_id]?.name : undefined}>
              <div
                className={`w-7 h-7 rounded flex items-center justify-center cursor-default transition-colors border-b-2 ${
                  isEmpty
                    ? 'bg-bambu-dark-tertiary/20 border-bambu-dark-tertiary/20'
                    : 'bg-bambu-dark-tertiary/40 border-bambu-dark-tertiary/40'
                }`}
                style={filamentBg ? { backgroundColor: filamentBg } : undefined}
              >
                <span className={`text-[10px] font-semibold ${isEmpty ? 'text-bambu-gray/30' : lightBg ? 'text-black/80' : 'text-white'}`}
                      style={filamentBg && !lightBg ? { textShadow: '0 1px 3px rgba(0,0,0,0.9)' } : undefined}
                >
                  {isEmpty ? '-' : (slot.nozzle_diameter || '?')}
                </span>
              </div>
            </NozzleSlotHoverCard>
          );
        })}
      </div>
    </div>
  );
}

// Water drop SVG - empty outline (Bambu Lab style from bambu-humidity)
function WaterDropEmpty({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 36 54" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M17.8131 0.00538C18.4463 -0.15091 20.3648 3.14642 20.8264 3.84781C25.4187 10.816 35.3089 26.9368 35.9383 34.8694C37.4182 53.5822 11.882 61.3357 2.53721 45.3789C-1.73471 38.0791 0.016 32.2049 3.178 25.0232C6.99221 16.3662 12.6411 7.90372 17.8131 0.00538ZM18.3738 7.24807L17.5881 7.48441C14.4452 12.9431 10.917 18.2341 8.19369 23.9368C4.6808 31.29 1.18317 38.5479 7.69403 45.5657C17.3058 55.9228 34.9847 46.8808 31.4604 32.8681C29.2558 24.0969 22.4207 15.2913 18.3776 7.24807H18.3738Z" fill="#C3C2C1"/>
    </svg>
  );
}

// Water drop SVG - half filled with blue water (Bambu Lab style from bambu-humidity)
function WaterDropHalf({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 35 53" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M17.3165 0.0038C17.932 -0.14959 19.7971 3.08645 20.2458 3.77481C24.7103 10.6135 34.3251 26.4346 34.937 34.2198C36.3757 52.5848 11.5505 60.1942 2.46584 44.534C-1.68714 37.3735 0.0148 31.6085 3.08879 24.5603C6.79681 16.0605 12.2884 7.75907 17.3165 0.0038ZM17.8615 7.11561L17.0977 7.34755C14.0423 12.7048 10.6124 17.8974 7.96483 23.4941C4.54975 30.7107 1.14949 37.8337 7.47908 44.721C16.8233 54.8856 34.01 46.0117 30.5838 32.2595C28.4405 23.6512 21.7957 15.0093 17.8652 7.11561H17.8615Z" fill="#C3C2C1"/>
      <path d="M5.03547 30.112C9.64453 30.4936 11.632 35.7985 16.4154 35.791C19.6339 35.7873 20.2161 33.2283 22.3853 31.6197C31.6776 24.7286 33.5835 37.4894 27.9881 44.4254C18.1878 56.5653 -1.16063 44.6013 5.03917 30.1158L5.03547 30.112Z" fill="#1F8FEB"/>
    </svg>
  );
}

// Water drop SVG - fully filled with blue water (Bambu Lab style from bambu-humidity)
function WaterDropFull({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 36 54" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M17.9625 4.48059L4.77216 26.3154L2.08228 40.2175L10.0224 50.8414H23.1594L33.3246 42.1693V30.2455L17.9625 4.48059Z" fill="#1F8FEB"/>
      <path d="M17.7948 0.00538C18.4273 -0.15091 20.3438 3.14642 20.8048 3.84781C25.3921 10.816 35.2715 26.9368 35.9001 34.8694C37.3784 53.5822 11.8702 61.3357 2.53562 45.3789C-1.73163 38.0829 0.0134 32.2087 3.1757 25.027C6.98574 16.3662 12.6284 7.90372 17.7948 0.00538ZM18.3549 7.24807L17.57 7.48441C14.4306 12.9431 10.9063 18.2341 8.1859 23.9368C4.67686 31.29 1.18305 38.5479 7.68679 45.5657C17.2881 55.9228 34.9476 46.8808 31.4271 32.8681C29.2249 24.0969 22.3974 15.2913 18.3587 7.24807H18.3549Z" fill="#C3C2C1"/>
    </svg>
  );
}

// Thermometer SVG - empty outline
function ThermometerEmpty({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 12 20" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M6 0.5C4.6 0.5 3.5 1.6 3.5 3V12.1C2.6 12.8 2 13.9 2 15C2 17.2 3.8 19 6 19C8.2 19 10 17.2 10 15C10 13.9 9.4 12.8 8.5 12.1V3C8.5 1.6 7.4 0.5 6 0.5Z" stroke="#C3C2C1" strokeWidth="1" fill="none"/>
      <circle cx="6" cy="15" r="2.5" stroke="#C3C2C1" strokeWidth="1" fill="none"/>
    </svg>
  );
}

// Thermometer SVG - half filled (gold - same as humidity fair)
function ThermometerHalf({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 12 20" fill="none" xmlns="http://www.w3.org/2000/svg">
      <rect x="4.5" y="8" width="3" height="4.5" fill="#d4a017" rx="0.5"/>
      <circle cx="6" cy="15" r="2" fill="#d4a017"/>
      <path d="M6 0.5C4.6 0.5 3.5 1.6 3.5 3V12.1C2.6 12.8 2 13.9 2 15C2 17.2 3.8 19 6 19C8.2 19 10 17.2 10 15C10 13.9 9.4 12.8 8.5 12.1V3C8.5 1.6 7.4 0.5 6 0.5Z" stroke="#C3C2C1" strokeWidth="1" fill="none"/>
    </svg>
  );
}

// Nozzle icon — schematic hot-end view (filament body + heater block + tip).
// Added for visual parity with the thermometer icons on the dual-nozzle card
// that previously had no icon at all (#1115).
function NozzleIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <rect x="9.2" y="3.4" width="5.6" height="8.1" />
      <rect x="6" y="11.5" width="12.1" height="3.7" />
      <path d="M 7.3 15.2 L 12.1 19.6 L 16.7 15.2" />
    </svg>
  );
}

// Thermometer SVG - fully filled (red - same as humidity bad)
function ThermometerFull({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 12 20" fill="none" xmlns="http://www.w3.org/2000/svg">
      <rect x="4.5" y="3" width="3" height="9.5" fill="#c62828" rx="0.5"/>
      <circle cx="6" cy="15" r="2" fill="#c62828"/>
      <path d="M6 0.5C4.6 0.5 3.5 1.6 3.5 3V12.1C2.6 12.8 2 13.9 2 15C2 17.2 3.8 19 6 19C8.2 19 10 17.2 10 15C10 13.9 9.4 12.8 8.5 12.1V3C8.5 1.6 7.4 0.5 6 0.5Z" stroke="#C3C2C1" strokeWidth="1" fill="none"/>
    </svg>
  );
}

// Heater thermometer icon - filled when heating, outline when off
interface HeaterThermometerProps {
  className?: string;
  color: string;  // The color class (e.g., "text-orange-400")
  isHeating: boolean;
}

function HeaterThermometer({ className, color, isHeating }: HeaterThermometerProps) {
  // Extract the actual color from Tailwind class for SVG fill
  const colorMap: Record<string, string> = {
    'text-orange-400': '#fb923c',
    'text-blue-400': '#60a5fa',
    'text-green-400': '#4ade80',
  };
  const fillColor = colorMap[color] || '#888';

  // Glow style when heating
  const glowStyle = isHeating ? {
    filter: `drop-shadow(0 0 4px ${fillColor}) drop-shadow(0 0 8px ${fillColor})`,
  } : {};

  if (isHeating) {
    // Filled thermometer with glow - heater is ON
    return (
      <svg className={className} style={glowStyle} viewBox="0 0 12 20" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="4.5" y="3" width="3" height="9.5" fill={fillColor} rx="0.5"/>
        <circle cx="6" cy="15" r="2" fill={fillColor}/>
        <path d="M6 0.5C4.6 0.5 3.5 1.6 3.5 3V12.1C2.6 12.8 2 13.9 2 15C2 17.2 3.8 19 6 19C8.2 19 10 17.2 10 15C10 13.9 9.4 12.8 8.5 12.1V3C8.5 1.6 7.4 0.5 6 0.5Z" stroke={fillColor} strokeWidth="1" fill="none"/>
      </svg>
    );
  }

  // Empty thermometer - heater is OFF
  return (
    <svg className={className} viewBox="0 0 12 20" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M6 0.5C4.6 0.5 3.5 1.6 3.5 3V12.1C2.6 12.8 2 13.9 2 15C2 17.2 3.8 19 6 19C8.2 19 10 17.2 10 15C10 13.9 9.4 12.8 8.5 12.1V3C8.5 1.6 7.4 0.5 6 0.5Z" stroke={fillColor} strokeWidth="1" fill="none"/>
      <circle cx="6" cy="15" r="2.5" stroke={fillColor} strokeWidth="1" fill="none"/>
    </svg>
  );
}

// Humidity indicator with water drop that fills based on level (Bambu Lab style)
// Reference: https://github.com/theicedmango/bambu-humidity
interface HumidityIndicatorProps {
  humidity: number | string;
  goodThreshold?: number;  // <= this is green
  fairThreshold?: number;  // <= this is orange, > is red
  onClick?: () => void;
  compact?: boolean;  // Smaller version for grid layout
}

function HumidityIndicator({ humidity, goodThreshold = 40, fairThreshold = 60, onClick, compact }: HumidityIndicatorProps) {
  const humidityValue = typeof humidity === 'string' ? parseInt(humidity, 10) : humidity;
  const good = typeof goodThreshold === 'number' ? goodThreshold : 40;
  const fair = typeof fairThreshold === 'number' ? fairThreshold : 60;

  // Status thresholds (configurable via settings)
  // Good: ≤goodThreshold (green #22a352), Fair: ≤fairThreshold (gold #d4a017), Bad: >fairThreshold (red #c62828)
  let textColor: string;
  let statusText: string;

  if (isNaN(humidityValue)) {
    textColor = '#C3C2C1';
    statusText = 'Unknown';
  } else if (humidityValue <= good) {
    textColor = '#22a352'; // Green - Good
    statusText = 'Good';
  } else if (humidityValue <= fair) {
    textColor = '#d4a017'; // Gold - Fair
    statusText = 'Fair';
  } else {
    textColor = '#c62828'; // Red - Bad
    statusText = 'Bad';
  }

  // Fill level based on status: Good=Empty (dry), Fair=Half, Bad=Full (wet)
  let DropComponent: React.FC<{ className?: string }>;
  if (isNaN(humidityValue)) {
    DropComponent = WaterDropEmpty;
  } else if (humidityValue <= good) {
    DropComponent = WaterDropEmpty; // Good - empty drop (dry)
  } else if (humidityValue <= fair) {
    DropComponent = WaterDropHalf; // Fair - half filled
  } else {
    DropComponent = WaterDropFull; // Bad - full (too humid)
  }

  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex items-center gap-1 ${onClick ? 'cursor-pointer hover:opacity-80 transition-opacity' : ''}`}
      title={`Humidity: ${humidityValue}% - ${statusText}${onClick ? ' (click for history)' : ''}`}
    >
      <DropComponent className={compact ? "w-2.5 h-3" : "w-3 h-4"} />
      <span className={`font-medium tabular-nums ${compact ? 'text-[10px]' : 'text-xs'}`} style={{ color: textColor }}>{humidityValue}%</span>
    </button>
  );
}

// Temperature indicator with dynamic icon and coloring
interface TemperatureIndicatorProps {
  temp: number;
  goodThreshold?: number;  // <= this is blue
  fairThreshold?: number;  // <= this is orange, > is red
  onClick?: () => void;
  compact?: boolean;  // Smaller version for grid layout
}

function TemperatureIndicator({ temp, goodThreshold = 28, fairThreshold = 35, onClick, compact }: TemperatureIndicatorProps) {
  // Ensure thresholds are numbers
  const good = typeof goodThreshold === 'number' ? goodThreshold : 28;
  const fair = typeof fairThreshold === 'number' ? fairThreshold : 35;

  let textColor: string;
  let statusText: string;
  let ThermoComponent: React.FC<{ className?: string }>;

  if (temp <= good) {
    textColor = '#22a352'; // Green - good (same as humidity)
    statusText = 'Good';
    ThermoComponent = ThermometerEmpty;
  } else if (temp <= fair) {
    textColor = '#d4a017'; // Gold - fair (same as humidity)
    statusText = 'Fair';
    ThermoComponent = ThermometerHalf;
  } else {
    textColor = '#c62828'; // Red - bad (same as humidity)
    statusText = 'Bad';
    ThermoComponent = ThermometerFull;
  }

  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex items-center gap-1 ${onClick ? 'cursor-pointer hover:opacity-80 transition-opacity' : ''}`}
      title={`Temperature: ${temp}°C - ${statusText}${onClick ? ' (click for history)' : ''}`}
    >
      <ThermoComponent className={compact ? "w-2.5 h-3" : "w-3 h-4"} />
      <span className={`tabular-nums text-right ${compact ? 'text-[10px] w-8' : 'w-12'}`} style={{ color: textColor }}>{temp}°C</span>
    </button>
  );
}

// Get AMS label: AMS-A/B/C/D for regular AMS, HT-A/B for AMS-HT (single spool)
// Always use tray count as the source of truth (1 tray = AMS-HT, 4 trays = regular AMS)
// AMS-HT uses IDs 128+ while regular AMS uses 0-3
function getAmsLabel(amsId: number | string, trayCount: number): string {
  // Ensure amsId is a number (backend might send string)
  const id = typeof amsId === 'string' ? parseInt(amsId, 10) : amsId;
  const safeId = isNaN(id) ? 0 : id;
  const isHt = trayCount === 1;
  // AMS-HT uses IDs starting at 128, regular AMS uses 0-3
  const normalizedId = safeId >= 128 ? safeId - 128 : safeId;
  const letter = String.fromCharCode(65 + normalizedId); // 0=A, 1=B, 2=C, 3=D
  return isHt ? `HT-${letter}` : `AMS-${letter}`;
}


// isBambuLabSpool moved to ../utils/amsHelpers (upstream PR #1241).

function CoverImage({ url, printName }: { url: string | null; printName?: string }) {
  const { t } = useTranslation();
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState(false);
  const [showOverlay, setShowOverlay] = useState(false);

  // Cache-bust the image URL when the print name changes so the browser
  // fetches the new cover instead of serving the stale cached image.
  const cacheBustedUrl = useMemo(() => {
    if (!url) return null;
    const sep = url.includes('?') ? '&' : '?';
    return withStreamToken(`${url}${sep}v=${encodeURIComponent(printName || Date.now().toString())}`);
  }, [url, printName]);

  // Reset loaded/error state when the image URL changes
  useEffect(() => {
    setLoaded(false);
    setError(false);
  }, [cacheBustedUrl]);

  return (
    <>
      <div
        className={`w-20 h-20 flex-shrink-0 rounded-lg overflow-hidden bg-bambu-dark-tertiary flex items-center justify-center ${cacheBustedUrl && loaded ? 'cursor-pointer' : ''}`}
        onClick={() => cacheBustedUrl && loaded && setShowOverlay(true)}
      >
        {cacheBustedUrl && !error ? (
          <>
            <img
              src={cacheBustedUrl}
              alt={t('printers.printPreview')}
              className={`w-full h-full object-cover ${loaded ? 'block' : 'hidden'}`}
              onLoad={() => setLoaded(true)}
              onError={() => setError(true)}
            />
            {!loaded && <Box className="w-8 h-8 text-bambu-gray" />}
          </>
        ) : (
          <Box className="w-8 h-8 text-bambu-gray" />
        )}
      </div>

      {/* Cover Image Overlay */}
      {showOverlay && cacheBustedUrl && (
        <div
          className="fixed inset-0 bg-black/80 flex items-center justify-center z-50 p-8"
          onClick={() => setShowOverlay(false)}
        >
          <div className="relative max-w-2xl max-h-full">
            <img
              src={cacheBustedUrl}
              alt={t('printers.printPreview')}
              className="max-w-full max-h-[80vh] rounded-lg shadow-2xl"
            />
            {printName && (
              <p className="text-white text-center mt-4 text-lg">{printName}</p>
            )}
          </div>
        </div>
      )}
    </>
  );
}

interface PrinterMaintenanceInfo {
  due_count: number;
  warning_count: number;
  total_print_hours: number;
}

// Status summary bar component - uses queryClient to read cached statuses
function StatusSummaryBar({ printers }: { printers: Printer[] | undefined }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  // Subscribe to query cache changes to re-render when status updates
  // Throttled to prevent rapid re-renders from causing tab crashes
  const [cacheTick, setCacheTick] = useState(0);
  useEffect(() => {
    let pending = false;
    const unsubscribe = queryClient.getQueryCache().subscribe(() => {
      if (!pending) {
        pending = true;
        requestAnimationFrame(() => {
          setCacheTick(t => t + 1);
          pending = false;
        });
      }
    });
    return () => unsubscribe();
  }, [queryClient]);

  const { counts, nextFinish } = useMemo(() => {
    let printing = 0;
    let idle = 0;
    let offline = 0;
    let loading = 0;
    let problem = 0;
    let nextPrinterName: string | null = null;
    let nextRemainingMin: number | null = null;
    let nextProgress: number = 0;

    printers?.forEach((printer) => {
      const status = queryClient.getQueryData<{ connected: boolean; state: string | null; remaining_time: number | null; progress: number | null; hms_errors?: HMSError[] }>(['printerStatus', printer.id]);
      if (status === undefined) {
        // Status not yet loaded - don't count as offline yet
        loading++;
      } else if (!status.connected) {
        offline++;
      } else {
        // Count printers with HMS errors
        if (status.hms_errors && filterKnownHMSErrors(status.hms_errors).length > 0) {
          problem++;
        }
        if (status.state === 'RUNNING') {
          printing++;
          if (status.remaining_time != null && status.remaining_time > 0) {
            if (nextRemainingMin === null || status.remaining_time < nextRemainingMin) {
              nextRemainingMin = status.remaining_time;
              nextPrinterName = printer.name;
              nextProgress = status.progress || 0;
            }
          }
        } else {
          idle++;
        }
      }
    });

    return {
      counts: { printing, idle, offline, loading, problem, total: (printers?.length || 0) },
      nextFinish: nextPrinterName && nextRemainingMin ? { name: nextPrinterName, remainingMin: nextRemainingMin, progress: nextProgress } : null,
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [printers, queryClient, cacheTick]);

  if (!printers?.length) return null;

  return (
    <div className="flex flex-wrap items-center gap-4 gap-y-2 text-sm">
      <div className="flex items-center gap-1.5">
        <div className={`w-2 h-2 rounded-full ${counts.idle > 0 ? 'bg-bambu-green' : 'bg-gray-500'}`} />
        <span className="text-bambu-gray">
          <span className="text-white font-medium">{counts.idle}</span>{' '}
          {t('printers.summary.available', { count: counts.idle })}
        </span>
      </div>
      {counts.printing > 0 && (
        <div className="flex items-center gap-1.5">
          <div className="w-2 h-2 rounded-full bg-bambu-green animate-pulse" />
          <span className="text-bambu-gray">
            <span className="text-white font-medium">{counts.printing}</span>{' '}
            {t('printers.summary.printing', { count: counts.printing })}
          </span>
        </div>
      )}
      {counts.offline > 0 && (
        <div className="flex items-center gap-1.5">
          <div className="w-2 h-2 rounded-full bg-gray-400" />
          <span className="text-bambu-gray">
            <span className="text-white font-medium">{counts.offline}</span>{' '}
            {t('printers.summary.offline', { count: counts.offline })}
          </span>
        </div>
      )}
      {counts.problem > 0 && (
        <div className="flex items-center gap-1.5">
          <div className="w-2 h-2 rounded-full bg-status-error" />
          <span className="text-bambu-gray">
            <span className="text-white font-medium">{counts.problem}</span>{' '}
            {t('printers.summary.problem', { count: counts.problem })}
          </span>
        </div>
      )}
      {nextFinish && (
        <>
          <div className="w-px h-4 bg-bambu-dark-tertiary" />
          <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:gap-2">
            <div className="flex items-center gap-2">
              <span className="text-bambu-green font-medium">{t('printers.nextAvailable')}:</span>
              <span className="text-white font-medium">{nextFinish.name}</span>
            </div>
            <div className="flex items-center gap-2 w-full sm:w-auto">
              <div className="w-full sm:w-16 bg-bambu-dark-tertiary rounded-full h-1.5">
                <div
                  className="bg-bambu-green h-1.5 rounded-full transition-all"
                  style={{ width: `${nextFinish.progress}%` }}
                />
              </div>
              <span className="text-white font-medium">{Math.round(nextFinish.progress)}%</span>
              <span className="text-bambu-gray">({formatDuration(nextFinish.remainingMin * 60)})</span>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

type SortOption = 'name' | 'status' | 'model' | 'location';
type ViewMode = 'expanded' | 'compact';

// Toolbar building blocks (upstream PR #1203). The Printers page header
// renders the same control set inline on wide viewports and grouped under
// 3 overflow menus (Filters / View / Actions) on narrow viewports — a
// ResizeObserver below decides which layout fits.

type ToolbarDropdownOption<T extends string> = {
  value: T;
  label: string;
};

function ToolbarDropdown<T extends string>({
  value,
  options,
  onChange,
  fullWidth = false,
}: {
  value: T;
  options: ToolbarDropdownOption<T>[];
  onChange: (value: T) => void;
  fullWidth?: boolean;
}) {
  const [isOpen, setIsOpen] = useState(false);
  const selectedOption = options.find((option) => option.value === value) ?? options[0];

  return (
    <div className={`relative ${fullWidth ? 'w-full min-w-0' : ''}`}>
      <button
        type="button"
        onClick={() => setIsOpen((open) => !open)}
        className={`h-8 px-2 rounded-lg border bg-bambu-dark border-bambu-dark-tertiary text-white text-sm font-medium transition-colors hover:bg-bambu-dark-tertiary focus:outline-none focus:border-bambu-green flex items-center justify-between gap-2 ${fullWidth ? 'w-full' : 'min-w-28'}`}
      >
        <span className="truncate">{selectedOption?.label}</span>
        <ChevronDown className={`w-4 h-4 text-bambu-gray transition-transform ${isOpen ? 'rotate-180' : ''}`} />
      </button>

      {isOpen && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setIsOpen(false)} />
          <div className="absolute left-0 top-full z-20 mt-1 min-w-full rounded-lg border border-bambu-dark-tertiary bg-bambu-dark-secondary py-1 shadow-xl">
            {options.map((option) => (
              <button
                key={option.value}
                type="button"
                onClick={() => {
                  onChange(option.value);
                  setIsOpen(false);
                }}
                className={`w-full px-3 py-2 text-left text-sm transition-colors hover:bg-bambu-dark-tertiary ${
                  option.value === value ? 'text-bambu-green' : 'text-white'
                }`}
              >
                {option.label}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function ToolbarMenu({
  label,
  icon,
  children,
}: {
  label: string;
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  const [isOpen, setIsOpen] = useState(false);

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setIsOpen((open) => !open)}
        className="h-8 w-8 rounded-lg border bg-bambu-dark border-bambu-dark-tertiary text-white hover:bg-bambu-dark-tertiary transition-colors flex items-center justify-center"
        aria-label={label}
        title={label}
      >
        {icon}
      </button>

      {isOpen && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setIsOpen(false)} />
          <div
            className="absolute right-0 top-full z-20 mt-1 min-w-40 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark-secondary p-2 shadow-xl"
            onClick={() => setIsOpen(false)}
          >
            {children}
          </div>
        </>
      )}
    </div>
  );
}

/**
 * Get human-readable status display text for a printer.
 * Uses stg_cur_name for detailed calibration/preparation stages,
 * otherwise formats the gcode_state nicely.
 */
function getStatusDisplay(state: string | null | undefined, stg_cur_name: string | null | undefined): string {
  // If we have a specific stage name (calibration, heating, etc.), use it
  if (stg_cur_name) {
    return stg_cur_name;
  }

  // Format the gcode_state nicely
  switch (state) {
    case 'RUNNING':
      return 'Printing';
    case 'PAUSE':
      return 'Paused';
    case 'FINISH':
      return 'Finished';
    case 'FAILED':
      return 'Failed';
    case 'IDLE':
      return 'Idle';
    default:
      return state ? state.charAt(0) + state.slice(1).toLowerCase() : 'Idle';
  }
}

// ─── AMS Name Hover Card ──────────────────────────────────────────────────────
// Wraps the AMS label (e.g. "AMS-A") and shows a popup with:
//  • User-defined friendly name (editable, protected by printers:update)
//  • AMS serial number
//  • AMS firmware version
//
// Internal — rendered twice in PrintersPage; not consumed elsewhere.
function AmsNameHoverCard({
  ams,
  printerId,
  label,
  amsLabels,
  canEdit,
  onSaved,
  children,
}: {
  ams: import('../api/client').AMSUnit;
  printerId: number;
  label: string;           // auto-generated label, e.g. "AMS-A"
  amsLabels?: Record<number, string>;
  canEdit: boolean;
  onSaved: () => void;
  children: React.ReactNode;
}) {
  const { t } = useTranslation();
  const [isVisible, setIsVisible] = useState(false);
  const [position, setPosition] = useState<'top' | 'bottom'>('top');
  const [editValue, setEditValue] = useState('');
  const [isSaving, setIsSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [isInputFocused, setIsInputFocused] = useState(false);
  const triggerRef = useRef<HTMLDivElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (isVisible) {
      setEditValue(amsLabels?.[ams.id] ?? '');
      setSaveError(null);
      requestAnimationFrame(() => {
        if (triggerRef.current && cardRef.current) {
          const rect = triggerRef.current.getBoundingClientRect();
          const spaceAbove = rect.top - 56;
          const spaceBelow = window.innerHeight - rect.bottom;
          setPosition(spaceAbove < cardRef.current.offsetHeight + 12 && spaceBelow > spaceAbove ? 'bottom' : 'top');
        }
      });
    }
  }, [isVisible, amsLabels, ams.id]);

  const handleMouseEnter = () => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(() => setIsVisible(true), 80);
  };
  const handleMouseLeave = () => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    if (!isInputFocused) {
      timeoutRef.current = setTimeout(() => setIsVisible(false), 200);
    }
  };
  useEffect(() => () => { if (timeoutRef.current) clearTimeout(timeoutRef.current); }, []);

  const handleSave = async () => {
    if (!canEdit) return;
    setIsSaving(true);
    setSaveError(null);
    try {
      const trimmed = editValue.trim();
      if (trimmed) {
        await api.saveAmsLabel(printerId, ams.id, trimmed, ams.serial_number);
      } else {
        await api.deleteAmsLabel(printerId, ams.id, ams.serial_number);
      }
      onSaved();
      setIsVisible(false);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsSaving(false);
    }
  };

  const handleClear = async () => {
    if (!canEdit) return;
    setIsSaving(true);
    setSaveError(null);
    try {
      await api.deleteAmsLabel(printerId, ams.id, ams.serial_number);
      onSaved();
      setIsVisible(false);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div
      ref={triggerRef}
      className="relative inline-block"
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
    >
      {children}

      {isVisible && (
        <div
          ref={cardRef}
          className={`
            absolute left-0 z-50
            ${position === 'top' ? 'bottom-full mb-2' : 'top-full mt-2'}
            animate-in fade-in-0 zoom-in-95 duration-150
          `}
          style={{ maxWidth: 'calc(100vw - 24px)' }}
          onMouseEnter={handleMouseEnter}
          onMouseLeave={handleMouseLeave}
        >
          <div className="w-52 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl overflow-hidden backdrop-blur-sm p-2.5 space-y-2">
            {/* AMS auto-label */}
            <div className="text-[10px] uppercase tracking-wider text-bambu-gray font-medium">{label}</div>

            {/* Serial number */}
            <div className="flex items-center justify-between gap-2">
              <span className="text-[10px] tracking-wide text-bambu-gray font-medium shrink-0">
                {t('printers.amsPopup.serialNumber')}
              </span>
              <span className="text-[10px] text-white font-mono truncate">{ams.serial_number || '-'}</span>
            </div>

            {/* Firmware version */}
            <div className="flex items-center justify-between gap-2">
              <span className="text-[10px] tracking-wide text-bambu-gray font-medium shrink-0">
                {t('printers.amsPopup.firmwareVersion')}
              </span>
              <span className="text-[10px] text-white font-mono truncate">{ams.sw_ver || '-'}</span>
            </div>

            {/* Divider */}
            <div className="h-px bg-bambu-dark-tertiary/50" />

            {/* Friendly name editor */}
            <div className="space-y-1">
              <span className="text-[10px] text-bambu-gray font-medium block">
                {t('printers.amsPopup.friendlyName')}
              </span>
              <input
                type="text"
                value={editValue}
                onChange={(e) => canEdit && setEditValue(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleSave()}
                onFocus={() => setIsInputFocused(true)}
                onBlur={() => {
                  setIsInputFocused(false);
                  if (timeoutRef.current) clearTimeout(timeoutRef.current);
                    timeoutRef.current = setTimeout(() => setIsVisible(false), 200);
                }}
                placeholder={canEdit ? t('printers.amsPopup.friendlyNamePlaceholder') : (amsLabels?.[ams.id] || '-')}
                disabled={!canEdit}
                title={!canEdit ? t('printers.amsPopup.noEditPermission') : undefined}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1 text-xs text-white placeholder-bambu-gray/60 focus:outline-none focus:border-bambu-green disabled:opacity-50 disabled:cursor-not-allowed"
                maxLength={100}
              />
              {canEdit && (
                <div className="space-y-1">
                  {saveError && (
                    <p className="text-[10px] text-red-400 break-words">{saveError}</p>
                  )}
                  <div className="flex gap-1 justify-end">
                    <button
                      onClick={handleSave}
                      disabled={isSaving}
                      className="px-2 py-0.5 text-[10px] bg-bambu-green text-white rounded hover:bg-bambu-green/80 disabled:opacity-50"
                    >
                      {t('printers.amsPopup.save')}
                    </button>
                    {amsLabels?.[ams.id] && (
                      <button
                        onClick={handleClear}
                        disabled={isSaving}
                        className="px-2 py-0.5 text-[10px] bg-bambu-dark-tertiary text-bambu-gray rounded hover:bg-bambu-dark-tertiary/70 disabled:opacity-50"
                      >
                        {t('printers.amsPopup.clear')}
                      </button>
                    )}
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}


// AMS drying presets from BambuStudio filament profiles (idle mode temps)
// Format: { n3f temp, n3s temp, n3f hours, n3s hours }
const DRYING_PRESETS: Record<string, { n3f: number; n3s: number; n3f_hours: number; n3s_hours: number }> = {
  'PLA':   { n3f: 45, n3s: 45, n3f_hours: 12, n3s_hours: 12 },
  'PETG':  { n3f: 65, n3s: 65, n3f_hours: 12, n3s_hours: 12 },
  'TPU':   { n3f: 65, n3s: 75, n3f_hours: 12, n3s_hours: 18 },
  'ABS':   { n3f: 65, n3s: 80, n3f_hours: 12, n3s_hours: 8 },
  'ASA':   { n3f: 65, n3s: 80, n3f_hours: 12, n3s_hours: 8 },
  'PA':    { n3f: 65, n3s: 85, n3f_hours: 12, n3s_hours: 12 },
  'PC':    { n3f: 65, n3s: 80, n3f_hours: 12, n3s_hours: 8 },
  'PVA':   { n3f: 65, n3s: 85, n3f_hours: 12, n3s_hours: 18 },
};

function PrinterCard({
  printer,
  hideIfDisconnected,
  maintenanceInfo,
  viewMode = 'expanded',
  cardSize = 2,
  amsThresholds,
  spoolmanEnabled = false,
  linkedSpools,
  spoolmanUrl,
  spoolmanSyncMode,
  spoolmanSpools,
  spoolmanSlotAssignments,
  spoolmanLoading = false,
  onUnassignSpoolmanSpool,
  onGetAssignment,
  onUnassignSpool,
  timeFormat = 'system',
  dateFormat = 'system',
  cameraViewMode = 'window',
  onOpenEmbeddedCamera,
  checkPrinterFirmware = true,
  dryingPresets = DRYING_PRESETS,
  isSelected = false,
  onSelect,
  onExpand,
  spoolDisplayTemplate,
}: {
  printer: Printer;
  hideIfDisconnected?: boolean;
  maintenanceInfo?: PrinterMaintenanceInfo;
  viewMode?: ViewMode;
  cardSize?: number;
  amsThresholds?: {
    humidityGood: number;
    humidityFair: number;
    tempGood: number;
    tempFair: number;
  };
  spoolmanEnabled?: boolean;
  hasUnlinkedSpools?: boolean;
  linkedSpools?: Record<string, LinkedSpoolInfo>;
  spoolmanUrl?: string | null;
  spoolmanSyncMode?: string | null;
  spoolmanSpools?: InventorySpool[];
  spoolmanSlotAssignments?: SpoolmanSlotAssignmentRow[];
  spoolmanLoading?: boolean;
  onUnassignSpoolmanSpool?: (spoolmanSpoolId: number) => void;
  spoolAssignments?: SpoolAssignment[];
  onGetAssignment?: (printerId: number, amsId: number, trayId: number) => SpoolAssignment | undefined;
  onUnassignSpool?: (printerId: number, amsId: number, trayId: number) => void;
  timeFormat?: 'system' | '12h' | '24h';
  dateFormat?: 'system' | 'us' | 'eu' | 'iso';
  cameraViewMode?: 'window' | 'embedded';
  onOpenEmbeddedCamera?: (printerId: number, printerName: string) => void;
  checkPrinterFirmware?: boolean;
  dryingPresets?: Record<string, { n3f: number; n3s: number; n3f_hours: number; n3s_hours: number }>;
  isSelected?: boolean;
  // Modifier-aware select handler — receives the raw MouseEvent so the
  // parent can branch on ``shiftKey`` (range), ``ctrlKey`` / ``metaKey``
  // (toggle), or treat a plain checkbox click as a toggle. Always passed
  // by ``PrintersPage``; the card always renders the selection checkbox
  // and reacts to Ctrl/Cmd/Shift-click on its body.
  onSelect?: (id: number, e: React.MouseEvent) => void;
  // Expand-into-popup handler. Only renders the maximise button on
  // compact (S) cards — expanded cards already show everything. The
  // parent opens a modal that re-mounts ``<PrinterCard>`` with
  // ``viewMode='expanded'`` cardSize={2} for the picked printer; this
  // way nothing is duplicated and the controls inside the popup behave
  // identically to a real M-size card.
  onExpand?: (id: number) => void;
  spoolDisplayTemplate?: string;
}) {
  const { t } = useTranslation();
  const effectiveSpoolTemplate = spoolDisplayTemplate || DEFAULT_SPOOL_DISPLAY_TEMPLATE;
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { showToast } = useToast();
  const { hasPermission } = useAuth();
  const [showMenu, setShowMenu] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [deleteArchives, setDeleteArchives] = useState(true);
  const [showEditModal, setShowEditModal] = useState(false);
  const [showFileManager, setShowFileManager] = useState(false);
  const [showMQTTDebug, setShowMQTTDebug] = useState(false);
  const [showCalibration, setShowCalibration] = useState(false);
  const [showPowerOnConfirm, setShowPowerOnConfirm] = useState(false);
  const [showPowerOffConfirm, setShowPowerOffConfirm] = useState(false);
  const [haToggleConfirm, setHaToggleConfirm] = useState<SmartPlug | null>(null);
  const [showHMSModal, setShowHMSModal] = useState(false);
  const [showStopConfirm, setShowStopConfirm] = useState(false);
  const [showPauseConfirm, setShowPauseConfirm] = useState(false);
  const [showSpeedMenu, setShowSpeedMenu] = useState<number | null>(null);
  const [showResumeConfirm, setShowResumeConfirm] = useState(false);
  const [showBedJogMenu, setShowBedJogMenu] = useState<number | null>(null);
  const [bedJogStep, setBedJogStep] = useState<number>(10);
  const [showNotHomedModal, setShowNotHomedModal] = useState<null | { distance: number }>(null);
  const [showSkipObjectsModal, setShowSkipObjectsModal] = useState(false);
  const [showUploadForPrint, setShowUploadForPrint] = useState(false);
  const [showPrinterInfo, setShowPrinterInfo] = useState(false);
  const [showMacrosMenu, setShowMacrosMenu] = useState(false);
  const closePrinterInfo = useCallback(() => setShowPrinterInfo(false), []);
  const [printAfterUpload, setPrintAfterUpload] = useState<{ id: number; filename: string } | null>(null);
  // AMS drying popover state: which AMS unit has the popover open
  const [dryingPopoverAmsId, setDryingPopoverAmsId] = useState<number | null>(null);
  const [dryingPopoverModuleType, setDryingPopoverModuleType] = useState<string>('n3f');
  const [dryingFilament, setDryingFilament] = useState('PLA');
  const [dryingTemp, setDryingTemp] = useState(50);
  const [dryingDuration, setDryingDuration] = useState(4);
  const [dryingRotateTray, setDryingRotateTray] = useState(false);
  const [dryingPopoverPos, setDryingPopoverPos] = useState<{ top: number; left: number } | null>(null);
  const [isDraggingFile, setIsDraggingFile] = useState(false);
  const [isDropUploading, setIsDropUploading] = useState(false);
  const dragCounterRef = useRef(0);
  const [amsHistoryModal, setAmsHistoryModal] = useState<{
    amsId: number;
    amsLabel: string;
    mode: 'humidity' | 'temperature';
  } | null>(null);
  const [linkSpoolModal, setLinkSpoolModal] = useState<{
    tagUid: string;
    trayUuid: string;
    printerId: number;
    amsId: number;
    trayId: number;
  } | null>(null);
  const [assignSpoolModal, setAssignSpoolModal] = useState<{
    printerId: number;
    amsId: number;
    trayId: number;
    trayInfo: { type: string; color: string; location: string; material?: string; profile?: string };
  } | null>(null);
  const [configureSlotModal, setConfigureSlotModal] = useState<{
    amsId: number;
    trayId: number;
    trayCount: number;
    trayType?: string;
    trayColor?: string;
    traySubBrands?: string;
    trayInfoIdx?: string;
    extruderId?: number;
    caliIdx?: number | null;
    savedPresetId?: string;
  } | null>(null);
  const [showFirmwareModal, setShowFirmwareModal] = useState(false);
  const [plateCheckResult, setPlateCheckResult] = useState<{
    is_empty: boolean;
    confidence: number;
    difference_percent: number;
    message: string;
    debug_image_url?: string;
    needs_calibration: boolean;
    light_warning?: boolean;
    reference_count?: number;
    max_references?: number;
    roi?: { x: number; y: number; w: number; h: number };
  } | null>(null);
  const [isCheckingPlate, setIsCheckingPlate] = useState(false);
  const [isCalibrating, setIsCalibrating] = useState(false);
  const [editingRoi, setEditingRoi] = useState<{ x: number; y: number; w: number; h: number } | null>(null);
  const [isSavingRoi, setIsSavingRoi] = useState(false);
  const [plateCheckLightWasOff, setPlateCheckLightWasOff] = useState(false);

  const { data: status } = useQuery({
    queryKey: ['printerStatus', printer.id],
    queryFn: () => api.getPrinterStatus(printer.id),
    refetchInterval: 30000, // Fallback polling, WebSocket handles real-time
  });

  // Check if any macros match this printer (for showing/hiding Macros menu item)
  const { data: allMacros } = useQuery({
    queryKey: ['macros'],
    queryFn: macrosApi.getMacros,
    staleTime: 60000,
  });
  const hasMatchingMacros = (allMacros || []).some((m: Macro) => {
    if (!m.enabled || !m.gcode?.trim()) return false;
    if (!m.printer_models.includes('*') && (!printer.model || !m.printer_models.includes(printer.model))) return false;
    if (m.swap_mode_only && !printer.swap_mode_enabled) return false;
    // A profile-bound macro only matches a printer that opted into that profile;
    // generic (null) macros still show everywhere.
    if (m.swap_profile && m.swap_profile !== printer.swap_profile) return false;
    return true;
  });

  // Check for firmware updates (cached for 5 minutes, can be disabled in settings)
  const { data: firmwareInfo } = useQuery({
    queryKey: ['firmwareUpdate', printer.id],
    queryFn: () => firmwareApi.checkPrinterUpdate(printer.id),
    staleTime: 5 * 60 * 1000,
    refetchInterval: 5 * 60 * 1000,
    enabled: checkPrinterFirmware && hasPermission('firmware:read'),
  });

  // Collect unique tray_info_idx values for cloud filament info lookup
  const trayInfoIds = useMemo(() => {
    const ids = new Set<string>();
    if (status?.ams) {
      for (const ams of status.ams) {
        for (const tray of ams.tray || []) {
          if (tray.tray_info_idx) {
            ids.add(tray.tray_info_idx);
          }
        }
      }
    }
    for (const vt of status?.vt_tray ?? []) {
      if (vt.tray_info_idx) ids.add(vt.tray_info_idx);
    }
    if (status?.nozzle_rack) {
      for (const slot of status.nozzle_rack) {
        if (slot.filament_id) {
          ids.add(slot.filament_id);
        }
      }
    }
    return Array.from(ids);
  }, [status?.ams, status?.vt_tray, status?.nozzle_rack]);

  // Fetch cloud filament info for tooltips (name includes color, also has K value)
  const { data: filamentInfo } = useQuery({
    queryKey: ['filamentInfo', trayInfoIds],
    queryFn: () => api.getFilamentInfo(trayInfoIds),
    enabled: trayInfoIds.length > 0,
    staleTime: 5 * 60 * 1000, // 5 minutes
  });

  // Fetch slot preset mappings (stores preset name for user-configured slots)
  const { data: slotPresets } = useQuery({
    queryKey: ['slotPresets', printer.id],
    queryFn: () => api.getSlotPresets(printer.id),
    staleTime: 2 * 60 * 1000, // 2 minutes
  });

  // Fetch plate list for the archive linked to the active print (upstream #881
  // follow-up). Only queried when there's a running print backed by an archive;
  // shared React Query cache with the Queue / Archives pages keeps it cheap.
  const activeArchiveId =
    (status?.state === 'RUNNING' || status?.state === 'PAUSE') ? status?.current_archive_id ?? null : null;
  const { data: activeArchivePlates } = useQuery({
    queryKey: ['archive-plates', activeArchiveId],
    queryFn: () => api.getArchivePlates(activeArchiveId!),
    enabled: activeArchiveId != null,
    staleTime: 5 * 60 * 1000,
  });
  const activePlateLabel = (() => {
    if (!activeArchivePlates?.is_multi_plate || status?.current_plate_id == null) return null;
    const plate = activeArchivePlates.plates.find(p => p.index === status.current_plate_id);
    return plate?.name || t('printers.plateNumber', { number: status.current_plate_id });
  })();

  // Fetch user-defined AMS friendly names from the database
  const { data: amsLabels, refetch: refetchAmsLabels } = useQuery({
    queryKey: ['amsLabels', printer.id],
    queryFn: () => api.getAmsLabels(printer.id),
    staleTime: 5 * 60 * 1000, // 5 minutes
  });

  // Cache WiFi signal to prevent it disappearing on updates
  const [cachedWifiSignal, setCachedWifiSignal] = useState<number | null>(null);
  useEffect(() => {
    if (status?.wifi_signal != null) {
      setCachedWifiSignal(status.wifi_signal);
    }
  }, [status?.wifi_signal]);
  const wifiSignal = status?.wifi_signal ?? cachedWifiSignal;

  // Cache connected state to prevent flicker when status briefly becomes undefined
  const cachedConnected = useRef<boolean | undefined>(undefined);
  useEffect(() => {
    if (status?.connected !== undefined) {
      cachedConnected.current = status.connected;
    }
  }, [status?.connected]);
  const isConnected = status?.connected ?? cachedConnected.current;

  // Cache ams_extruder_map to prevent L/R indicators bouncing on updates
  const cachedAmsExtruderMap = useRef<Record<string, number>>({});
  useEffect(() => {
    if (status?.ams_extruder_map && Object.keys(status.ams_extruder_map).length > 0) {
      cachedAmsExtruderMap.current = status.ams_extruder_map;
    }
  }, [status?.ams_extruder_map]);
  const amsExtruderMap = (status?.ams_extruder_map && Object.keys(status.ams_extruder_map).length > 0)
    ? status.ams_extruder_map
    : cachedAmsExtruderMap.current;

  // Cache AMS data to prevent it disappearing on idle/offline printers
  const cachedAmsData = useRef<AMSUnit[]>([]);
  useEffect(() => {
    if (status?.ams && status.ams.length > 0) {
      cachedAmsData.current = status.ams;
    }
  }, [status?.ams]);
  const amsData = (status?.ams && status.ams.length > 0) ? status.ams : cachedAmsData.current;

  // Cache tray_now to prevent flickering when undefined values come in
  // Valid tray IDs: 0-253 for AMS, 254 for external spool
  // tray_now=255 means "no tray loaded" (Bambu protocol sentinel) - never active
  const cachedTrayNow = useRef<number | undefined>(undefined);
  const currentTrayNow = status?.tray_now;
  // Update cache: 255 means "no tray" so clear cache; valid values get cached
  if (currentTrayNow !== undefined && currentTrayNow !== 255) {
    cachedTrayNow.current = currentTrayNow;
  } else if (currentTrayNow === 255) {
    cachedTrayNow.current = undefined;
  }
  const effectiveTrayNow = (currentTrayNow !== undefined && currentTrayNow !== 255)
    ? currentTrayNow
    : cachedTrayNow.current;

  // Fetch smart plug for this printer
  const { data: smartPlug } = useQuery({
    queryKey: ['smartPlugByPrinter', printer.id],
    queryFn: () => api.getSmartPlugByPrinter(printer.id),
  });

  // Fetch script plugs for this printer (for multi-device control)
  const { data: scriptPlugs } = useQuery({
    queryKey: ['scriptPlugsByPrinter', printer.id],
    queryFn: () => api.getScriptPlugsByPrinter(printer.id),
  });

  // Fetch smart plug status if plug exists (faster refresh for energy monitoring)
  const { data: plugStatus } = useQuery({
    queryKey: ['smartPlugStatus', smartPlug?.id],
    queryFn: () => smartPlug ? api.getSmartPlugStatus(smartPlug.id) : null,
    enabled: !!smartPlug,
    refetchInterval: 10000, // 10 seconds for real-time power display
  });

  // Fetch queue count for this printer
  const { data: queueItems } = useQuery({
    queryKey: ['queue', printer.id, 'pending'],
    queryFn: () => api.getQueue(printer.id, 'pending'),
  });
  const queueCount = queueItems?.length ?? 0;

  // Pull this printer's summary counters off the global queues list. Shared
  // react-query key with QueuePage means no extra network on visits that
  // already hydrated the cache.
  const { data: printerQueues } = useQuery({
    queryKey: ['queues'],
    queryFn: api.getQueues,
    staleTime: 15000,
  });
  const printerQueue = printerQueues?.find(q => q.printer_id === printer.id);

  // Fetch currently printing queue item to show who started it (Issue #206)
  const { data: printingQueueItems } = useQuery({
    queryKey: ['queue', printer.id, 'printing'],
    queryFn: () => api.getQueue(printer.id, 'printing'),
    enabled: status?.state === 'RUNNING',
  });

  // Fetch reprint user info (for prints started via Reprint, not queue - Issue #206)
  const { data: reprintUser } = useQuery({
    queryKey: ['currentPrintUser', printer.id],
    queryFn: () => api.getCurrentPrintUser(printer.id),
    enabled: status?.state === 'RUNNING',
  });

  // Combine both sources: queue item user takes precedence, then reprint user
  const currentPrintUser = printingQueueItems?.[0]?.created_by_username || reprintUser?.username;

  // Fetch last completed print for this printer
  const { data: lastPrints } = useQuery({
    queryKey: ['archives', printer.id, 'last'],
    queryFn: () => api.getArchives({ printer_id: printer.id, per_page: 1 }),
    enabled: status?.connected && status?.state !== 'RUNNING',
  });
  const lastPrint = lastPrints?.data?.[0];

  // Plate-clear status pill + button (#939, ported from upstream b046c2ca).
  const requirePlateClear = printer.require_plate_clear;
  const isPrintingOrPaused = status?.state === 'RUNNING' || status?.state === 'PAUSE';
  const needsPlateClear = requirePlateClear && status?.awaiting_plate_clear === true;
  // Share the same queue query PrinterQueueWidget already uses — react-query
  // dedupes, zero extra network. We only need to know whether the widget will
  // be rendering its green "Clear & Start Next" CTA so we can hide our yellow
  // duplicate while it's visible.
  const { data: pendingQueue } = useQuery({
    queryKey: ['queue', printer.id, 'pending'],
    queryFn: () => api.getQueue(printer.id, 'pending'),
    refetchInterval: 30000,
    enabled: status?.connected === true,
  });
  const hasAutoDispatchableQueue = (pendingQueue ?? []).some(i => !i.manual_start);
  const greenClearCtaVisible =
    needsPlateClear
    && (status?.state === 'FINISH' || status?.state === 'FAILED')
    && hasAutoDispatchableQueue;
  const showClearPlateButton =
    status?.connected && needsPlateClear && !isPrintingOrPaused && !greenClearCtaVisible;
  const plateStatus = (() => {
    if (!requirePlateClear || !status?.connected) return null;
    if (isPrintingOrPaused) {
      return {
        label: t('printers.plateStatus.inUse'),
        className: 'bg-blue-500/20 text-blue-400',
      };
    }
    if (status.awaiting_plate_clear) {
      return {
        label: t('printers.plateStatus.notCleared'),
        className: 'bg-yellow-500/20 text-yellow-400',
      };
    }
    return {
      label: t('printers.plateStatus.cleared'),
      className: 'bg-status-ok/20 text-status-ok',
    };
  })();
  const plateStatusPill = plateStatus ? (
    <span className={`inline-flex flex-shrink-0 items-center rounded-full px-2 py-0.5 text-[10px] font-medium ${plateStatus.className}`}>
      {plateStatus.label}
    </span>
  ) : null;

  // Determine if this card should be hidden (use cached connected state to prevent flicker)
  const shouldHide = hideIfDisconnected && isConnected === false;

  const deleteMutation = useMutation({
    mutationFn: (options: { deleteArchives: boolean }) =>
      api.deletePrinter(printer.id, options.deleteArchives),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['printers'] });
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      queryClient.invalidateQueries({ queryKey: ['maintenanceOverview'] });
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToDelete'), 'error'),
  });

  const connectMutation = useMutation({
    mutationFn: () => api.connectPrinter(printer.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printer.id] });
    },
  });

  const unlinkSpoolMutation = useMutation({
    mutationFn: (spoolId: number) => api.unlinkSpool(spoolId),
    onSuccess: (result) => {
      showToast(t('spoolman.unlinkSuccess') || result?.message, 'success');
      queryClient.invalidateQueries({ queryKey: ['linked-spools'] });
      queryClient.invalidateQueries({ queryKey: ['unlinked-spools'] });
    },
    onError: (error: Error) => {
      showToast(error.message || t('spoolman.unlinkFailed'), 'error');
    },
  });

  // AMS drying mutations
  const startDryingMutation = useMutation({
    mutationFn: ({ amsId, temp, duration, filament, rotateTray }: { amsId: number; temp: number; duration: number; filament: string; rotateTray: boolean }) =>
      api.startDrying(printer.id, amsId, temp, duration, filament, rotateTray),
    onSuccess: () => {
      setDryingPopoverAmsId(null);
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printer.id] });
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToSendCommand'), 'error'),
  });

  const stopDryingMutation = useMutation({
    mutationFn: (amsId: number) => api.stopDrying(printer.id, amsId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printer.id] });
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToSendCommand'), 'error'),
  });

  // Smart plug control mutations
  const powerControlMutation = useMutation({
    mutationFn: (action: 'on' | 'off') =>
      smartPlug ? api.controlSmartPlug(smartPlug.id, action) : Promise.reject('No plug'),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['smartPlugStatus', smartPlug?.id] });
    },
  });

  const toggleAutoOffMutation = useMutation({
    mutationFn: (enabled: boolean) =>
      smartPlug ? api.updateSmartPlug(smartPlug.id, { auto_off: enabled }) : Promise.reject('No plug'),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['smartPlugByPrinter', printer.id] });
      // Also invalidate the smart-plugs list to keep Settings page in sync
      queryClient.invalidateQueries({ queryKey: ['smart-plugs'] });
    },
  });

  // Run HA entity mutation - scripts use 'on' (trigger), switches use 'toggle'
  const runScriptMutation = useMutation({
    mutationFn: ({ id, action }: { id: number; action: 'on' | 'toggle' }) => api.controlSmartPlug(id, action),
    onSuccess: () => {
      showToast(t('printers.toast.scriptTriggered'));
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToRunScript'), 'error'),
  });

  // Print control mutations
  const stopPrintMutation = useMutation({
    mutationFn: () => api.stopPrint(printer.id),
    onSuccess: () => {
      showToast(t('printers.toast.printStopped'));
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printer.id] });
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToStopPrint'), 'error'),
  });

  const pausePrintMutation = useMutation({
    mutationFn: () => api.pausePrint(printer.id),
    onSuccess: () => {
      showToast(t('printers.toast.printPaused'));
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printer.id] });
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToPausePrint'), 'error'),
  });

  const resumePrintMutation = useMutation({
    mutationFn: () => api.resumePrint(printer.id),
    onSuccess: () => {
      showToast(t('printers.toast.printResumed'));
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printer.id] });
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToResumePrint'), 'error'),
  });

  const clearPlateMutation = useMutation({
    mutationFn: () => api.clearPlate(printer.id),
    onSuccess: () => {
      showToast(t('queue.clearPlateSuccess'));
      queryClient.setQueryData(['printerStatus', printer.id], (old: PrinterStatus | undefined) =>
        old ? { ...old, awaiting_plate_clear: false } : old
      );
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printer.id] });
      queryClient.invalidateQueries({ queryKey: ['queue', printer.id] });
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToSendCommand'), 'error'),
  });

  // Chamber light mutation with optimistic update
  const chamberLightMutation = useMutation({
    mutationFn: (on: boolean) => api.setChamberLight(printer.id, on),
    onMutate: async (on) => {
      // Cancel any outgoing refetches
      await queryClient.cancelQueries({ queryKey: ['printerStatus', printer.id] });
      // Snapshot the previous value
      const previousStatus = queryClient.getQueryData(['printerStatus', printer.id]);
      // Optimistically update
      queryClient.setQueryData(['printerStatus', printer.id], (old: typeof status) => ({
        ...old,
        chamber_light: on,
      }));
      return { previousStatus };
    },
    onSuccess: (_, on) => {
      showToast(`Chamber light ${on ? 'on' : 'off'}`);
    },
    onError: (error: Error, _, context) => {
      // Rollback on error
      if (context?.previousStatus) {
        queryClient.setQueryData(['printerStatus', printer.id], context.previousStatus);
      }
      showToast(error.message || t('printers.toast.failedToControlChamberLight'), 'error');
    },
  });

  // Print speed mutation with optimistic update
  const printSpeedMutation = useMutation({
    mutationFn: (mode: number) => api.setPrintSpeed(printer.id, mode),
    onMutate: async (mode) => {
      await queryClient.cancelQueries({ queryKey: ['printerStatus', printer.id] });
      const previousStatus = queryClient.getQueryData(['printerStatus', printer.id]);
      queryClient.setQueryData(['printerStatus', printer.id], (old: typeof status) => ({
        ...old,
        speed_level: mode,
      }));
      return { previousStatus };
    },
    onError: (error: Error, _, context) => {
      if (context?.previousStatus) {
        queryClient.setQueryData(['printerStatus', printer.id], context.previousStatus);
      }
      showToast(error.message || t('printers.toast.failedToSetSpeed'), 'error');
    },
  });

  const bedJogMutation = useMutation({
    mutationFn: ({ distance, force }: { distance: number; force?: boolean }) =>
      api.bedJog(printer.id, distance, force ?? false),
    onError: (error: Error) =>
      showToast(error.message || t('printers.toast.failedToSendCommand'), 'error'),
  });

  const homeAxesMutation = useMutation({
    mutationFn: (axes: 'z' | 'xy' | 'all') => api.homeAxes(printer.id, axes),
    onSuccess: () => {
      showToast(t('printers.bedJog.homingStarted'));
      // Suppress the "not homed" re-prompt for this printer in the current
      // session — Auto Home just put the printer in a homed state, so the
      // next jog click shouldn't re-open the warning modal. Mirrors the flag
      // set by "Move anyway" so either path closes the modal for the session
      // (upstream #1052 follow-up).
      try {
        sessionStorage.setItem(`bamdude.bedJog.warned.${printer.id}`, '1');
      } catch {
        /* ignore */
      }
    },
    onError: (error: Error) =>
      showToast(error.message || t('printers.toast.failedToSendCommand'), 'error'),
  });

  // Plate detection setting mutation
  const plateDetectionMutation = useMutation({
    mutationFn: (enabled: boolean) => api.updatePrinter(printer.id, { plate_detection_enabled: enabled }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['printers'] });
      showToast(plateDetectionMutation.variables ? t('printers.toast.plateCheckEnabled') : t('printers.toast.plateCheckDisabled'));
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToUpdateSetting'), 'error'),
  });

  // Query for printable objects (for skip functionality)
  // Fetch when printing with 2+ objects, slicer-side exclude support, OR when modal is open
  const isPrintingWithObjects =
    (status?.state === 'RUNNING' || status?.state === 'PAUSE')
    && (status?.printable_objects_count ?? 0) >= 2
    && (status?.skip_objects_supported ?? false);
  const { data: objectsData } = useQuery({
    queryKey: ['printableObjects', printer.id],
    queryFn: () => api.getPrintableObjects(printer.id),
    enabled: showSkipObjectsModal || isPrintingWithObjects,
    refetchInterval: showSkipObjectsModal ? 5000 : (isPrintingWithObjects ? 30000 : false), // 5s when modal open, 30s otherwise
  });

  // State for tracking which AMS slot is being refreshed
  const [refreshingSlot, setRefreshingSlot] = useState<{ amsId: number; slotId: number } | null>(null);
  // Track if we've seen the printer enter "busy" state (ams_status_main !== 0)
  const seenBusyStateRef = useRef<boolean>(false);
  // Fallback timeout ref
  const refreshTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Minimum display time passed
  const minTimePassedRef = useRef<boolean>(false);

  // AMS slot refresh mutation
  const refreshAmsSlotMutation = useMutation({
    mutationFn: ({ amsId, slotId }: { amsId: number; slotId: number }) =>
      api.refreshAmsSlot(printer.id, amsId, slotId),
    onMutate: ({ amsId, slotId }) => {
      // Clear any existing timeout
      if (refreshTimeoutRef.current) {
        clearTimeout(refreshTimeoutRef.current);
      }
      // Reset state
      seenBusyStateRef.current = false;
      minTimePassedRef.current = false;
      setRefreshingSlot({ amsId, slotId });
      // Minimum display time (2 seconds)
      setTimeout(() => {
        minTimePassedRef.current = true;
      }, 2000);
      // Fallback timeout (30 seconds max)
      refreshTimeoutRef.current = setTimeout(() => {
        setRefreshingSlot(null);
      }, 30000);
    },
    onSuccess: (data) => {
      showToast(data.message || t('printers.toast.rfidRereadInitiated'));
    },
    onError: (error: Error) => {
      showToast(error.message || t('printers.toast.failedToRereadRfid'), 'error');
      if (refreshTimeoutRef.current) {
        clearTimeout(refreshTimeoutRef.current);
      }
      setRefreshingSlot(null);
    },
  });

  // AMS load / unload mutations (#891). The printer no-ops gracefully if the
  // target slot is empty, so the button stays enabled and the toast surfaces
  // whatever the printer actually reports.
  const amsLoadMutation = useMutation({
    mutationFn: (trayId: number) => api.amsLoadFilament(printer.id, trayId),
    onSuccess: () => showToast(t('printers.ams.loadSuccess')),
    onError: (error: Error) => showToast(error.message || t('printers.ams.loadFailed'), 'error'),
  });
  const amsUnloadMutation = useMutation({
    mutationFn: () => api.amsUnloadFilament(printer.id),
    onSuccess: () => showToast(t('printers.ams.unloadSuccess')),
    onError: (error: Error) => showToast(error.message || t('printers.ams.unloadFailed'), 'error'),
  });

  // Plate references state
  const [plateReferences, setPlateReferences] = useState<{
    references: Array<{ index: number; label: string; timestamp: string; has_image: boolean; thumbnail_url: string }>;
    max_references: number;
  } | null>(null);
  const [editingRefLabel, setEditingRefLabel] = useState<{ index: number; label: string } | null>(null);

  // Fetch plate references
  const fetchPlateReferences = async () => {
    try {
      const data = await api.getPlateReferences(printer.id);
      setPlateReferences(data);
    } catch {
      // Ignore errors - references will show as empty
    }
  };

  // Toggle plate detection enabled/disabled
  const handleTogglePlateDetection = () => {
    plateDetectionMutation.mutate(!printer.plate_detection_enabled);
  };

  // Open plate detection management modal (for calibration/references)
  const handleOpenPlateManagement = async () => {
    setIsCheckingPlate(true);
    setPlateCheckResult(null);

    // Auto-turn on light if it's off
    const lightWasOff = status?.chamber_light === false;
    setPlateCheckLightWasOff(lightWasOff);
    if (lightWasOff) {
      await api.setChamberLight(printer.id, true);
      // Wait for light to physically turn on and camera to adjust exposure
      // (MQTT command is async, light takes ~1s to turn on, camera needs time to adjust)
      await new Promise(resolve => setTimeout(resolve, 2500));
    }

    try {
      const result = await api.checkPlateEmpty(printer.id, { includeDebugImage: true });
      setPlateCheckResult(result);
      fetchPlateReferences();
    } catch (error) {
      showToast(error instanceof Error ? error.message : t('printers.toast.failedToCheckPlate'), 'error');
      // Restore light if check failed
      if (lightWasOff) {
        await api.setChamberLight(printer.id, false);
        setPlateCheckLightWasOff(false);
      }
    } finally {
      setIsCheckingPlate(false);
    }
  };

  // Close plate check modal and restore light state
  const closePlateCheckModal = useCallback(async () => {
    setPlateCheckResult(null);
    // Restore light to original state if we turned it on
    if (plateCheckLightWasOff) {
      await api.setChamberLight(printer.id, false);
      setPlateCheckLightWasOff(false);
    }
  }, [plateCheckLightWasOff, printer.id]);

  // Calibrate plate detection handler
  const handleCalibratePlate = async (label?: string) => {
    setIsCalibrating(true);
    try {
      const result = await api.calibratePlateDetection(printer.id, { label });
      if (result.success) {
        showToast(result.message || t('printers.toast.calibrationSaved'), 'success');
        // Refresh references and re-check
        fetchPlateReferences();
        const checkResult = await api.checkPlateEmpty(printer.id, { includeDebugImage: true });
        setPlateCheckResult(checkResult);
      } else {
        showToast(result.message || t('printers.toast.calibrationFailed'), 'error');
      }
    } catch (error) {
      showToast(error instanceof Error ? error.message : t('printers.toast.calibrationFailed'), 'error');
    } finally {
      setIsCalibrating(false);
    }
  };

  // Update reference label
  const handleUpdateRefLabel = async (index: number, label: string) => {
    try {
      await api.updatePlateReferenceLabel(printer.id, index, label);
      setEditingRefLabel(null);
      fetchPlateReferences();
    } catch (error) {
      showToast(error instanceof Error ? error.message : t('printers.toast.failedToUpdateLabel'), 'error');
    }
  };

  // Delete reference
  const handleDeleteRef = async (index: number) => {
    try {
      await api.deletePlateReference(printer.id, index);
      showToast(t('printers.toast.referenceDeleted'), 'success');
      fetchPlateReferences();
      // Re-check to update counts
      const checkResult = await api.checkPlateEmpty(printer.id, { includeDebugImage: true });
      setPlateCheckResult(checkResult);
    } catch (error) {
      showToast(error instanceof Error ? error.message : t('printers.toast.failedToDeleteReference'), 'error');
    }
  };

  // Save ROI settings
  const handleSaveRoi = async () => {
    if (!editingRoi) return;
    setIsSavingRoi(true);
    try {
      await api.updatePrinter(printer.id, { plate_detection_roi: editingRoi });
      showToast(t('printers.toast.detectionAreaSaved'), 'success');
      setEditingRoi(null);
      // Re-check to see new ROI in action
      const checkResult = await api.checkPlateEmpty(printer.id, { includeDebugImage: true });
      setPlateCheckResult(checkResult);
    } catch (error) {
      showToast(error instanceof Error ? error.message : t('printers.toast.failedToSaveDetectionArea'), 'error');
    } finally {
      setIsSavingRoi(false);
    }
  };

  // Close plate check modal on Escape key
  useEffect(() => {
    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && plateCheckResult) {
        closePlateCheckModal();
      }
    };
    window.addEventListener('keydown', handleEscape);
    return () => window.removeEventListener('keydown', handleEscape);
  }, [plateCheckResult, closePlateCheckModal]);

  // Watch ams_status_main to detect when RFID read completes
  // ams_status_main: 0=idle, 2=rfid_identifying
  const deferredClearRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!refreshingSlot) return;

    const amsStatus = status?.ams_status_main ?? 0;

    // Track when we see non-idle state (printer is working)
    if (amsStatus !== 0) {
      seenBusyStateRef.current = true;
      // Cancel any deferred clear since we're back to busy
      if (deferredClearRef.current) {
        clearTimeout(deferredClearRef.current);
        deferredClearRef.current = null;
      }
    }

    // When we've seen busy and now idle, clear (with min time check)
    if (seenBusyStateRef.current && amsStatus === 0) {
      if (minTimePassedRef.current) {
        // Min time passed - clear now
        if (refreshTimeoutRef.current) {
          clearTimeout(refreshTimeoutRef.current);
        }
        setRefreshingSlot(null);
      } else {
        // Schedule clear after min time (2 seconds from start)
        if (!deferredClearRef.current) {
          deferredClearRef.current = setTimeout(() => {
            if (refreshTimeoutRef.current) {
              clearTimeout(refreshTimeoutRef.current);
            }
            setRefreshingSlot(null);
          }, 2000);
        }
      }
    }

    return () => {
      if (deferredClearRef.current) {
        clearTimeout(deferredClearRef.current);
      }
    };
  }, [status?.ams_status_main, refreshingSlot]);

  // State for AMS slot menu
  const [amsSlotMenu, setAmsSlotMenu] = useState<{ amsId: number; slotId: number } | null>(null);

  if (shouldHide) {
    return null;
  }

  // Size-based styling helpers
  const getImageSize = () => {
    switch (cardSize) {
      case 1: return 'w-10 h-10';
      case 2: return 'w-14 h-14';
      case 3: return 'w-16 h-16';
      case 4: return 'w-20 h-20';
      default: return 'w-14 h-14';
    }
  };
  const getTitleSize = () => {
    switch (cardSize) {
      case 1: return 'text-base truncate';
      case 2: return 'text-lg';
      case 3: return 'text-xl';
      case 4: return 'text-2xl';
      default: return 'text-lg';
    }
  };
  const getSpacing = () => {
    switch (cardSize) {
      case 1: return 'mb-2';
      case 2: return 'mb-4';
      case 3: return 'mb-5';
      case 4: return 'mb-6';
      default: return 'mb-4';
    }
  };

  const canDrop = isConnected && status?.state !== 'RUNNING' && status?.state !== 'PAUSE' && hasPermission('printers:control');

  const handleCardDragEnter = (e: React.DragEvent) => {
    e.preventDefault();
    dragCounterRef.current++;
    if (dragCounterRef.current === 1) setIsDraggingFile(true);
  };

  const handleCardDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = canDrop ? 'copy' : 'none';
  };

  const handleCardDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    dragCounterRef.current--;
    if (dragCounterRef.current === 0) setIsDraggingFile(false);
  };

  const handleCardDrop = async (e: React.DragEvent) => {
    e.preventDefault();
    dragCounterRef.current = 0;
    setIsDraggingFile(false);

    if (!canDrop) return;

    const droppedFiles = Array.from(e.dataTransfer.files);
    const file = droppedFiles[0];
    if (!file) return;

    // Only accept sliced/printable files (.gcode, .gcode.3mf, etc.)
    const lower = file.name.toLowerCase();
    if (!lower.endsWith('.gcode') && !lower.includes('.gcode.')) {
      showToast(t('printers.dropNotPrintable', 'Only .gcode and .gcode.3mf files can be printed'), 'error');
      return;
    }

    setIsDropUploading(true);
    try {
      const result = await api.uploadLibraryFile(file, null);

      // Check printer compatibility if sliced_for_model is available in metadata
      const slicedFor = (result.metadata as Record<string, unknown>)?.sliced_for_model as string | undefined;
      const printerModel = mapModelCode(printer.model);
      if (slicedFor && printerModel && slicedFor.toLowerCase() !== printerModel.toLowerCase()) {
        await api.deleteLibraryFile(result.id).catch(() => {});
        showToast(
          t('printers.incompatibleFile', 'This file was sliced for {{slicedFor}}, but this printer is a {{printerModel}}', { slicedFor, printerModel }),
          'error'
        );
        return;
      }

      setPrintAfterUpload({ id: result.id, filename: result.filename });
    } catch {
      showToast(t('common.uploadFailed', 'Upload failed'), 'error');
    } finally {
      setIsDropUploading(false);
    }
  };

  return (
    <Card
      id={`printer-${printer.id}`}
      className={`relative scroll-mt-20 ${isSelected ? 'ring-2 ring-bambu-green' : ''}`}
      onDragEnter={handleCardDragEnter}
      onDragOver={handleCardDragOver}
      onDragLeave={handleCardDragLeave}
      onDrop={handleCardDrop}
      // Card-body modifier-click → enter selection. Plain click is left
      // alone so the buttons / hover cards inside the card aren't
      // hijacked. Buttons that bubble (no e.stopPropagation()) are still
      // safe because we only react to Ctrl/Cmd/Shift modifiers — ordinary
      // single clicks are no-op here.
      onClick={(e) => {
        if (!onSelect) return;
        if (e.ctrlKey || e.metaKey || e.shiftKey) {
          e.preventDefault();
          onSelect(printer.id, e);
        }
      }}
    >
      {/* Selection checkbox is rendered inside the three-dot menu (see
          below) as a regular menu row — keeps the card visual clean and
          surfaces the same Ctrl/Shift modifiers via the menu-item click
          event. The ``ring-2 ring-bambu-green`` on the Card root above is
          the only at-a-glance "this printer is selected" indicator. */}

      {/* Drop zone overlay */}
      {(isDraggingFile || isDropUploading) && (
        <div
          className={`absolute inset-0 z-10 rounded-xl border-2 border-dashed flex items-center justify-center transition-colors ${
            isDropUploading
              ? 'bg-bambu-green/10 border-bambu-green/50'
              : canDrop
                ? 'bg-bambu-green/10 border-bambu-green'
                : 'bg-red-500/10 border-red-500/50'
          }`}
        >
          <div className="text-center">
            {isDropUploading ? (
              <>
                <Loader2 className="w-8 h-8 mx-auto mb-2 text-bambu-green animate-spin" />
                <p className="text-sm font-medium text-bambu-green">{t('common.uploading', 'Uploading...')}</p>
              </>
            ) : canDrop ? (
              <>
                <PrinterIcon className="w-8 h-8 mx-auto mb-2 text-bambu-green" />
                <p className="text-sm font-medium text-bambu-green">{t('printers.dropToPrint', 'Drop to print')}</p>
              </>
            ) : (
              <>
                <X className="w-8 h-8 mx-auto mb-2 text-red-400" />
                <p className="text-sm font-medium text-red-400">{t('printers.cannotPrint', 'Printer busy')}</p>
              </>
            )}
          </div>
        </div>
      )}
      <CardContent className={cardSize >= 3 ? 'p-5' : ''}>
        {/* Header */}
        <div className={getSpacing()}>
          {/* Top row: Image, Name, Menu */}
          <div className="flex items-start justify-between gap-2">
            <div className="flex items-center gap-3 min-w-0 flex-1">
              {/* Printer Model Image (or print preview in compact mode) */}
              {cardSize === 1 && status?.cover_url && (status.state === 'RUNNING' || status.state === 'PAUSE') ? (
                <img
                  src={withStreamToken(status.cover_url)}
                  alt={status.subtask_name || t('printers.printPreview')}
                  className={`object-cover rounded-lg bg-bambu-dark flex-shrink-0 ${getImageSize()}`}
                />
              ) : (
                <img
                  src={getPrinterImage(printer.model)}
                  alt={printer.model || t('common.printer')}
                  className={`object-contain rounded-lg bg-bambu-dark flex-shrink-0 ${getImageSize()}`}
                />
              )}
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <h3 className={`font-semibold text-white ${getTitleSize()}`}>{printer.name}</h3>
                  {/* Pause chip — visible whenever state === 'PAUSE'; renders the
                      classified reason + a live-ticking elapsed counter. Sits next
                      to the printer name in both compact (xs) and expanded (sm)
                      modes so the cause is immediately scannable across a grid. */}
                  <PauseChip
                    state={status?.state}
                    pauseReasonLabel={status?.pause_reason_label}
                    pauseStartedAt={status?.pause_started_at}
                    size={viewMode === 'compact' ? 'xs' : 'sm'}
                  />
                  {/* Connection indicator dot for compact mode */}
                  {viewMode === 'compact' && (() => {
                    const hmsErrors = status?.connected && status.hms_errors ? filterKnownHMSErrors(status.hms_errors) : [];
                    const hasSevere = hmsErrors.some(e => e.severity <= 2);
                    const hasWarning = hmsErrors.length > 0;
                    // PAUSE state without HMS used to render as a green "ok" pip — easy to
                    // miss in a 20-printer compact grid. Treat any PAUSE as warning-yellow
                    // unless severe HMS bumps it to red.
                    const isPaused = status?.connected && status?.state === 'PAUSE';
                    const pipColor = !status?.connected
                      ? 'bg-status-error'
                      : hasSevere
                        ? 'bg-status-error'
                        : (hasWarning || isPaused)
                          ? 'bg-status-warning'
                          : 'bg-status-ok';
                    const pipTitle = !status?.connected
                      ? t('printers.connection.offline')
                      : hasWarning
                        ? `${hmsErrors.length} HMS ${hmsErrors.length === 1 ? 'error' : 'errors'}`
                        : isPaused
                          ? (status?.pause_reason_label || t('printers.status.paused'))
                          : t('printers.connection.connected');
                    return (
                      <div
                        className={`w-2 h-2 rounded-full flex-shrink-0 ${pipColor}`}
                        title={pipTitle}
                      />
                    );
                  })()}
                </div>
                <p className="text-sm text-bambu-gray flex items-center gap-1.5 flex-wrap">
                  {printer.swap_mode_enabled && (
                    <span className="text-[10px] px-1 py-0.5 bg-amber-500/20 text-amber-400 rounded inline-flex items-center gap-0.5" title={t('printers.swapMode')}>
                      <ArrowLeftRight className="w-2.5 h-2.5" />
                      SWAP
                    </span>
                  )}
                  {printer.model || 'Unknown Model'}
                  {viewMode === 'expanded' && status?.nozzles && status.nozzles[0]?.nozzle_diameter && (
                    <span className="text-bambu-gray" title={status.nozzles[0].nozzle_type || 'Nozzle'}>
                      • {status.nozzles[0].nozzle_diameter}mm
                    </span>
                  )}
                  {viewMode === 'expanded' && maintenanceInfo && maintenanceInfo.total_print_hours > 0 && (
                    <span className="text-bambu-gray">
                      <Clock className="w-3 h-3 inline-block mr-1" />
                      {Math.round(maintenanceInfo.total_print_hours)}h
                    </span>
                  )}
                </p>
              </div>
            </div>
            {/* Selection checkbox + menu button + dropdown — single
                ``relative flex`` container so the dropdown's ``right-0``
                still anchors to the rightmost edge (which is the menu
                button's right edge — checkbox sits to its left).
                Checkbox is hidden when the parent doesn't pass
                ``onSelect`` (single-printer farms etc.). Native click on
                the checkbox preserves Shift/Ctrl/Meta modifiers, so
                range-select works from the checkbox the same as from the
                card body. */}
            <div className="relative flex items-center gap-1 flex-shrink-0">
              {onSelect && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={(e) => {
                    e.stopPropagation();
                    onSelect(printer.id, e);
                  }}
                  title={t('printers.bulk.selectHint')}
                  aria-pressed={isSelected}
                >
                  {isSelected ? (
                    <CheckSquare className="w-4 h-4 text-bambu-green" />
                  ) : (
                    <Square className="w-4 h-4 text-bambu-gray" />
                  )}
                </Button>
              )}
              {/* Maximise: only meaningful on compact (S) cards — clicking
                  pops up the same printer rendered as a regular M-card so
                  the operator can poke its controls without flipping the
                  whole grid out of compact view. Hidden on M/L/XL where
                  every action is already inline. */}
              {viewMode === 'compact' && onExpand && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={(e) => {
                    e.stopPropagation();
                    onExpand(printer.id);
                  }}
                  title={t('printers.expandCardHint')}
                  aria-label={t('printers.expandCard')}
                >
                  <Maximize2 className="w-4 h-4" />
                </Button>
              )}
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setShowMenu(!showMenu)}
              >
                <MoreVertical className="w-4 h-4" />
              </Button>
              {showMenu && (
                <>
                <div className="fixed inset-0 z-10" onClick={() => setShowMenu(false)} />
                {/* Anchor by the top-right corner of the cluster (kebab's
                    own top edge): ``top-full`` was the previous default
                    via static-positioned ``mt-2``, which let the dropdown
                    push downward past the viewport bottom in the expand
                    popup (and the small visible portion at the top
                    suggested it was being clipped). ``top-0`` aligns the
                    dropdown's top with the kebab's top so the menu unfurls
                    from the corner — same direction (downward) but the
                    visual origin is the corner, not the button's bottom. */}
                <div className="absolute right-0 top-0 max-w-58 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-lg z-20 whitespace-nowrap">
                  {/* Info & Maintenance */}
                  <button
                    className="w-full px-4 py-2 text-left text-sm hover:bg-bambu-dark-tertiary flex items-center gap-2"
                    onClick={() => {
                      setShowPrinterInfo(true);
                      setShowMenu(false);
                    }}
                  >
                    <Info className="w-4 h-4" />
                    {t('printers.printerInformation')}
                  </button>
                  <button
                    className="w-full px-4 py-2 text-left text-sm hover:bg-bambu-dark-tertiary flex items-center gap-2"
                    onClick={() => {
                      navigate(`/maintenance?printer=${printer.id}`);
                      setShowMenu(false);
                    }}
                  >
                    <Wrench className="w-4 h-4" />
                    {t('printers.maintenanceHistory')}
                  </button>
                  <div className="mx-3 my-1 border-t border-bambu-dark-tertiary" />
                  {/* Calibration & Macros */}
                  <button
                    className={`w-full px-4 py-2 text-left text-sm hover:bg-bambu-dark-tertiary flex items-center gap-2 ${
                      !hasPermission('printers:control') ? 'opacity-50 cursor-not-allowed' : ''
                    }`}
                    onClick={() => {
                      if (!hasPermission('printers:control')) return;
                      setShowCalibration(true);
                      setShowMenu(false);
                    }}
                  >
                    <Wrench className="w-4 h-4" />
                    {t('printers.calibration.menuItem')}
                  </button>
                  {hasMatchingMacros && (
                    <button
                      className={`w-full px-4 py-2 text-left text-sm hover:bg-bambu-dark-tertiary flex items-center gap-2 ${
                        !hasPermission('printers:control') ? 'opacity-50 cursor-not-allowed' : ''
                      }`}
                      onClick={() => {
                        if (!hasPermission('printers:control')) return;
                        setShowMacrosMenu(true);
                        setShowMenu(false);
                      }}
                    >
                      <Play className="w-4 h-4" />
                      {t('printers.macros')}
                    </button>
                  )}
                  <div className="mx-3 my-1 border-t border-bambu-dark-tertiary" />
                  {/* Connection & Debug */}
                  <button
                    className="w-full px-4 py-2 text-left text-sm hover:bg-bambu-dark-tertiary flex items-center gap-2"
                    onClick={() => {
                      connectMutation.mutate();
                      setShowMenu(false);
                    }}
                  >
                    <RefreshCw className="w-4 h-4" />
                    {t('printers.reconnect')}
                  </button>
                  <button
                    className="w-full px-4 py-2 text-left text-sm hover:bg-bambu-dark-tertiary flex items-center gap-2"
                    onClick={() => {
                      setShowMQTTDebug(true);
                      setShowMenu(false);
                    }}
                  >
                    <Terminal className="w-4 h-4" />
                    {t('printers.mqttDebug')}
                  </button>
                  <div className="mx-3 my-1 border-t border-bambu-dark-tertiary" />
                  {/* Edit & Delete */}
                  <button
                    className={`w-full px-4 py-2 text-left text-sm flex items-center gap-2 ${
                      hasPermission('printers:update')
                        ? 'hover:bg-bambu-dark-tertiary'
                        : 'opacity-50 cursor-not-allowed'
                    }`}
                    onClick={() => {
                      if (!hasPermission('printers:update')) return;
                      setShowEditModal(true);
                      setShowMenu(false);
                    }}
                    title={!hasPermission('printers:update') ? t('printers.permission.noEdit') : undefined}
                  >
                    <Pencil className="w-4 h-4" />
                    {t('common.edit')}
                  </button>
                  <button
                    className={`w-full px-4 py-2 text-left text-sm flex items-center gap-2 ${
                      hasPermission('printers:delete')
                        ? 'text-red-400 hover:bg-bambu-dark-tertiary'
                        : 'text-red-400/50 cursor-not-allowed'
                    }`}
                    onClick={() => {
                      if (!hasPermission('printers:delete')) return;
                      setShowDeleteConfirm(true);
                      setShowMenu(false);
                    }}
                    title={!hasPermission('printers:delete') ? t('printers.permission.noDelete') : undefined}
                  >
                    <Trash2 className="w-4 h-4" />
                    {t('common.delete')}
                  </button>
                </div>
                </>
              )}
            </div>
          </div>

          {/* Badges row - only in expanded mode */}
          {viewMode === 'expanded' && (
            <div className="flex flex-wrap items-center gap-2 mt-2">
              {/* Connection status badge */}
              <span
                className={`flex items-center gap-1.5 px-2 py-1 rounded-full text-xs ${
                  status?.connected
                    ? 'bg-status-ok/20 text-status-ok'
                    : 'bg-status-error/20 text-status-error'
                }`}
              >
                {status?.connected ? (
                  <Link className="w-3 h-3" />
                ) : (
                  <Unlink className="w-3 h-3" />
                )}
                {status?.connected ? t('printers.connection.connected') : t('printers.connection.offline')}
              </span>
              {/* Network connection indicator */}
              {status?.connected && status?.wired_network && (
                <span
                  className="flex items-center gap-1 px-2 py-1 rounded-full text-xs bg-status-ok/20 text-status-ok"
                  title={t('printers.connection.ethernet', 'Ethernet')}
                >
                  <Cable className="w-3 h-3" />
                  {t('printers.connection.ethernet', 'Ethernet')}
                </span>
              )}
              {/* WiFi signal indicator */}
              {status?.connected && !status?.wired_network && wifiSignal != null && (
                <span
                  className={`flex items-center gap-1 px-2 py-1 rounded-full text-xs ${
                    wifiSignal >= -50
                      ? 'bg-status-ok/20 text-status-ok'
                      : wifiSignal >= -60
                      ? 'bg-status-ok/20 text-status-ok'
                      : wifiSignal >= -70
                      ? 'bg-status-warning/20 text-status-warning'
                      : wifiSignal >= -80
                      ? 'bg-orange-500/20 text-orange-600'
                      : 'bg-status-error/20 text-status-error'
                  }`}
                  title={`WiFi: ${wifiSignal} dBm - ${t(getWifiStrength(wifiSignal).labelKey)}`}
                >
                  <Signal className="w-3 h-3" />
                  {wifiSignal}dBm
                </span>
              )}
              {/* HMS Status Indicator */}
              {status?.connected && (() => {
                const knownErrors = status.hms_errors ? filterKnownHMSErrors(status.hms_errors) : [];
                return (
                  <button
                    onClick={() => setShowHMSModal(true)}
                    className={`flex items-center gap-1 px-2 py-1 rounded-full text-xs cursor-pointer hover:opacity-80 transition-opacity ${
                      knownErrors.length > 0
                        ? knownErrors.some(e => e.severity <= 2)
                          ? 'bg-status-error/20 text-status-error'
                          : 'bg-status-warning/20 text-status-warning'
                        : 'bg-status-ok/20 text-status-ok'
                    }`}
                    title={t('printers.clickToViewHmsErrors')}
                  >
                    <AlertTriangle className="w-3 h-3" />
                    {knownErrors.length > 0 ? knownErrors.length : 'OK'}
                  </button>
                );
              })()}
              {/* SD card missing indicator — shown only when the printer is online
                  AND reports no SD card. Heartbeat flap from upstream's #899/#0D7C0D40
                  series can't happen here because our permissive sdcard parser
                  reads the top-level field only (it doesn't derive from
                  home_flag bits 8-9, which is what flipped on heartbeats). */}
              {status?.connected && status?.sdcard === false && (
                <button
                  onClick={() => setShowPrinterInfo(true)}
                  className="flex items-center gap-1 px-2 py-1 rounded-full text-xs bg-status-warning/20 text-status-warning cursor-pointer hover:opacity-80 transition-opacity"
                  title={t('printers.sdCardMissing')}
                >
                  <HardDrive className="w-3 h-3" />
                  {t('printers.noSd')}
                </button>
              )}
              {/* Enclosure door badge — rendered only for printers with a
                  confirmed door-open sensor exposed via MQTT (X1 family only).
                  Open = yellow warning (firmware may pause the print), closed
                  = green OK. See utils/printer.ts::hasDoorSensor for the
                  whitelist + rationale; keep it in sync with the backend
                  counterpart in utils/printer_models.py. */}
              {status?.connected && hasDoorSensor(printer.model) && (
                <span
                  className={`flex items-center px-2 py-1 rounded-full text-xs ${
                    status.door_open
                      ? 'bg-status-warning/20 text-status-warning'
                      : 'bg-status-ok/20 text-status-ok'
                  }`}
                  title={status.door_open ? t('printers.door.open') : t('printers.door.closed')}
                >
                  {status.door_open ? <DoorOpen className="w-3 h-3" /> : <DoorClosed className="w-3 h-3" />}
                </span>
              )}
              {/* Maintenance Status Indicator */}
              {maintenanceInfo && (
                <button
                  onClick={() => navigate('/maintenance')}
                  className={`flex items-center gap-1 px-2 py-1 rounded-full text-xs cursor-pointer hover:opacity-80 transition-opacity ${
                    maintenanceInfo.due_count > 0
                      ? 'bg-status-error/20 text-status-error'
                      : maintenanceInfo.warning_count > 0
                      ? 'bg-status-warning/20 text-status-warning'
                      : 'bg-status-ok/20 text-status-ok'
                  }`}
                  title={
                    maintenanceInfo.due_count > 0 && maintenanceInfo.warning_count > 0
                      ? t('printers.maintenanceDueAndWarningTooltip', {
                          due: maintenanceInfo.due_count,
                          warning: maintenanceInfo.warning_count,
                        })
                      : maintenanceInfo.due_count > 0
                      ? t('printers.maintenanceDueTooltip', { count: maintenanceInfo.due_count })
                      : maintenanceInfo.warning_count > 0
                      ? t('printers.maintenanceWarningTooltip', { count: maintenanceInfo.warning_count })
                      : t('printers.maintenanceUpToDate')
                  }
                >
                  <Wrench className="w-3 h-3" />
                  {maintenanceInfo.due_count > 0 || maintenanceInfo.warning_count > 0
                    ? maintenanceInfo.due_count + maintenanceInfo.warning_count
                    : 'OK'}
                </button>
              )}
              {/* Queue Count Badge */}
              {queueCount > 0 && (
                <button
                  onClick={() => navigate('/queue')}
                  className="flex items-center gap-1 px-2 py-1 rounded-full text-xs bg-purple-500/20 text-purple-400 hover:opacity-80 transition-opacity"
                  title={t('printers.queue.inQueue', { count: queueCount })}
                >
                  <Layers className="w-3 h-3" />
                  {queueCount}
                </button>
              )}
              {/* Firmware Version Badge */}
              {checkPrinterFirmware && firmwareInfo?.current_version && firmwareInfo?.latest_version ? (
                <button
                  onClick={() => setShowFirmwareModal(true)}
                  className={`flex items-center gap-1 px-2 py-1 rounded-full text-xs hover:opacity-80 transition-opacity ${
                    firmwareInfo.update_available
                      ? 'bg-orange-500/20 text-orange-400'
                      : 'bg-status-ok/20 text-status-ok'
                  }`}
                  title={
                    firmwareInfo.update_available
                      ? t('printers.firmwareUpdateAvailable', { current: firmwareInfo.current_version, latest: firmwareInfo.latest_version })
                      : t('printers.firmwareUpToDate', { version: firmwareInfo.current_version })
                  }
                >
                  {firmwareInfo.update_available ? <Download className="w-3 h-3" /> : <CheckCircle className="w-3 h-3" />}
                  {firmwareInfo.current_version}
                </button>
              ) : status?.firmware_version ? (
                <span className="flex items-center gap-1 px-2 py-1 rounded-full text-xs bg-bambu-dark-tertiary/50 text-bambu-gray">
                  {status.firmware_version}
                </span>
              ) : null}
            </div>
          )}
        </div>

        {/* Delete Confirmation */}
        {showDeleteConfirm && (
          <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
            <Card className="w-full max-w-md mx-4">
              <CardContent>
                <div className="flex items-start gap-3 mb-4">
                  <div className="p-2 rounded-full bg-red-500/20">
                    <AlertTriangle className="w-5 h-5 text-red-400" />
                  </div>
                  <div>
                    <h3 className="text-lg font-semibold text-white">{t('printers.confirm.deleteTitle')}</h3>
                    <p className="text-sm text-bambu-gray mt-1">
                      {t('printers.confirm.deleteMessage', { name: printer.name })}
                    </p>
                  </div>
                </div>

                <div className="bg-bambu-dark rounded-lg p-3 mb-4">
                  <label className="flex items-start gap-3 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={deleteArchives}
                      onChange={(e) => setDeleteArchives(e.target.checked)}
                      className="mt-0.5 w-4 h-4 rounded border-bambu-gray bg-bambu-dark-secondary text-bambu-green focus:ring-bambu-green focus:ring-offset-0"
                    />
                    <div>
                      <span className="text-sm text-white">{t('printers.deleteArchives')}</span>
                      <p className="text-xs text-bambu-gray mt-0.5">
                        {deleteArchives
                          ? t('printers.confirm.deleteArchivesNote')
                          : t('printers.confirm.keepArchivesNote')}
                      </p>
                    </div>
                  </label>
                </div>

                <div className="flex justify-end gap-2">
                  <Button
                    variant="secondary"
                    onClick={() => {
                      setShowDeleteConfirm(false);
                      setDeleteArchives(true);
                    }}
                  >
                    {t('common.cancel')}
                  </Button>
                  <Button
                    variant="danger"
                    onClick={() => {
                      deleteMutation.mutate({ deleteArchives });
                      setShowDeleteConfirm(false);
                      setDeleteArchives(true);
                    }}
                  >
                    Delete
                  </Button>
                </div>
              </CardContent>
            </Card>
          </div>
        )}

        {/* Status */}
        {status?.connected && (
          <>
            {/* Compact: Simple status bar */}
            {viewMode === 'compact' ? (
              <div className="mt-2">
                {(status.state === 'RUNNING' || status.state === 'PAUSE') ? (
                  <div className="flex items-center gap-2">
                    <div className="flex-1 bg-bambu-dark-tertiary rounded-full h-1.5">
                      <div
                        className={`${status.state === 'PAUSE' ? 'bg-status-warning' : 'bg-bambu-green'} h-1.5 rounded-full transition-all`}
                        style={{ width: `${status.progress || 0}%` }}
                      />
                    </div>
                    <div className="flex flex-shrink-0 items-center gap-1.5">
                      <span className="text-xs text-white">{Math.round(status.progress || 0)}%</span>
                      {plateStatusPill}
                    </div>
                  </div>
                ) : (
                  <div className="flex items-center justify-between gap-2">
                    <div className="min-w-0 flex-1 flex items-center gap-1.5">
                      <p className="min-w-0 truncate text-xs text-bambu-gray">{getStatusDisplay(status.state, status.stg_cur_name)}</p>
                      {plateStatusPill}
                    </div>
                    {showClearPlateButton && (
                      <button
                        type="button"
                        onClick={() => clearPlateMutation.mutate()}
                        disabled={clearPlateMutation.isPending || !hasPermission('printers:clear_plate')}
                        aria-label={t('printers.plateStatus.markCleared')}
                        className="inline-flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full bg-yellow-500/20 border border-yellow-400/40 text-yellow-400 hover:bg-yellow-500/30 transition-colors disabled:opacity-50"
                        title={!hasPermission('printers:clear_plate') ? t('printers.permission.noControl') : t('printers.plateStatus.markCleared')}
                      >
                        {clearPlateMutation.isPending ? (
                          <Loader2 className="w-3 h-3 animate-spin" />
                        ) : (
                          <PlateClearedIcon className="w-3 h-3" />
                        )}
                      </button>
                    )}
                  </div>
                )}
              </div>
            ) : (
              /* Expanded: Full status section */
              <>
                {/* Current Print or Idle Placeholder */}
                <div className="mb-4 p-3 bg-bambu-dark rounded-lg relative">
                  {/* Skip Objects button - top right corner, always visible */}
                  <button
                    onClick={() => setShowSkipObjectsModal(true)}
                    disabled={!(status.state === 'RUNNING' || status.state === 'PAUSE') || (status.printable_objects_count ?? 0) < 2 || !(status.skip_objects_supported ?? false) || !hasPermission('printers:control')}
                    className={`absolute top-2 right-2 p-1.5 rounded transition-colors z-10 ${
                      (status.state === 'RUNNING' || status.state === 'PAUSE') && (status.printable_objects_count ?? 0) >= 2 && (status.skip_objects_supported ?? false) && hasPermission('printers:control')
                        ? 'text-bambu-gray hover:text-white hover:bg-white/10'
                        : 'text-bambu-gray/30 cursor-not-allowed'
                    }`}
                    title={
                      !hasPermission('printers:control')
                        ? t('printers.permission.noControl')
                        : !(status.state === 'RUNNING' || status.state === 'PAUSE')
                          ? t('printers.skipObjects.onlyWhilePrinting')
                          : (status.printable_objects_count ?? 0) >= 2
                            ? t('printers.skipObjects.tooltip')
                            : t('printers.skipObjects.requiresMultiple')
                    }
                  >
                    <SkipObjectsIcon className="w-4 h-4" />
                    {/* Badge showing skipped count */}
                    {objectsData && objectsData.skipped_count > 0 && (
                      <span className="absolute -top-1 -right-1 min-w-[16px] h-4 px-1 flex items-center justify-center text-[10px] font-bold bg-red-500 text-white rounded-full">
                        {objectsData.skipped_count}
                      </span>
                    )}
                  </button>
                  <div className="flex gap-3">
                    {/* Cover Image */}
                    <CoverImage
                      url={(status.state === 'RUNNING' || status.state === 'PAUSE') ? status.cover_url : null}
                      printName={(status.state === 'RUNNING' || status.state === 'PAUSE') ? (formatPrintName(status.subtask_name || status.current_print, status.gcode_file, t, activePlateLabel) || undefined) : undefined}
                    />
                    {/* Print Info */}
                    <div className="flex-1 min-w-0">
                      {status.current_print && (status.state === 'RUNNING' || status.state === 'PAUSE') ? (
                        <>
                          <div className="mb-1 flex items-center gap-2">
                            <p className="text-sm text-bambu-gray">{getStatusDisplay(status.state, status.stg_cur_name)}</p>
                            {plateStatusPill}
                          </div>
                          <p className="text-white text-sm mb-2 truncate">
                            {formatPrintName(status.subtask_name || status.current_print, status.gcode_file, t, activePlateLabel)}
                          </p>
                          <div className="flex items-center justify-between text-sm">
                            <div className="flex-1 bg-bambu-dark-tertiary rounded-full h-2 mr-3">
                              <div
                                className={`${status.state === 'PAUSE' ? 'bg-status-warning' : 'bg-bambu-green'} h-2 rounded-full transition-all`}
                                style={{ width: `${status.progress || 0}%` }}
                              />
                            </div>
                            <span className="text-white">{Math.round(status.progress || 0)}%</span>
                          </div>
                          <div className="flex items-center gap-3 mt-2 text-xs text-bambu-gray">
                            {status.remaining_time != null && status.remaining_time > 0 && (
                              <>
                                <span className="flex items-center gap-1">
                                  <Clock className="w-3 h-3" />
                                  {formatDuration(status.remaining_time * 60)}
                                </span>
                                <span className="text-bambu-green font-medium" title={t('printers.estimatedCompletion')}>
                                  ETA {formatETA(status.remaining_time, timeFormat, t)}
                                </span>
                              </>
                            )}
                            {status.layer_num != null && status.total_layers != null && status.total_layers > 0 && (
                              <span className="flex items-center gap-1">
                                <Layers className="w-3 h-3" />
                                {status.layer_num}/{status.total_layers}
                              </span>
                            )}
                            {currentPrintUser && (
                              <span className="flex items-center gap-1" title={`Started by ${currentPrintUser}`}>
                                <User className="w-3 h-3" />
                                {currentPrintUser}
                              </span>
                            )}
                          </div>
                        </>
                      ) : (
                        <>
                          <p className="text-sm text-bambu-gray mb-1">{t('printers.sort.status')}</p>
                          <div className="mb-2 flex items-center gap-2">
                            <p className="text-white text-sm">
                              {getStatusDisplay(status.state, status.stg_cur_name)}
                            </p>
                            {plateStatusPill}
                          </div>
                          <div className="flex items-center justify-between text-sm">
                            <div className="flex-1 bg-bambu-dark-tertiary rounded-full h-2 mr-3">
                              <div className="bg-bambu-dark-tertiary h-2 rounded-full" />
                            </div>
                            <span className="text-bambu-gray">-</span>
                          </div>
                          {lastPrint ? (
                            <p className="text-xs text-bambu-gray mt-2 truncate" title={lastPrint.print_name || lastPrint.filename}>
                              Last: {lastPrint.print_name || lastPrint.filename}
                              {lastPrint.completed_at && (
                                <span className="ml-1 text-bambu-gray/60">
                                  • {formatDateOnly(lastPrint.completed_at, { month: 'short', day: 'numeric' }, dateFormat)}
                                </span>
                              )}
                            </p>
                          ) : (
                            <p className="text-xs text-bambu-gray mt-2">{t('printers.readyToPrint')}</p>
                          )}
                        </>
                      )}
                    </div>
                  </div>
                </div>

                {/* Queue Widget - always visible when there are pending items */}
                <PrinterQueueWidget printerId={printer.id} printerModel={printer.model} printerState={status.state} awaitingPlateClear={status.awaiting_plate_clear} requirePlateClear={printer.require_plate_clear} />
              </>
            )}

            {/* Temperatures */}
            {status.temperatures && viewMode === 'expanded' && (() => {
              // Use actual heater states from MQTT stream
              const nozzleHeating = status.temperatures.nozzle_heating || status.temperatures.nozzle_2_heating || false;
              const bedHeating = status.temperatures.bed_heating || false;
              const chamberHeating = status.temperatures.chamber_heating || false;
              const isDualNozzle = printer.nozzle_count === 2 || status.temperatures.nozzle_2 !== undefined;
              // active_extruder: 0=right, 1=left
              const activeNozzle = status.active_extruder === 1 ? 'L' : 'R';
              // Extended nozzle data from nozzle_rack (H2 series: wear, serial, max_temp, etc.)
              // nozzle_rack id 0 = extruder 0 = RIGHT, id 1 = extruder 1 = LEFT
              const leftNozzleSlot = status.nozzle_rack?.find(s => s.id === 1);
              const rightNozzleSlot = status.nozzle_rack?.find(s => s.id === 0);
              // Single-nozzle models (H2D, H2C): use the primary nozzle (id 0)
              const singleNozzleSlot = rightNozzleSlot || leftNozzleSlot;

              return (
                <div className="flex items-stretch gap-1.5 flex-wrap">
                  {/* Nozzle temp - combined for dual nozzle */}
                  <div className="text-center px-2 py-1.5 bg-bambu-dark rounded-lg flex-1 flex flex-col justify-center items-center">
                    <HeaterThermometer className="w-3.5 h-3.5 mb-0.5" color="text-orange-400" isHeating={nozzleHeating} />
                    {status.temperatures.nozzle_2 !== undefined ? (
                      <>
                        <p className="text-[9px] text-bambu-gray">L / R</p>
                        <p className="text-[11px] text-white">
                          {Math.round(status.temperatures.nozzle || 0)}° / {Math.round(status.temperatures.nozzle_2 || 0)}°
                        </p>
                      </>
                    ) : singleNozzleSlot ? (
                      <NozzleSlotHoverCard slot={singleNozzleSlot} index={0} activeStatus filamentName={singleNozzleSlot.filament_id ? filamentInfo?.[singleNozzleSlot.filament_id]?.name : undefined}>
                        <div className="cursor-default">
                          <p className="text-[9px] text-bambu-gray">{t('printers.temperatures.nozzle')}</p>
                          <p className="text-[11px] text-white">
                            {Math.round(status.temperatures.nozzle || 0)}°C
                          </p>
                        </div>
                      </NozzleSlotHoverCard>
                    ) : (
                      <>
                        <p className="text-[9px] text-bambu-gray">{t('printers.temperatures.nozzle')}</p>
                        <p className="text-[11px] text-white">
                          {Math.round(status.temperatures.nozzle || 0)}°C
                        </p>
                      </>
                    )}
                  </div>
                  <div className="text-center px-2 py-1.5 bg-bambu-dark rounded-lg flex-1 flex flex-col justify-center items-center">
                    <HeaterThermometer className="w-3.5 h-3.5 mb-0.5" color="text-blue-400" isHeating={bedHeating} />
                    <p className="text-[9px] text-bambu-gray">{t('printers.temperatures.bed')}</p>
                    <p className="text-[11px] text-white">
                      {Math.round(status.temperatures.bed || 0)}°C
                    </p>
                  </div>
                  {status.temperatures.chamber !== undefined && (
                    <div className="text-center px-2 py-1.5 bg-bambu-dark rounded-lg flex-1 flex flex-col justify-center items-center">
                      <HeaterThermometer className="w-3.5 h-3.5 mb-0.5" color="text-green-400" isHeating={chamberHeating} />
                      <p className="text-[9px] text-bambu-gray">{t('printers.temperatures.chamber')}</p>
                      <p className="text-[11px] text-white">
                        {Math.round(status.temperatures.chamber || 0)}°C
                      </p>
                    </div>
                  )}
                  {/* Active nozzle indicator for dual-nozzle printers */}
                  {isDualNozzle && (
                    <DualNozzleHoverCard
                      leftSlot={leftNozzleSlot}
                      rightSlot={rightNozzleSlot}
                      activeNozzle={activeNozzle}
                      filamentInfo={filamentInfo}
                    >
                      <div className="text-center px-3 py-1.5 bg-bambu-dark rounded-lg h-full flex flex-col justify-center items-center cursor-default" title={t('printers.activeNozzle', { nozzle: activeNozzle === 'L' ? t('common.left') : t('common.right') })}>
                        <NozzleIcon className="w-3.5 h-3.5 mb-0.5 text-amber-400" />
                        <div className="flex items-center gap-2">
                          <span className={`text-[11px] font-bold ${activeNozzle === 'L' ? 'text-amber-400' : 'text-gray-500'}`}>
                            L{leftNozzleSlot?.nozzle_diameter ? ` ${leftNozzleSlot.nozzle_diameter}` : ''}
                          </span>
                          <span className="text-[9px] text-bambu-gray/40">·</span>
                          <span className={`text-[11px] font-bold ${activeNozzle === 'R' ? 'text-amber-400' : 'text-gray-500'}`}>
                            R{rightNozzleSlot?.nozzle_diameter ? ` ${rightNozzleSlot.nozzle_diameter}` : ''}
                          </span>
                        </div>
                        <p className="text-[9px] text-bambu-gray">{t('printers.temperatures.nozzle')}</p>
                      </div>
                    </DualNozzleHoverCard>
                  )}
                  {/* H2C nozzle rack (tool-changer dock) - only show when rack nozzles exist (IDs >= 2) */}
                  {status.nozzle_rack && status.nozzle_rack.some(s => s.id >= 2) && (
                    <NozzleRackCard slots={status.nozzle_rack} filamentInfo={filamentInfo} />
                  )}
                </div>
              );
            })()}

            {viewMode === 'expanded' && showClearPlateButton && (
              <button
                type="button"
                onClick={() => clearPlateMutation.mutate()}
                disabled={clearPlateMutation.isPending || !hasPermission('printers:clear_plate')}
                className="mt-2 w-full inline-flex items-center justify-center gap-2 px-3 py-1.5 rounded-lg bg-yellow-500/20 border border-yellow-400/40 text-yellow-400 hover:bg-yellow-500/30 transition-colors text-xs font-medium disabled:opacity-50"
                title={!hasPermission('printers:clear_plate') ? t('printers.permission.noControl') : t('printers.plateStatus.markCleared')}
              >
                {clearPlateMutation.isPending ? (
                  <Loader2 className="w-3 h-3 animate-spin" />
                ) : (
                  <PlateClearedIcon className="w-4 h-4" />
                )}
                {t('printers.plateStatus.markCleared')}
              </button>
            )}

            {/* Controls - Fans + Print Buttons */}
            {viewMode === 'expanded' && (() => {
              // Determine print state for control buttons
              const isRunning = status.state === 'RUNNING';
              const isPaused = status.state === 'PAUSE';
              const isPrinting = isRunning || isPaused;
              const isControlBusy = stopPrintMutation.isPending || pausePrintMutation.isPending || resumePrintMutation.isPending;

              // Fan data
              const partFan = status.cooling_fan_speed;
              const auxFan = status.big_fan1_speed;
              const chamberFan = status.big_fan2_speed;

              return (
                <div className="mt-3">
                  {/* Section Header */}
                  <div className="flex items-center gap-2 mb-2">
                    <span className="text-[10px] uppercase tracking-wider text-bambu-gray font-medium">
                      {t('printers.controls')}
                    </span>
                    <div className="flex-1 h-px bg-bambu-dark-tertiary/30" />
                  </div>

                  <div className="flex flex-wrap items-start justify-between gap-x-2 gap-y-2">
                    {/* Left: Fan Status - always visible, dynamic coloring */}
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5 min-w-0">
                      {/* Part Cooling Fan */}
                      <div
                        className={`flex items-center gap-1 px-1.5 py-1 rounded ${partFan && partFan > 0 ? 'bg-cyan-500/10' : 'bg-bambu-dark'}`}
                        title={t('printers.fans.partCooling')}
                      >
                        <Fan className={`w-3.5 h-3.5 ${partFan && partFan > 0 ? 'text-cyan-400' : 'text-bambu-gray/50'}`} />
                        <span className={`text-[10px] ${partFan && partFan > 0 ? 'text-cyan-400' : 'text-bambu-gray/50'}`}>
                          {partFan ?? 0}%
                        </span>
                      </div>

                      {/* Auxiliary Fan */}
                      <div
                        className={`flex items-center gap-1 px-1.5 py-1 rounded ${auxFan && auxFan > 0 ? 'bg-blue-500/10' : 'bg-bambu-dark'}`}
                        title={t('printers.fans.auxiliary')}
                      >
                        <Wind className={`w-3.5 h-3.5 ${auxFan && auxFan > 0 ? 'text-blue-400' : 'text-bambu-gray/50'}`} />
                        <span className={`text-[10px] ${auxFan && auxFan > 0 ? 'text-blue-400' : 'text-bambu-gray/50'}`}>
                          {auxFan ?? 0}%
                        </span>
                      </div>

                      {/* Chamber Fan */}
                      <div
                        className={`flex items-center gap-1 px-1.5 py-1 rounded ${chamberFan && chamberFan > 0 ? 'bg-green-500/10' : 'bg-bambu-dark'}`}
                        title={t('printers.fans.chamber')}
                      >
                        <AirVent className={`w-3.5 h-3.5 ${chamberFan && chamberFan > 0 ? 'text-green-400' : 'text-bambu-gray/50'}`} />
                        <span className={`text-[10px] ${chamberFan && chamberFan > 0 ? 'text-green-400' : 'text-bambu-gray/50'}`}>
                          {chamberFan ?? 0}%
                        </span>
                      </div>

                      {/* Separator */}
                      <div className="w-px h-5 bg-bambu-gray/30" />

                      {/* Print Speed */}
                      {(() => {
                        const speedLabels: Record<number, string> = { 1: '50%', 2: '100%', 3: '124%', 4: '166%' };
                        const speedPct = speedLabels[status.speed_level] || '100%';
                        return (
                          <div className="relative">
                            <button
                              onClick={() => setShowSpeedMenu(showSpeedMenu === printer.id ? null : printer.id)}
                              disabled={!isPrinting || !hasPermission('printers:control')}
                              className={`flex items-center gap-1 px-1.5 py-1 rounded transition-colors ${
                                isPrinting
                                  ? 'bg-amber-500/10 hover:bg-amber-500/20'
                                  : 'bg-bambu-dark cursor-not-allowed'
                              }`}
                              title={isPrinting ? t('printers.speed.title') : undefined}
                            >
                              <Gauge className={`w-3.5 h-3.5 ${
                                isPrinting ? 'text-amber-400' : 'text-bambu-gray/50'
                              }`} />
                              <span className={`text-[10px] ${
                                isPrinting ? 'text-amber-400' : 'text-bambu-gray/50'
                              }`}>
                                {speedPct}
                              </span>
                            </button>
                            {showSpeedMenu === printer.id && (
                              <>
                                <div className="fixed inset-0 z-40" onClick={() => setShowSpeedMenu(null)} />
                                <div className="absolute bottom-full left-0 mb-1 z-50 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-lg py-1 min-w-[130px]">
                                  {([
                                    { mode: 1, label: t('printers.speed.silent') },
                                    { mode: 2, label: t('printers.speed.standard') },
                                    { mode: 3, label: t('printers.speed.sport') },
                                    { mode: 4, label: t('printers.speed.ludicrous') },
                                  ] as const).map(({ mode, label }) => (
                                    <button
                                      key={mode}
                                      onClick={() => {
                                        printSpeedMutation.mutate(mode);
                                        setShowSpeedMenu(null);
                                      }}
                                      className={`w-full text-left px-3 py-1.5 text-xs transition-colors ${
                                        status.speed_level === mode
                                          ? 'text-bambu-green bg-bambu-green/10'
                                          : 'text-white hover:bg-bambu-dark-tertiary'
                                      }`}
                                    >
                                      {label}
                                    </button>
                                  ))}
                                </div>
                              </>
                            )}
                          </div>
                        );
                      })()}

                      {/* Separator */}
                      <div className="w-px h-5 bg-bambu-gray/30" />

                      {/* Bed Jog (Z-axis) — compact badge, popover holds the actual controls.
                          When the printer isn't yet homed since finish, show a Studio-style
                          warning modal offering Home Z, Move Anyway, or Cancel. "Move anyway"
                          gets remembered per-printer in sessionStorage so the warning only
                          appears once per browser session. */}
                      {(() => {
                        const canControl = hasPermission('printers:control');
                        const disabled = isPrinting || !canControl;
                        const bambuIsPlateBelow = true; // positive Z moves plate away from nozzle
                        const requestJog = (direction: 1 | -1) => {
                          const signed = direction * bedJogStep * (bambuIsPlateBelow ? 1 : -1);
                          const warnedKey = `bamdude.bedJog.warned.${printer.id}`;
                          const warned = (() => {
                            try { return sessionStorage.getItem(warnedKey) === '1'; }
                            catch { return false; }
                          })();
                          setShowBedJogMenu(null);
                          if (warned) {
                            bedJogMutation.mutate({ distance: signed, force: true });
                          } else {
                            setShowNotHomedModal({ distance: signed });
                          }
                        };
                        return (
                          <div className="relative">
                            <button
                              onClick={() => setShowBedJogMenu(showBedJogMenu === printer.id ? null : printer.id)}
                              disabled={disabled}
                              className={`flex items-center gap-1 px-1.5 py-1 rounded transition-colors ${
                                disabled
                                  ? 'bg-bambu-dark cursor-not-allowed'
                                  : 'bg-indigo-500/10 hover:bg-indigo-500/20'
                              }`}
                              title={!canControl ? t('printers.permission.noControl') : isPrinting ? t('printers.bedJog.disabledWhilePrinting') : t('printers.bedJog.title')}
                            >
                              <MoveVertical className={`w-3.5 h-3.5 ${disabled ? 'text-bambu-gray/50' : 'text-indigo-400'}`} />
                              <span className={`text-[10px] ${disabled ? 'text-bambu-gray/50' : 'text-indigo-400'}`}>
                                {t('printers.bedJog.bed')}
                              </span>
                              <span className={`text-[10px] tabular-nums opacity-70 ${disabled ? 'text-bambu-gray/50' : 'text-indigo-400'}`}>
                                {bedJogStep}mm
                              </span>
                            </button>
                            {showBedJogMenu === printer.id && (
                              <>
                                <div className="fixed inset-0 z-40" onClick={() => setShowBedJogMenu(null)} />
                                <div className="absolute bottom-full left-0 mb-1 z-50 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-lg p-2 min-w-[140px]">
                                  <div className="flex items-center justify-between gap-1 mb-2">
                                    <button
                                      onClick={() => requestJog(-1)}
                                      className="flex-1 flex items-center justify-center py-1.5 rounded bg-indigo-500/15 hover:bg-indigo-500/30 text-indigo-300"
                                      aria-label={t('printers.bedJog.up')}
                                    >
                                      <ArrowUp className="w-4 h-4" />
                                    </button>
                                    <button
                                      onClick={() => requestJog(1)}
                                      className="flex-1 flex items-center justify-center py-1.5 rounded bg-indigo-500/15 hover:bg-indigo-500/30 text-indigo-300"
                                      aria-label={t('printers.bedJog.down')}
                                    >
                                      <ArrowDown className="w-4 h-4" />
                                    </button>
                                  </div>
                                  <div className="text-[9px] uppercase tracking-wider text-bambu-gray/70 px-1 mb-1">
                                    {t('printers.bedJog.step')}
                                  </div>
                                  <div className="flex gap-1">
                                    {[1, 10, 50].map((step) => (
                                      <button
                                        key={step}
                                        onClick={() => setBedJogStep(step)}
                                        className={`flex-1 px-1 py-1 rounded text-[10px] transition-colors ${
                                          bedJogStep === step
                                            ? 'bg-bambu-green/20 text-bambu-green'
                                            : 'bg-bambu-dark text-bambu-gray hover:bg-bambu-dark-tertiary'
                                        }`}
                                      >
                                        {step}
                                      </button>
                                    ))}
                                  </div>
                                </div>
                              </>
                            )}
                          </div>
                        );
                      })()}

                    </div>

                    {/* Right: Print Control Buttons */}
                    <div className="flex items-center gap-2 flex-shrink-0 max-[550px]:self-start">
                      {/* Stop button */}
                      <button
                        onClick={() => setShowStopConfirm(true)}
                        disabled={!isPrinting || isControlBusy || !hasPermission('printers:control')}
                        className={`
                          flex items-center justify-center gap-1 px-3 py-1.5 rounded-lg text-xs font-medium
                          transition-colors
                          ${isPrinting && hasPermission('printers:control')
                            ? 'bg-red-500/20 text-red-400 hover:bg-red-500/30'
                            : 'bg-bambu-dark text-bambu-gray/50 cursor-not-allowed'
                          }
                        `}
                        title={!hasPermission('printers:control') ? t('printers.permission.noControl') : t('printers.stop')}
                      >
                        <Square className="w-3 h-3" />
                        {t('printers.stop')}
                      </button>

                      {/* Pause/Resume button */}
                      <button
                        onClick={() => isPaused ? setShowResumeConfirm(true) : setShowPauseConfirm(true)}
                        disabled={!isPrinting || isControlBusy || !hasPermission('printers:control')}
                        className={`
                          flex items-center justify-center gap-1 px-3 py-1.5 rounded-lg text-xs font-medium
                          transition-colors
                          ${isPrinting && hasPermission('printers:control')
                            ? isPaused
                              ? 'bg-bambu-green/20 text-bambu-green hover:bg-bambu-green/30'
                              : 'bg-yellow-500/20 text-yellow-400 hover:bg-yellow-500/30'
                            : 'bg-bambu-dark text-bambu-gray/50 cursor-not-allowed'
                          }
                        `}
                        title={!hasPermission('printers:control') ? t('printers.permission.noControl') : (isPaused ? t('printers.resume') : t('printers.pause'))}
                      >
                        {isPaused ? <Play className="w-3 h-3" /> : <Pause className="w-3 h-3" />}
                        {isPaused ? t('printers.resume') : t('printers.pause')}
                      </button>
                    </div>
                  </div>
                </div>
              );
            })()}

            {/* AMS Units - 2-Column Grid Layout */}
            {(amsData?.length > 0 || status.vt_tray.length > 0) && viewMode === 'expanded' && (() => {
              // Separate regular AMS (4-tray) from HT AMS (1-tray)
              const regularAms = amsData.filter(ams => ams.tray.length > 1);
              const htAms = amsData.filter(ams => ams.tray.length === 1);
              const isDualNozzle = printer.nozzle_count === 2 || status?.temperatures?.nozzle_2 !== undefined;

              return (
                <div className="mt-3">
                  {/* Section Header */}
                  <div className="flex items-center gap-2 mb-2">
                    <span className="text-[10px] uppercase tracking-wider text-bambu-gray font-medium">
                      {t('printers.filaments')}
                    </span>
                    <div className="flex-1 h-px bg-bambu-dark-tertiary/30" />
                  </div>

                  {/* AMS Content */}
                  <div className="space-y-3">
                    {/* Row 1-2: Regular AMS (4-tray) in 2-column grid */}
                    {regularAms.length > 0 && (
                      <div className="grid grid-cols-2 gap-3">
                        {regularAms.map((ams) => {
                        const mappedExtruderId = amsExtruderMap[String(ams.id)];
                        const normalizedId = ams.id >= 128 ? ams.id - 128 : ams.id;
                        const extruderId = mappedExtruderId !== undefined ? mappedExtruderId : normalizedId;
                        const isLeftNozzle = extruderId === 1;
                        const isRightNozzle = extruderId === 0;

                        return (
                          <div key={ams.id} className="p-2.5 bg-bambu-dark rounded-lg border border-bambu-dark-tertiary/30">
                            {/* Header: Label + Stats (no icon) */}
                            <div className="flex items-center justify-between mb-2">
                              <div className="flex items-center gap-1.5">
                                {/* AMS name - hover to see serial, firmware, and edit friendly name */}
                                <AmsNameHoverCard
                                  ams={ams}
                                  printerId={printer.id}
                                  label={getAmsLabel(ams.id, ams.tray.length)}
                                  amsLabels={amsLabels}
                                  canEdit={hasPermission('printers:update')}
                                  onSaved={refetchAmsLabels}
                                >
                                  <span className="text-[10px] text-white font-medium cursor-default select-none">
                                    {amsLabels?.[ams.id] || getAmsLabel(ams.id, ams.tray.length)}
                                  </span>
                                </AmsNameHoverCard>
                                {isDualNozzle && (isLeftNozzle || isRightNozzle) && (
                                  <NozzleBadge side={isLeftNozzle ? 'L' : 'R'} />
                                )}
                              </div>
                              {(ams.humidity != null || ams.temp != null) && (
                                <div className="flex items-center gap-1.5 max-[550px]:flex-col max-[550px]:items-start">
                                  {ams.humidity != null && (
                                    <HumidityIndicator
                                      humidity={ams.humidity}
                                      goodThreshold={amsThresholds?.humidityGood}
                                      fairThreshold={amsThresholds?.humidityFair}
                                      onClick={() => setAmsHistoryModal({
                                        amsId: ams.id,
                                        amsLabel: getAmsLabel(ams.id, ams.tray.length),
                                        mode: 'humidity',
                                      })}
                                      compact
                                    />
                                  )}
                                  {ams.temp != null && (
                                    <TemperatureIndicator
                                      temp={ams.temp}
                                      goodThreshold={amsThresholds?.tempGood}
                                      fairThreshold={amsThresholds?.tempFair}
                                      onClick={() => setAmsHistoryModal({
                                        amsId: ams.id,
                                        amsLabel: getAmsLabel(ams.id, ams.tray.length),
                                        mode: 'temperature',
                                      })}
                                      compact
                                    />
                                  )}
                                  {/* Drying button - only for AMS 2 Pro (n3f) and AMS-HT (n3s) */}
                                  {status.supports_drying && (ams.module_type === 'n3f' || ams.module_type === 'n3s') && hasPermission('printers:control') && (
                                    <button
                                      disabled={!!(ams.dry_sf_reason?.length && ams.dry_time === 0)}
                                      onClick={(e) => {
                                        if (ams.dry_time > 0) {
                                          stopDryingMutation.mutate(ams.id);
                                        } else if (dryingPopoverAmsId === ams.id) {
                                          setDryingPopoverAmsId(null);
                                        } else {
                                          const firstTray = ams.tray.find(t => t.tray_type);
                                          const filType = (firstTray?.tray_type || 'PLA').split(' ')[0].toUpperCase();
                                          const preset = dryingPresets[filType] || dryingPresets['PLA'];
                                          const moduleType = ams.module_type as 'n3f' | 'n3s';
                                          setDryingFilament(filType);
                                          setDryingTemp(preset[moduleType] || preset.n3f);
                                          setDryingDuration(moduleType === 'n3s' ? preset.n3s_hours : preset.n3f_hours);
                                          setDryingRotateTray(false);
                                          setDryingPopoverModuleType(ams.module_type);
                                          setDryingPopoverAmsId(ams.id);
                                          const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
                                          setDryingPopoverPos({ top: rect.bottom + 4, left: Math.max(8, rect.right - 240) });
                                        }
                                      }}
                                      className={`flex items-center gap-0.5 px-1 py-0.5 rounded text-[9px] transition-colors ${
                                        ams.dry_time > 0
                                          ? 'bg-amber-500/20 text-amber-400'
                                          : ams.dry_sf_reason?.length
                                            ? 'bg-bambu-dark-tertiary/30 text-bambu-gray/50 cursor-not-allowed'
                                            : 'bg-bambu-dark-tertiary/50 text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary'
                                      }`}
                                      title={ams.dry_time > 0 ? t('printers.drying.stop') : ams.dry_sf_reason?.length ? t('printers.drying.powerRequired') : t('printers.drying.start')}
                                    >
                                      <Flame className="w-3 h-3" />
                                    </button>
                                  )}
                                </div>
                              )}
                            </div>
                            {/* Drying status bar */}
                            {ams.dry_time > 0 && (
                              <div className="flex items-center gap-2 px-2 py-1 mb-1 bg-amber-500/10 border border-amber-500/20 rounded text-[9px]">
                                <Flame className="w-3 h-3 text-amber-400 shrink-0" />
                                <span className="text-amber-400 font-medium">{t('printers.drying.active')}</span>
                                <span className="text-amber-300/70">
                                  {t('printers.drying.timeRemaining', {
                                    time: ams.dry_time >= 60
                                      ? `${Math.floor(ams.dry_time / 60)}h ${ams.dry_time % 60}m`
                                      : `${ams.dry_time}m`
                                  })}
                                </span>
                                <button
                                  onClick={() => stopDryingMutation.mutate(ams.id)}
                                  disabled={stopDryingMutation.isPending}
                                  className="ml-auto text-amber-400 hover:text-amber-300 transition-colors disabled:opacity-50"
                                  title={t('printers.drying.stop')}
                                >
                                  <X className="w-3 h-3" />
                                </button>
                              </div>
                            )}
                            {/* Slots grid: 4 columns - always render 4 slots */}
                            <div className="grid grid-cols-4 gap-1.5">
                              {[0, 1, 2, 3].map((slotIdx) => {
                                // Find tray data for this slot (may be undefined if data incomplete)
                                // Use array index if available, as tray.id may not always be set
                                const tray = ams.tray[slotIdx] || ams.tray.find(t => t.id === slotIdx);
                                const hasFillLevel = tray?.tray_type && tray.remain >= 0;
                                const isEmpty = !tray?.tray_type;
                                // Check if this is the currently loaded tray
                                // Global tray ID = ams.id * 4 + slot index (for standard AMS)
                                const globalTrayId = ams.id * 4 + slotIdx;
                                const isActive = effectiveTrayNow === globalTrayId;
                                // Get cloud preset info if available
                                const cloudInfo = tray?.tray_info_idx ? filamentInfo?.[tray.tray_info_idx] : null;
                                // Get saved slot preset mapping (for user-configured slots)
                                const slotPreset = slotPresets?.[globalTrayId];

                                // Fill level fallback chain: Spoolman link → Spoolman slot-assignment → Inventory → AMS remain
                                const trayTag = (tray?.tray_uuid || tray?.tag_uid || getFallbackSpoolTag(printer.serial_number, ams.id, slotIdx))?.toUpperCase();
                                const linkedSpool = trayTag ? linkedSpools?.[trayTag] : undefined;
                                const spoolmanFill = getSpoolmanFillLevel(linkedSpool);
                                // Slot-assigned-only spool (no tag link required) — upstream PR #1241.
                                // When the operator explicitly assigned a Spoolman spool to this
                                // (printer, ams, tray) via spoolman_slot_assignments, we resolve the
                                // full spool record so hover card / fill bar / preset name reflect it.
                                const slotAssignmentForFill = spoolmanEnabled && !spoolmanLoading
                                  ? spoolmanSlotAssignments?.find((a) => a.printer_id === printer.id && a.ams_id === ams.id && a.tray_id === slotIdx)
                                  : undefined;
                                const slotSpoolForFill = slotAssignmentForFill
                                  ? spoolmanSpools?.find((s) => s.id === slotAssignmentForFill.spoolman_spool_id)
                                  : undefined;
                                const slotSpoolFill = (slotSpoolForFill && (slotSpoolForFill.label_weight ?? 0) > 0)
                                  ? Math.round(Math.max(0, (slotSpoolForFill.label_weight ?? 0) - slotSpoolForFill.weight_used) / (slotSpoolForFill.label_weight ?? 1) * 100)
                                  : null;
                                const inventoryAssignment = onGetAssignment?.(printer.id, ams.id, slotIdx);
                                const inventoryFill = (() => {
                                  const sp = inventoryAssignment?.spool;
                                  if (sp && sp.label_weight > 0 && sp.weight_used != null) {
                                    return Math.round(Math.max(0, sp.label_weight - sp.weight_used) / sp.label_weight * 100);
                                  }
                                  return null;
                                })();
                                // If inventory says 0% but AMS reports positive remain, prefer AMS
                                // (inventory weight_used may be stale or over-counted - #676)
                                const resolvedInventoryFill = (inventoryFill === 0 && hasFillLevel && tray.remain > 0)
                                  ? null : inventoryFill;
                                const effectiveFill = spoolmanFill ?? slotSpoolFill ?? resolvedInventoryFill ?? (hasFillLevel ? tray.remain : null);
                                const fillSource = (spoolmanFill !== null || slotSpoolFill !== null) ? 'spoolman' as const
                                  : resolvedInventoryFill !== null ? 'inventory' as const
                                  : hasFillLevel ? 'ams' as const
                                  : undefined;

                                // Build filament data for hover card
                                const filamentData = tray?.tray_type ? {
                                  vendor: (isBambuLabSpool(tray) ? 'Bambu Lab' : 'Generic') as 'Bambu Lab' | 'Generic',
                                  // Spoolman spool name wins over cloud lookup so a slot bound to a
                                  // Spoolman spool shows that spool's preset name (e.g. "Devil
                                  // Design PLA") instead of whatever the printer's filament_id
                                  // resolves to (often "Generic PLA" for P-prefix local presets).
                                  // Spoolman's filament.name is just the material+subtype
                                  // ("PLA Basic"); prepend the spool's brand so the hover card
                                  // shows "Devil Design PLA Basic" rather than the vendor-less
                                  // form. Strip the "@<printer>..." suffix that BambuStudio
                                  // appends to user-preset names.
                                  profile: slotPreset?.preset_name
                                    || (slotSpoolForFill ? [slotSpoolForFill.brand, slotSpoolForFill.slicer_filament_name?.split('@')[0].trim() || slotSpoolForFill.material].filter(Boolean).join(' ').trim() : null)
                                    || inventoryAssignment?.spool?.slicer_filament_name
                                    || cloudInfo?.name
                                    || tray.tray_sub_brands
                                    || tray.tray_type,
                                  colorName: getColorName(tray.tray_color || ''),
                                  colorHex: tray.tray_color || null,
                                  kFactor: formatKValue(tray.k),
                                  fillLevel: effectiveFill,
                                  trayUuid: tray.tray_uuid || null,
                                  tagUid: tray.tag_uid || null,
                                  fillSource,
                                } : null;

                                // Check if this specific slot is being refreshed
                                const isRefreshing = refreshingSlot?.amsId === ams.id &&
                                  refreshingSlot?.slotId === slotIdx;

                                // Slot visual content (goes inside hover card)
                                const slotVisual = (
                                  <div
                                    className={`bg-bambu-dark-tertiary rounded p-1 text-center ${isEmpty ? 'opacity-50' : ''} ${isActive ? 'ring-2 ring-bambu-green ring-offset-1 ring-offset-bambu-dark' : ''}`}
                                  >
                                    {/* Filament color circle with 1-based slot number centered inside */}
                                    <FilamentSlotCircle
                                      trayColor={tray?.tray_color}
                                      trayType={tray?.tray_type}
                                      isEmpty={isEmpty}
                                      slotNumber={slotIdx + 1}
                                    />
                                    <div className="text-[9px] text-white font-bold truncate">
                                      {tray?.tray_type || '-'}
                                    </div>
                                    {/* Fill bar */}
                                    <div className="mt-1 h-1.5 bg-black/30 rounded-full overflow-hidden">
                                      {effectiveFill !== null && effectiveFill >= 0 && !isEmpty && tray && (
                                        <div
                                          className="h-full rounded-full transition-all"
                                          style={{
                                            width: `${effectiveFill}%`,
                                            backgroundColor: getFillBarColor(effectiveFill),
                                          }}
                                        />
                                      )}
                                    </div>
                                  </div>
                                );

                                // Wrapper with menu button, dropdown, and loading overlay (outside hover card)
                                return (
                                  <div key={slotIdx} className="relative group">
                                    {/* Loading overlay during RFID re-read */}
                                    {isRefreshing && (
                                      <div className="absolute inset-0 bg-bambu-dark-tertiary/80 rounded flex items-center justify-center z-20">
                                        <RefreshCw className="w-4 h-4 text-bambu-green animate-spin" />
                                      </div>
                                    )}
                                    {/* Menu button - appears on hover, hidden when printer busy */}
                                    {status?.state !== 'RUNNING' && (
                                      <button
                                        onClick={(e) => {
                                          e.stopPropagation();
                                          setAmsSlotMenu(
                                            amsSlotMenu?.amsId === ams.id && amsSlotMenu?.slotId === slotIdx
                                              ? null
                                              : { amsId: ams.id, slotId: slotIdx }
                                          );
                                        }}
                                        className="absolute -top-1 -right-1 w-4 h-4 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity z-10 hover:bg-bambu-dark-tertiary"
                                        title={t('printers.slotOptions')}
                                      >
                                        <MoreVertical className="w-2.5 h-2.5 text-bambu-gray" />
                                      </button>
                                    )}
                                    {/* Dropdown menu */}
                                    {status?.state !== 'RUNNING' && amsSlotMenu?.amsId === ams.id && amsSlotMenu?.slotId === slotIdx && (
                                      <div className="absolute top-full left-0 mt-1 z-50 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl py-1 min-w-[140px]">
                                        <button
                                          className={`w-full px-3 py-1.5 text-left text-xs flex items-center gap-2 ${
                                            hasPermission('printers:ams_rfid')
                                              ? 'text-white hover:bg-bambu-dark-tertiary'
                                              : 'text-bambu-gray/50 cursor-not-allowed'
                                          }`}
                                          onClick={(e) => {
                                            e.stopPropagation();
                                            if (!hasPermission('printers:ams_rfid')) return;
                                            refreshAmsSlotMutation.mutate({ amsId: ams.id, slotId: slotIdx });
                                            setAmsSlotMenu(null);
                                          }}
                                          disabled={isRefreshing || !hasPermission('printers:ams_rfid')}
                                          title={!hasPermission('printers:ams_rfid') ? t('printers.permission.noAmsRfid') : undefined}
                                        >
                                          <RefreshCw className={`w-3 h-3 ${isRefreshing ? 'animate-spin' : ''}`} />
                                          {t('printers.rfid.reread')}
                                        </button>
                                        <button
                                          className={`w-full px-3 py-1.5 text-left text-xs flex items-center gap-2 ${
                                            hasPermission('printers:control')
                                              ? 'text-white hover:bg-bambu-dark-tertiary'
                                              : 'text-bambu-gray/50 cursor-not-allowed'
                                          }`}
                                          onClick={(e) => {
                                            e.stopPropagation();
                                            if (!hasPermission('printers:control')) return;
                                            amsLoadMutation.mutate(ams.id * 4 + slotIdx);
                                            setAmsSlotMenu(null);
                                          }}
                                          disabled={amsLoadMutation.isPending || !hasPermission('printers:control')}
                                          title={!hasPermission('printers:control') ? t('printers.permission.noControl') : undefined}
                                        >
                                          <ArrowDownToLine className="w-3 h-3" />
                                          {amsLoadMutation.isPending ? t('printers.ams.loading') : t('printers.ams.load')}
                                        </button>
                                        <button
                                          className={`w-full px-3 py-1.5 text-left text-xs flex items-center gap-2 ${
                                            hasPermission('printers:control')
                                              ? 'text-white hover:bg-bambu-dark-tertiary'
                                              : 'text-bambu-gray/50 cursor-not-allowed'
                                          }`}
                                          onClick={(e) => {
                                            e.stopPropagation();
                                            if (!hasPermission('printers:control')) return;
                                            amsUnloadMutation.mutate();
                                            setAmsSlotMenu(null);
                                          }}
                                          disabled={amsUnloadMutation.isPending || !hasPermission('printers:control')}
                                          title={!hasPermission('printers:control') ? t('printers.permission.noControl') : undefined}
                                        >
                                          <ArrowUpFromLine className="w-3 h-3" />
                                          {amsUnloadMutation.isPending ? t('printers.ams.unloading') : t('printers.ams.unload')}
                                        </button>
                                      </div>
                                    )}
                                    {/* Hover card wraps only the visual content */}
                                    {filamentData ? (
                                      <FilamentHoverCard
                                        data={filamentData}
                                        spoolman={{
                                          enabled: spoolmanEnabled,
                                          linkedSpoolId: (trayTag ? linkedSpools?.[trayTag]?.id : undefined)
                                            ?? slotAssignmentForFill?.spoolman_spool_id,
                                          spoolmanUrl,
                                          syncMode: spoolmanSyncMode,
                                          // Suppress Link button when slot is already occupied by ANY assignment
                                          // (Spoolman SlotAssignment OR local SpoolAssignment) — upstream PR #1241.
                                          onLinkSpool: (spoolmanEnabled && !slotAssignmentForFill && !inventoryAssignment) ? () => {
                                            const linkTag = (filamentData.trayUuid || filamentData.tagUid || getFallbackSpoolTag(printer.serial_number, ams.id, slotIdx)).toUpperCase();
                                            setLinkSpoolModal({
                                              tagUid: filamentData.tagUid || linkTag,
                                              trayUuid: filamentData.trayUuid || '',
                                              printerId: printer.id,
                                              amsId: ams.id,
                                              trayId: slotIdx,
                                            });
                                          } : undefined,
                                          onUnlinkSpool: linkedSpool?.id ? () => unlinkSpoolMutation.mutate(linkedSpool.id) : undefined,
                                        }}
                                        inventory={(() => {
                                          // Spoolman-mode inventory branch (upstream PR #1241): the slot's
                                          // bound Spoolman spool drives the hover-card "Assigned spool"
                                          // pill + Assign/Unassign buttons. Falls through to the local
                                          // SpoolAssignment branch when Spoolman is not configured.
                                          if (spoolmanEnabled) {
                                            if (spoolmanLoading) return undefined;
                                            const spoolmanSpool = slotSpoolForFill;
                                            return {
                                              assignedSpool: spoolmanSpool ? {
                                                id: spoolmanSpool.id,
                                                material: spoolmanSpool.material,
                                                brand: spoolmanSpool.brand ?? null,
                                                color_name: spoolmanSpool.color_name ?? null,
                                                remainingWeightGrams: spoolmanSpool.label_weight
                                                  ? Math.max(0, Math.round(spoolmanSpool.label_weight - spoolmanSpool.weight_used))
                                                  : undefined,
                                                displayName: formatSpoolDisplayName(spoolmanSpool, effectiveSpoolTemplate),
                                              } : null,
                                              onAssignSpool: () => setAssignSpoolModal({
                                                printerId: printer.id,
                                                amsId: ams.id,
                                                trayId: slotIdx,
                                                trayInfo: {
                                                  type: tray?.tray_type || filamentData.profile,
                                                  material: tray?.tray_type ?? undefined,
                                                  profile: filamentData.profile,
                                                  color: filamentData.colorHex || '',
                                                  location: `${getAmsLabel(ams.id, ams.tray.length)} Slot ${slotIdx + 1}`,
                                                },
                                              }),
                                              onUnassignSpool: spoolmanSpool ? () => onUnassignSpoolmanSpool?.(spoolmanSpool.id) : undefined,
                                            };
                                          }
                                          const assignment = onGetAssignment?.(printer.id, ams.id, slotIdx);
                                          return {
                                            assignedSpool: assignment?.spool ? {
                                              id: assignment.spool.id,
                                              material: assignment.spool.material,
                                              brand: assignment.spool.brand,
                                              color_name: assignment.spool.color_name,
                                              remainingWeightGrams: Math.max(0, Math.round(assignment.spool.label_weight - assignment.spool.weight_used)),
                                              displayName: formatSpoolDisplayName(assignment.spool, effectiveSpoolTemplate),
                                            } : null,
                                            onAssignSpool: filamentData.vendor !== 'Bambu Lab' ? () => setAssignSpoolModal({
                                              printerId: printer.id,
                                              amsId: ams.id,
                                              trayId: slotIdx,
                                              trayInfo: {
                                                type: tray?.tray_type || filamentData.profile,
                                                material: tray?.tray_type ?? undefined,
                                                profile: filamentData.profile,
                                                color: filamentData.colorHex || '',
                                                location: `${getAmsLabel(ams.id, ams.tray.length)} Slot ${slotIdx + 1}`,
                                              },
                                            }) : undefined,
                                            onUnassignSpool: assignment && filamentData.vendor !== 'Bambu Lab' ? () => onUnassignSpool?.(printer.id, ams.id, slotIdx) : undefined,
                                          };
                                        })()}
                                        configureSlot={{
                                          enabled: hasPermission('printers:control'),
                                          onConfigure: () => setConfigureSlotModal({
                                            amsId: ams.id,
                                            trayId: slotIdx,
                                            trayCount: ams.tray.length,
                                            trayType: tray?.tray_type || undefined,
                                            trayColor: tray?.tray_color || undefined,
                                            traySubBrands: tray?.tray_sub_brands || undefined,
                                            trayInfoIdx: tray?.tray_info_idx || undefined,
                                            extruderId: mappedExtruderId,
                                            caliIdx: tray?.cali_idx,
                                            savedPresetId: slotPreset?.preset_id,
                                          }),
                                        }}
                                      >
                                        {slotVisual}
                                      </FilamentHoverCard>
                                    ) : (
                                      <EmptySlotHoverCard
                                        configureSlot={{
                                          enabled: hasPermission('printers:control'),
                                          onConfigure: () => setConfigureSlotModal({
                                            amsId: ams.id,
                                            trayId: slotIdx,
                                            trayCount: ams.tray.length,
                                            extruderId: mappedExtruderId,
                                          }),
                                        }}
                                      >
                                        {slotVisual}
                                      </EmptySlotHoverCard>
                                    )}
                                  </div>
                                );
                              })}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}

                    {/* Row 3: HT AMS + External spools (same style as regular AMS, 4 across) */}
                    {(htAms.length > 0 || status.vt_tray.length > 0) && (
                      <div className="grid grid-cols-4 gap-3">
                      {/* HT AMS units - name/badge top, slot left, stats right */}
                      {htAms.map((ams) => {
                        const mappedExtruderId = amsExtruderMap[String(ams.id)];
                        const normalizedId = ams.id >= 128 ? ams.id - 128 : ams.id;
                        const extruderId = mappedExtruderId !== undefined ? mappedExtruderId : normalizedId;
                        const isLeftNozzle = extruderId === 1;
                        const isRightNozzle = extruderId === 0;
                        const tray = ams.tray[0];
                        const hasFillLevel = tray?.tray_type && tray.remain >= 0;
                        const isEmpty = !tray?.tray_type;
                        // Check if this is the currently loaded tray
                        const globalTrayId = getGlobalTrayId(ams.id, tray?.id ?? 0, false);
                        const isActive = effectiveTrayNow === globalTrayId;
                        // Get cloud preset info if available
                        const cloudInfo = tray?.tray_info_idx ? filamentInfo?.[tray.tray_info_idx] : null;
                        // Get saved slot preset mapping (for user-configured slots)
                        const slotPreset = slotPresets?.[globalTrayId];
                        const htSlotId = tray?.id ?? 0;

                        // Fill level fallback chain: Spoolman → Inventory → AMS remain
                        const htTrayTag = (tray?.tray_uuid || tray?.tag_uid || getFallbackSpoolTag(printer.serial_number, ams.id, htSlotId))?.toUpperCase();
                        const htLinkedSpool = htTrayTag ? linkedSpools?.[htTrayTag] : undefined;
                        const htSpoolmanFill = getSpoolmanFillLevel(htLinkedSpool);
                        // Slot-assigned-only spool (upstream PR #1241) — same shape as the regular AMS branch.
                        const htSlotAssignmentForFill = spoolmanEnabled && !spoolmanLoading
                          ? spoolmanSlotAssignments?.find((a) => a.printer_id === printer.id && a.ams_id === ams.id && a.tray_id === htSlotId)
                          : undefined;
                        const htSlotSpoolForFill = htSlotAssignmentForFill
                          ? spoolmanSpools?.find((s) => s.id === htSlotAssignmentForFill.spoolman_spool_id)
                          : undefined;
                        const htSlotSpoolFill = (htSlotSpoolForFill && (htSlotSpoolForFill.label_weight ?? 0) > 0)
                          ? Math.round(Math.max(0, (htSlotSpoolForFill.label_weight ?? 0) - htSlotSpoolForFill.weight_used) / (htSlotSpoolForFill.label_weight ?? 1) * 100)
                          : null;
                        const htInventoryAssignment = onGetAssignment?.(printer.id, ams.id, htSlotId);
                        const htInventoryFill = (() => {
                          const sp = htInventoryAssignment?.spool;
                          if (sp && sp.label_weight > 0 && sp.weight_used != null) {
                            return Math.round(Math.max(0, sp.label_weight - sp.weight_used) / sp.label_weight * 100);
                          }
                          return null;
                        })();
                        // If inventory says 0% but AMS reports positive remain, prefer AMS (#676)
                        const htResolvedInventoryFill = (htInventoryFill === 0 && hasFillLevel && tray.remain > 0)
                          ? null : htInventoryFill;
                        const htEffectiveFill = htSpoolmanFill ?? htSlotSpoolFill ?? htResolvedInventoryFill ?? (hasFillLevel ? tray.remain : null);
                        const htFillSource = (htSpoolmanFill !== null || htSlotSpoolFill !== null) ? 'spoolman' as const
                          : htResolvedInventoryFill !== null ? 'inventory' as const
                          : hasFillLevel ? 'ams' as const
                          : undefined;

                        // Build filament data for hover card
                        const filamentData = tray?.tray_type ? {
                          vendor: (isBambuLabSpool(tray) ? 'Bambu Lab' : 'Generic') as 'Bambu Lab' | 'Generic',
                          profile: slotPreset?.preset_name
                            || (htSlotSpoolForFill ? [htSlotSpoolForFill.brand, htSlotSpoolForFill.slicer_filament_name?.split('@')[0].trim() || htSlotSpoolForFill.material].filter(Boolean).join(' ').trim() : null)
                            || htInventoryAssignment?.spool?.slicer_filament_name
                            || cloudInfo?.name
                            || tray.tray_sub_brands
                            || tray.tray_type,
                          colorName: getColorName(tray.tray_color || ''),
                          colorHex: tray.tray_color || null,
                          kFactor: formatKValue(tray.k),
                          fillLevel: htEffectiveFill,
                          trayUuid: tray.tray_uuid || null,
                          tagUid: tray.tag_uid || null,
                          fillSource: htFillSource,
                        } : null;

                        // Check if this specific slot is being refreshed
                        const isHtRefreshing = refreshingSlot?.amsId === ams.id &&
                          refreshingSlot?.slotId === htSlotId;

                        // Slot visual content (goes inside hover card)
                        const slotVisual = (
                          <div
                            className={`bg-bambu-dark-tertiary rounded p-1 text-center ${isEmpty ? 'opacity-50' : ''} ${isActive ? 'ring-2 ring-bambu-green ring-offset-1 ring-offset-bambu-dark' : ''}`}
                          >
                            {/* Filament color circle with 1-based slot number centered inside */}
                            <FilamentSlotCircle
                              trayColor={tray?.tray_color}
                              trayType={tray?.tray_type}
                              isEmpty={isEmpty}
                              slotNumber={1}
                            />
                            <div className="text-[9px] text-white font-bold truncate">
                              {tray?.tray_type || '-'}
                            </div>
                            {/* Fill bar */}
                            <div className="mt-1 h-1.5 bg-black/30 rounded-full overflow-hidden">
                              {htEffectiveFill !== null && htEffectiveFill >= 0 && !isEmpty && (
                                <div
                                  className="h-full rounded-full transition-all"
                                  style={{
                                    width: `${htEffectiveFill}%`,
                                    backgroundColor: getFillBarColor(htEffectiveFill),
                                  }}
                                />
                              )}
                            </div>
                          </div>
                        );

                        return (
                          <div key={ams.id} className="p-2.5 bg-bambu-dark rounded-lg border border-bambu-dark-tertiary/30">
                            {/* Row 1: Label + Nozzle + Drying */}
                            <div className="flex items-center gap-1 mb-2">
                              {/* AMS name - hover to see serial, firmware, and edit friendly name */}
                              <AmsNameHoverCard
                                ams={ams}
                                printerId={printer.id}
                                label={getAmsLabel(ams.id, ams.tray.length)}
                                amsLabels={amsLabels}
                                canEdit={hasPermission('printers:update')}
                                onSaved={refetchAmsLabels}
                              >
                                <span className="text-[10px] text-white font-medium cursor-default select-none">
                                  {amsLabels?.[ams.id] || getAmsLabel(ams.id, ams.tray.length)}
                                </span>
                              </AmsNameHoverCard>
                              {isDualNozzle && (isLeftNozzle || isRightNozzle) && (
                                <NozzleBadge side={isLeftNozzle ? 'L' : 'R'} />
                              )}
                              {/* Drying button for HT AMS */}
                              {status.supports_drying && (ams.module_type === 'n3f' || ams.module_type === 'n3s') && hasPermission('printers:control') && (
                                <div className="relative ml-auto">
                                  <button
                                    onClick={(e) => {
                                      if (ams.dry_time > 0) {
                                        stopDryingMutation.mutate(ams.id);
                                      } else if (dryingPopoverAmsId === ams.id) {
                                        setDryingPopoverAmsId(null);
                                      } else {
                                        const firstTray = ams.tray.find(t => t.tray_type);
                                        const filType = (firstTray?.tray_type || 'PLA').split(' ')[0].toUpperCase();
                                        const preset = dryingPresets[filType] || dryingPresets['PLA'];
                                        const moduleType = ams.module_type as 'n3f' | 'n3s';
                                        setDryingFilament(filType);
                                        setDryingTemp(preset[moduleType] || preset.n3f);
                                        setDryingDuration(moduleType === 'n3s' ? preset.n3s_hours : preset.n3f_hours);
                                        setDryingRotateTray(false);
                                        setDryingPopoverModuleType(ams.module_type);
                                        setDryingPopoverAmsId(ams.id);
                                        const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
                                        setDryingPopoverPos({ top: rect.bottom + 4, left: Math.max(8, rect.right - 240) });
                                      }
                                    }}
                                    className={`flex items-center gap-0.5 px-1 py-0.5 rounded text-[9px] transition-colors ${
                                      ams.dry_time > 0
                                        ? 'bg-amber-500/20 text-amber-400'
                                        : 'bg-bambu-dark-tertiary/50 text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary'
                                    }`}
                                    title={ams.dry_time > 0 ? t('printers.drying.stop') : t('printers.drying.start')}
                                  >
                                    <Flame className="w-3 h-3" />
                                  </button>
                                </div>
                              )}
                            </div>
                            {/* HT AMS drying status bar */}
                            {ams.dry_time > 0 && (
                              <div className="flex items-center gap-1.5 px-2 py-1 mb-1 bg-amber-500/10 border border-amber-500/20 rounded text-[9px] whitespace-nowrap overflow-hidden">
                                <Flame className="w-3 h-3 text-amber-400 shrink-0" />
                                <span className="text-amber-300/70 text-[8px] truncate">
                                  {ams.dry_time >= 60
                                    ? `${Math.floor(ams.dry_time / 60)}h ${ams.dry_time % 60}m`
                                    : `${ams.dry_time}m`}
                                </span>
                                <button
                                  onClick={() => stopDryingMutation.mutate(ams.id)}
                                  disabled={stopDryingMutation.isPending}
                                  className="ml-auto text-amber-400 hover:text-amber-300 transition-colors disabled:opacity-50 shrink-0"
                                  title={t('printers.drying.stop')}
                                >
                                  <X className="w-3 h-3" />
                                </button>
                              </div>
                            )}
                            {/* Row 2: Slot (left) + Stats (right stacked) */}
                            <div className="flex gap-1.5 max-[550px]:flex-col max-[550px]:items-start">
                              {/* Slot wrapper with menu button, dropdown, and loading overlay */}
                              <div className="relative group flex-1 max-[550px]:w-full">
                                {/* Loading overlay during RFID re-read */}
                                {isHtRefreshing && (
                                  <div className="absolute inset-0 bg-bambu-dark-tertiary/80 rounded flex items-center justify-center z-20">
                                    <RefreshCw className="w-4 h-4 text-bambu-green animate-spin" />
                                  </div>
                                )}
                                {/* Menu button - appears on hover, hidden when printer busy */}
                                {status?.state !== 'RUNNING' && (
                                  <button
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      setAmsSlotMenu(
                                        amsSlotMenu?.amsId === ams.id && amsSlotMenu?.slotId === htSlotId
                                          ? null
                                          : { amsId: ams.id, slotId: htSlotId }
                                      );
                                    }}
                                    className="absolute -top-1 -right-1 w-4 h-4 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity z-10 hover:bg-bambu-dark-tertiary"
                                    title={t('printers.slotOptions')}
                                  >
                                    <MoreVertical className="w-2.5 h-2.5 text-bambu-gray" />
                                  </button>
                                )}
                                {/* Dropdown menu */}
                                {status?.state !== 'RUNNING' && amsSlotMenu?.amsId === ams.id && amsSlotMenu?.slotId === htSlotId && (
                                  <div className="absolute top-full left-0 mt-1 z-50 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl py-1 min-w-[120px]">
                                    <button
                                      className={`w-full px-3 py-1.5 text-left text-xs flex items-center gap-2 ${
                                        hasPermission('printers:ams_rfid')
                                          ? 'text-white hover:bg-bambu-dark-tertiary'
                                          : 'text-bambu-gray/50 cursor-not-allowed'
                                      }`}
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        if (!hasPermission('printers:ams_rfid')) return;
                                        refreshAmsSlotMutation.mutate({ amsId: ams.id, slotId: htSlotId });
                                        setAmsSlotMenu(null);
                                      }}
                                      disabled={isHtRefreshing || !hasPermission('printers:ams_rfid')}
                                      title={!hasPermission('printers:ams_rfid') ? t('printers.permission.noAmsRfid') : undefined}
                                    >
                                      <RefreshCw className={`w-3 h-3 ${isHtRefreshing ? 'animate-spin' : ''}`} />
                                      {t('printers.rfid.reread')}
                                    </button>
                                  </div>
                                )}
                                {/* Hover card wraps only the visual content */}
                                {filamentData ? (
                                  <FilamentHoverCard
                                    data={filamentData}
                                    spoolman={{
                                      enabled: spoolmanEnabled,
                                      linkedSpoolId: (htTrayTag ? linkedSpools?.[htTrayTag]?.id : undefined)
                                        ?? htSlotAssignmentForFill?.spoolman_spool_id,
                                      spoolmanUrl,
                                      syncMode: spoolmanSyncMode,
                                      onLinkSpool: (spoolmanEnabled && !htSlotAssignmentForFill && !htInventoryAssignment) ? () => {
                                        const linkTag = (filamentData.trayUuid || filamentData.tagUid || getFallbackSpoolTag(printer.serial_number, ams.id, htSlotId)).toUpperCase();
                                        setLinkSpoolModal({
                                          tagUid: filamentData.tagUid || linkTag,
                                          trayUuid: filamentData.trayUuid || '',
                                          printerId: printer.id,
                                          amsId: ams.id,
                                          trayId: htSlotId,
                                        });
                                      } : undefined,
                                      onUnlinkSpool: htLinkedSpool?.id ? () => unlinkSpoolMutation.mutate(htLinkedSpool.id) : undefined,
                                    }}
                                    inventory={(() => {
                                      // Spoolman-mode inventory branch (upstream PR #1241).
                                      if (spoolmanEnabled) {
                                        if (spoolmanLoading) return undefined;
                                        const spoolmanSpool = htSlotSpoolForFill;
                                        return {
                                          assignedSpool: spoolmanSpool ? {
                                            id: spoolmanSpool.id,
                                            material: spoolmanSpool.material,
                                            brand: spoolmanSpool.brand ?? null,
                                            color_name: spoolmanSpool.color_name ?? null,
                                            remainingWeightGrams: spoolmanSpool.label_weight
                                              ? Math.max(0, Math.round(spoolmanSpool.label_weight - spoolmanSpool.weight_used))
                                              : undefined,
                                            displayName: formatSpoolDisplayName(spoolmanSpool, effectiveSpoolTemplate),
                                          } : null,
                                          onAssignSpool: () => setAssignSpoolModal({
                                            printerId: printer.id,
                                            amsId: ams.id,
                                            trayId: htSlotId,
                                            trayInfo: {
                                              type: tray?.tray_type || filamentData.profile,
                                              material: tray?.tray_type ?? undefined,
                                              profile: filamentData.profile,
                                              color: filamentData.colorHex || '',
                                              location: getAmsLabel(ams.id, ams.tray.length),
                                            },
                                          }),
                                          onUnassignSpool: spoolmanSpool ? () => onUnassignSpoolmanSpool?.(spoolmanSpool.id) : undefined,
                                        };
                                      }
                                      const assignment = onGetAssignment?.(printer.id, ams.id, htSlotId);
                                      return {
                                        assignedSpool: assignment?.spool ? {
                                          id: assignment.spool.id,
                                          material: assignment.spool.material,
                                          brand: assignment.spool.brand,
                                          color_name: assignment.spool.color_name,
                                          remainingWeightGrams: Math.max(0, Math.round(assignment.spool.label_weight - assignment.spool.weight_used)),
                                          displayName: formatSpoolDisplayName(assignment.spool, effectiveSpoolTemplate),
                                        } : null,
                                        onAssignSpool: filamentData.vendor !== 'Bambu Lab' ? () => setAssignSpoolModal({
                                          printerId: printer.id,
                                          amsId: ams.id,
                                          trayId: htSlotId,
                                          trayInfo: {
                                            type: tray?.tray_type || filamentData.profile,
                                            material: tray?.tray_type ?? undefined,
                                            profile: filamentData.profile,
                                            color: filamentData.colorHex || '',
                                            location: getAmsLabel(ams.id, ams.tray.length),
                                          },
                                        }) : undefined,
                                        onUnassignSpool: assignment && filamentData.vendor !== 'Bambu Lab' ? () => onUnassignSpool?.(printer.id, ams.id, htSlotId) : undefined,
                                      };
                                    })()}
                                    configureSlot={{
                                      enabled: hasPermission('printers:control'),
                                      onConfigure: () => setConfigureSlotModal({
                                        amsId: ams.id,
                                        trayId: htSlotId,
                                        trayCount: ams.tray.length,
                                        trayType: tray?.tray_type || undefined,
                                        trayColor: tray?.tray_color || undefined,
                                        traySubBrands: tray?.tray_sub_brands || undefined,
                                        trayInfoIdx: tray?.tray_info_idx || undefined,
                                        extruderId: mappedExtruderId,
                                        caliIdx: tray?.cali_idx,
                                        savedPresetId: slotPreset?.preset_id,
                                      }),
                                    }}
                                  >
                                    {slotVisual}
                                  </FilamentHoverCard>
                                ) : (
                                  <EmptySlotHoverCard
                                    configureSlot={{
                                      enabled: hasPermission('printers:control'),
                                      onConfigure: () => setConfigureSlotModal({
                                        amsId: ams.id,
                                        trayId: htSlotId,
                                        trayCount: ams.tray.length,
                                        extruderId: mappedExtruderId,
                                      }),
                                    }}
                                  >
                                    {slotVisual}
                                  </EmptySlotHoverCard>
                                )}
                              </div>
                              {/* Stats stacked vertically: Temp on top, Humidity below */}
                              {(ams.humidity != null || ams.temp != null) && (
                                <div className="flex flex-col justify-center gap-1 shrink-0 max-[550px]:w-full">
                                  {ams.temp != null && (
                                    <TemperatureIndicator
                                      temp={ams.temp}
                                      goodThreshold={amsThresholds?.tempGood}
                                      fairThreshold={amsThresholds?.tempFair}
                                      onClick={() => setAmsHistoryModal({
                                        amsId: ams.id,
                                        amsLabel: getAmsLabel(ams.id, ams.tray.length),
                                        mode: 'temperature',
                                      })}
                                      compact
                                    />
                                  )}
                                  {ams.humidity != null && (
                                    <HumidityIndicator
                                      humidity={ams.humidity}
                                      goodThreshold={amsThresholds?.humidityGood}
                                      fairThreshold={amsThresholds?.humidityFair}
                                      onClick={() => setAmsHistoryModal({
                                        amsId: ams.id,
                                        amsLabel: getAmsLabel(ams.id, ams.tray.length),
                                        mode: 'humidity',
                                      })}
                                      compact
                                    />
                                  )}
                                </div>
                              )}
                            </div>
                          </div>
                        );
                      })}
                      {/* External spool(s) - grouped in one card like regular AMS */}
                      {status.vt_tray.length > 0 && (
                        <div className={`p-2.5 bg-bambu-dark rounded-lg border border-bambu-dark-tertiary/30 ${status.vt_tray.length === 1 ? 'max-w-[50%]' : ''}`}>
                          <div className="flex items-center justify-center gap-1 mb-2">
                            <span className="text-[10px] text-white font-medium">{t('printers.external')}</span>
                          </div>
                          <div className={`grid ${status.vt_tray.length > 1 ? 'grid-cols-2' : 'grid-cols-1'} gap-1.5`}>
                            {[...status.vt_tray].sort((a, b) => (a.id ?? 254) - (b.id ?? 254)).map((extTray) => {
                              const extTrayId = extTray.id ?? 254;
                              // On dual-nozzle (H2C/H2D), tray_now=254 means "external spool"
                              // generically - use active_extruder to determine L vs R:
                              // extruder 1=left → Ext-L (id=254), extruder 0=right → Ext-R (id=255)
                              const isExtActive = isDualNozzle && effectiveTrayNow === 254
                                ? (extTrayId === 254 && status.active_extruder === 1) ||
                                  (extTrayId === 255 && status.active_extruder === 0)
                                : effectiveTrayNow === extTrayId;
                              const slotTrayId = extTrayId - 254; // 0 or 1
                              const extLabel = isDualNozzle
                                ? (extTrayId === 254 ? t('printers.extL') : t('printers.extR'))
                                : '';
                              const extCloudInfo = extTray.tray_info_idx ? filamentInfo?.[extTray.tray_info_idx] : null;
                              const extSlotPreset = slotPresets?.[255 * 4 + slotTrayId];

                              const extTrayTag = (extTray.tray_uuid || extTray.tag_uid || getFallbackSpoolTag(printer.serial_number, 255, slotTrayId))?.toUpperCase();
                              const extLinkedSpool = extTrayTag ? linkedSpools?.[extTrayTag] : undefined;
                              const extSpoolmanFill = getSpoolmanFillLevel(extLinkedSpool);
                              // Slot-assigned-only spool for the External Spool slot (upstream PR #1241).
                              // External feed AMS id is 255 (firmware convention).
                              const extSlotAssignmentForFill = spoolmanEnabled && !spoolmanLoading
                                ? spoolmanSlotAssignments?.find((a) => a.printer_id === printer.id && a.ams_id === 255 && a.tray_id === slotTrayId)
                                : undefined;
                              const extSlotSpoolForFill = extSlotAssignmentForFill
                                ? spoolmanSpools?.find((s) => s.id === extSlotAssignmentForFill.spoolman_spool_id)
                                : undefined;
                              const extSlotSpoolFill = (extSlotSpoolForFill && (extSlotSpoolForFill.label_weight ?? 0) > 0)
                                ? Math.round(Math.max(0, (extSlotSpoolForFill.label_weight ?? 0) - extSlotSpoolForFill.weight_used) / (extSlotSpoolForFill.label_weight ?? 1) * 100)
                                : null;
                              const extInventoryAssignment = onGetAssignment?.(printer.id, 255, slotTrayId);
                              const extInventoryFill = (() => {
                                const sp = extInventoryAssignment?.spool;
                                if (sp && sp.label_weight > 0 && sp.weight_used != null) {
                                  return Math.round(Math.max(0, sp.label_weight - sp.weight_used) / sp.label_weight * 100);
                                }
                                return null;
                              })();
                              const extHasFillLevel = extTray.tray_type && extTray.remain >= 0;
                              // If inventory says 0% but AMS reports positive remain, prefer AMS (#676)
                              const extResolvedInventoryFill = (extInventoryFill === 0 && extHasFillLevel && extTray.remain > 0)
                                ? null : extInventoryFill;
                              const extEffectiveFill = extSpoolmanFill ?? extSlotSpoolFill ?? extResolvedInventoryFill ?? (extHasFillLevel ? extTray.remain : null);
                              const extFillSource = (extSpoolmanFill !== null || extSlotSpoolFill !== null) ? 'spoolman' as const
                                : extResolvedInventoryFill !== null ? 'inventory' as const
                                : extHasFillLevel ? 'ams' as const
                                : undefined;

                              const extFilamentData = {
                                vendor: (isBambuLabSpool(extTray) ? 'Bambu Lab' : 'Generic') as 'Bambu Lab' | 'Generic',
                                profile: extSlotPreset?.preset_name
                                  || (extSlotSpoolForFill ? [extSlotSpoolForFill.brand, extSlotSpoolForFill.slicer_filament_name?.split('@')[0].trim() || extSlotSpoolForFill.material].filter(Boolean).join(' ').trim() : null)
                                  || extInventoryAssignment?.spool?.slicer_filament_name
                                  || extCloudInfo?.name
                                  || extTray.tray_sub_brands
                                  || extTray.tray_type
                                  || 'Unknown',
                                colorName: getColorName(extTray.tray_color || ''),
                                colorHex: extTray.tray_color || null,
                                kFactor: formatKValue(extTray.k),
                                fillLevel: extEffectiveFill,
                                trayUuid: extTray.tray_uuid || null,
                                tagUid: extTray.tag_uid || null,
                                fillSource: extFillSource,
                              };

                              const isEmpty = !extTray.tray_type;
                              const extSlotContent = (
                                <div className={`bg-bambu-dark-tertiary rounded p-1 text-center ${isEmpty ? 'opacity-50' : ''} ${isExtActive ? 'ring-2 ring-bambu-green ring-offset-1 ring-offset-bambu-dark' : ''}`}>
                                  {/* Filament color circle with 1-based slot number centered inside */}
                                  <FilamentSlotCircle
                                    trayColor={extTray.tray_color}
                                    trayType={extTray.tray_type}
                                    isEmpty={isEmpty}
                                    slotNumber={slotTrayId + 1}
                                  />
                                  <div className={`text-[9px] font-bold truncate ${isEmpty ? 'text-white/40' : 'text-white'}`}>
                                    {extTray.tray_type || '-'}
                                  </div>
                                  <div className="mt-1 h-1.5 bg-black/30 rounded-full overflow-hidden">
                                    {extEffectiveFill !== null && extEffectiveFill >= 0 && !isEmpty && (
                                      <div
                                        className="h-full rounded-full transition-all"
                                        style={{
                                          width: `${extEffectiveFill}%`,
                                          backgroundColor: getFillBarColor(extEffectiveFill),
                                        }}
                                      />
                                    )}
                                  </div>
                                  {extLabel && <div className="text-[7px] text-white/40 mt-0.5 truncate">{extLabel}</div>}
                                </div>
                              );

                              return (
                                <div key={extTrayId} className="relative group">
                                  {/* Slot-options menu (#891 — Load/Unload from the printer card).
                                   * For external spools the global tray_id IS extTrayId (254=Ext-L
                                   * single-extruder/H2D-left, 255=Ext-R H2D-right) — different from
                                   * the inline AMS slots where the tray_id is computed from
                                   * ams.id*4 + slotIdx. Hidden while RUNNING, mirroring the
                                   * AMS-slot popover gating above. */}
                                  {status?.state !== 'RUNNING' && (
                                    <button
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        setAmsSlotMenu(
                                          amsSlotMenu?.amsId === 255 && amsSlotMenu?.slotId === slotTrayId
                                            ? null
                                            : { amsId: 255, slotId: slotTrayId }
                                        );
                                      }}
                                      className="absolute -top-1 -right-1 w-4 h-4 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity z-10 hover:bg-bambu-dark-tertiary"
                                      title={t('printers.slotOptions')}
                                    >
                                      <MoreVertical className="w-2.5 h-2.5 text-bambu-gray" />
                                    </button>
                                  )}
                                  {status?.state !== 'RUNNING' && amsSlotMenu?.amsId === 255 && amsSlotMenu?.slotId === slotTrayId && (
                                    <div className="absolute top-full left-0 mt-1 z-50 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl py-1 min-w-[140px]">
                                      <button
                                        className={`w-full px-3 py-1.5 text-left text-xs flex items-center gap-2 ${
                                          hasPermission('printers:control')
                                            ? 'text-white hover:bg-bambu-dark-tertiary'
                                            : 'text-bambu-gray/50 cursor-not-allowed'
                                        }`}
                                        onClick={(e) => {
                                          e.stopPropagation();
                                          if (!hasPermission('printers:control')) return;
                                          amsLoadMutation.mutate(extTrayId);
                                          setAmsSlotMenu(null);
                                        }}
                                        disabled={amsLoadMutation.isPending || !hasPermission('printers:control')}
                                        title={!hasPermission('printers:control') ? t('printers.permission.noControl') : undefined}
                                      >
                                        <ArrowDownToLine className="w-3 h-3" />
                                        {amsLoadMutation.isPending ? t('printers.ams.loading') : t('printers.ams.load')}
                                      </button>
                                      <button
                                        className={`w-full px-3 py-1.5 text-left text-xs flex items-center gap-2 ${
                                          hasPermission('printers:control')
                                            ? 'text-white hover:bg-bambu-dark-tertiary'
                                            : 'text-bambu-gray/50 cursor-not-allowed'
                                        }`}
                                        onClick={(e) => {
                                          e.stopPropagation();
                                          if (!hasPermission('printers:control')) return;
                                          amsUnloadMutation.mutate();
                                          setAmsSlotMenu(null);
                                        }}
                                        disabled={amsUnloadMutation.isPending || !hasPermission('printers:control')}
                                        title={!hasPermission('printers:control') ? t('printers.permission.noControl') : undefined}
                                      >
                                        <ArrowUpFromLine className="w-3 h-3" />
                                        {amsUnloadMutation.isPending ? t('printers.ams.unloading') : t('printers.ams.unload')}
                                      </button>
                                    </div>
                                  )}
                                  {!isEmpty ? (
                                    <FilamentHoverCard
                                      data={extFilamentData}
                                      spoolman={{
                                        enabled: spoolmanEnabled,
                                        linkedSpoolId: (extTrayTag ? linkedSpools?.[extTrayTag]?.id : undefined)
                                          ?? extSlotAssignmentForFill?.spoolman_spool_id,
                                        spoolmanUrl,
                                        syncMode: spoolmanSyncMode,
                                        onLinkSpool: (spoolmanEnabled && !extSlotAssignmentForFill && !extInventoryAssignment) ? () => {
                                          const linkTag = (extFilamentData.trayUuid || extFilamentData.tagUid || getFallbackSpoolTag(printer.serial_number, 255, slotTrayId)).toUpperCase();
                                          setLinkSpoolModal({
                                            tagUid: extFilamentData.tagUid || linkTag,
                                            trayUuid: extFilamentData.trayUuid || '',
                                            printerId: printer.id,
                                            amsId: 255,
                                            trayId: slotTrayId,
                                          });
                                        } : undefined,
                                        onUnlinkSpool: extLinkedSpool?.id ? () => unlinkSpoolMutation.mutate(extLinkedSpool.id) : undefined,
                                      }}
                                      inventory={(() => {
                                        // Spoolman-mode inventory branch (upstream PR #1241).
                                        if (spoolmanEnabled) {
                                          if (spoolmanLoading) return undefined;
                                          const spoolmanSpool = extSlotSpoolForFill;
                                          return {
                                            assignedSpool: spoolmanSpool ? {
                                              id: spoolmanSpool.id,
                                              material: spoolmanSpool.material,
                                              brand: spoolmanSpool.brand ?? null,
                                              color_name: spoolmanSpool.color_name ?? null,
                                              remainingWeightGrams: spoolmanSpool.label_weight
                                                ? Math.max(0, Math.round(spoolmanSpool.label_weight - spoolmanSpool.weight_used))
                                                : undefined,
                                              displayName: formatSpoolDisplayName(spoolmanSpool, effectiveSpoolTemplate),
                                            } : null,
                                            onAssignSpool: () => setAssignSpoolModal({
                                              printerId: printer.id,
                                              amsId: 255,
                                              trayId: slotTrayId,
                                              trayInfo: {
                                                type: extTray.tray_type || extFilamentData.profile,
                                                material: extTray.tray_type ?? undefined,
                                                profile: extFilamentData.profile,
                                                color: extFilamentData.colorHex || '',
                                                location: extLabel || t('printers.external'),
                                              },
                                            }),
                                            onUnassignSpool: spoolmanSpool ? () => onUnassignSpoolmanSpool?.(spoolmanSpool.id) : undefined,
                                          };
                                        }
                                        const assignment = onGetAssignment?.(printer.id, 255, slotTrayId);
                                        return {
                                          assignedSpool: assignment?.spool ? {
                                            id: assignment.spool.id,
                                            material: assignment.spool.material,
                                            brand: assignment.spool.brand,
                                            color_name: assignment.spool.color_name,
                                            remainingWeightGrams: Math.max(0, Math.round(assignment.spool.label_weight - assignment.spool.weight_used)),
                                            displayName: formatSpoolDisplayName(assignment.spool, effectiveSpoolTemplate),
                                          } : null,
                                          onAssignSpool: () => setAssignSpoolModal({
                                            printerId: printer.id,
                                            amsId: 255,
                                            trayId: slotTrayId,
                                            trayInfo: {
                                              type: extTray.tray_type || extFilamentData.profile,
                                              material: extTray.tray_type ?? undefined,
                                              profile: extFilamentData.profile,
                                              color: extFilamentData.colorHex || '',
                                              location: extLabel || t('printers.external'),
                                            },
                                          }),
                                          onUnassignSpool: assignment ? () => onUnassignSpool?.(printer.id, 255, slotTrayId) : undefined,
                                        };
                                      })()}
                                      configureSlot={{
                                        enabled: hasPermission('printers:control'),
                                        onConfigure: () => setConfigureSlotModal({
                                          amsId: 255,
                                          trayId: slotTrayId,
                                          trayCount: 1,
                                          trayType: extTray.tray_type || undefined,
                                          trayColor: extTray.tray_color || undefined,
                                          traySubBrands: extTray.tray_sub_brands || undefined,
                                          trayInfoIdx: extTray.tray_info_idx || undefined,
                                          extruderId: isDualNozzle ? (extTrayId === 254 ? 1 : 0) : undefined,
                                          caliIdx: extTray.cali_idx,
                                          savedPresetId: extSlotPreset?.preset_id,
                                        }),
                                      }}
                                    >
                                      {extSlotContent}
                                    </FilamentHoverCard>
                                  ) : (
                                    <EmptySlotHoverCard
                                      configureSlot={{
                                        enabled: hasPermission('printers:control'),
                                        onConfigure: () => setConfigureSlotModal({
                                          amsId: 255,
                                          trayId: slotTrayId,
                                          trayCount: 1,
                                          extruderId: isDualNozzle ? (extTrayId === 254 ? 1 : 0) : undefined,
                                        }),
                                      }}
                                    >
                                      {extSlotContent}
                                    </EmptySlotHoverCard>
                                  )}
                                </div>
                              );
                            })}
                          </div>
                        </div>
                      )}
                      </div>
                    )}
                  </div>
                </div>
              );
            })()}
          </>
        )}

        {/* Smart Plug Controls - hidden in compact mode */}
        {smartPlug && viewMode === 'expanded' && (
          <div className="mt-4 pt-4 border-t border-bambu-dark-tertiary">
            <div className="flex items-center gap-3">
              {/* Plug name and status */}
              <div className="flex items-center gap-2 min-w-0">
                <Zap className="w-4 h-4 text-bambu-gray flex-shrink-0" />
                <span className="text-sm text-white truncate">{smartPlug.name}</span>
                {plugStatus && (
                  <span
                    className={`text-xs px-1.5 py-0.5 rounded flex-shrink-0 ${
                      plugStatus.state === 'ON'
                        ? 'bg-bambu-green/20 text-bambu-green'
                        : plugStatus.state === 'OFF'
                        ? 'bg-red-500/20 text-red-400'
                        : 'bg-bambu-gray/20 text-bambu-gray'
                    }`}
                  >
                    {plugStatus.state || '?'}
                    {plugStatus.state === 'ON' && plugStatus.energy?.power != null && (
                      <span className="text-yellow-400 ml-1.5">· {plugStatus.energy.power}W</span>
                    )}
                  </span>
                )}
              </div>

              {/* Spacer */}
              <div className="flex-1" />

              {/* Power buttons */}
              <div className="flex items-center gap-1">
                <button
                  onClick={() => setShowPowerOnConfirm(true)}
                  disabled={powerControlMutation.isPending || plugStatus?.state === 'ON' || !hasPermission('smart_plugs:control')}
                  className={`px-2 py-1 text-xs rounded transition-colors flex items-center gap-1 ${
                    !hasPermission('smart_plugs:control')
                      ? 'bg-bambu-dark text-bambu-gray/50 cursor-not-allowed'
                      : plugStatus?.state === 'ON'
                        ? 'bg-bambu-green text-white'
                        : 'bg-bambu-dark text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary'
                  }`}
                  title={!hasPermission('smart_plugs:control') ? t('printers.permission.noSmartPlugControl') : undefined}
                >
                  <Power className="w-3 h-3" />
                  On
                </button>
                <button
                  onClick={() => setShowPowerOffConfirm(true)}
                  disabled={powerControlMutation.isPending || plugStatus?.state === 'OFF' || !hasPermission('smart_plugs:control')}
                  className={`px-2 py-1 text-xs rounded transition-colors flex items-center gap-1 ${
                    !hasPermission('smart_plugs:control')
                      ? 'bg-bambu-dark text-bambu-gray/50 cursor-not-allowed'
                      : plugStatus?.state === 'OFF'
                        ? 'bg-red-500/30 text-red-400'
                        : 'bg-bambu-dark text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary'
                  }`}
                  title={!hasPermission('smart_plugs:control') ? t('printers.permission.noSmartPlugControl') : undefined}
                >
                  <PowerOff className="w-3 h-3" />
                  Off
                </button>
              </div>

              {/* Auto-off toggle */}
              <div className="flex items-center gap-2 flex-shrink-0">
                <span className={`text-xs hidden sm:inline ${smartPlug.auto_off_executed ? 'text-bambu-green' : 'text-bambu-gray'}`}>
                  {smartPlug.auto_off_executed ? 'Auto-off done' : 'Auto-off'}
                </span>
                <button
                  onClick={() => toggleAutoOffMutation.mutate(!smartPlug.auto_off)}
                  disabled={toggleAutoOffMutation.isPending || smartPlug.auto_off_executed || !hasPermission('smart_plugs:control')}
                  title={!hasPermission('smart_plugs:control') ? t('printers.permission.noSmartPlugControl') : (smartPlug.auto_off_executed ? t('printers.autoOffExecuted') : t('printers.autoOffAfterPrint'))}
                  className={`relative w-9 h-5 rounded-full transition-colors flex-shrink-0 ${
                    !hasPermission('smart_plugs:control')
                      ? 'bg-bambu-dark-tertiary/50 cursor-not-allowed'
                      : smartPlug.auto_off_executed
                        ? 'bg-bambu-green/50 cursor-not-allowed'
                        : smartPlug.auto_off ? 'bg-bambu-green' : 'bg-bambu-dark-tertiary'
                  }`}
                >
                  <span
                    className={`absolute top-[2px] left-[2px] w-4 h-4 bg-white rounded-full transition-transform ${
                      smartPlug.auto_off || smartPlug.auto_off_executed ? 'translate-x-4' : 'translate-x-0'
                    }`}
                  />
                </button>
              </div>
            </div>

            {/* HA entity buttons row */}
            {scriptPlugs && scriptPlugs.length > 0 && (
              <div className="flex items-center gap-2 mt-2 pt-2 border-t border-bambu-dark-tertiary/50">
                <Home className="w-3.5 h-3.5 text-blue-400 flex-shrink-0" />
                <span className="text-xs text-bambu-gray">HA:</span>
                <div className="flex flex-wrap gap-1">
                  {scriptPlugs.map(script => {
                    const isScript = script.ha_entity_id?.startsWith('script.');
                    return (
                      <button
                        key={script.id}
                        onClick={() => {
                          if (isScript) {
                            runScriptMutation.mutate({ id: script.id, action: 'on' });
                          } else {
                            setHaToggleConfirm(script);
                          }
                        }}
                        disabled={runScriptMutation.isPending}
                        title={`${isScript ? 'Run' : 'Toggle'} ${script.ha_entity_id}`}
                        className="px-2 py-0.5 text-xs bg-blue-500/20 text-blue-400 hover:bg-blue-500/30 rounded transition-colors flex items-center gap-1"
                      >
                        <Play className="w-2.5 h-2.5" />
                        {script.name}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        )}

        {/* Archive summary — counter line mirrors QueueCard footer, links
            to the archive filtered by this printer. Sits above the action
            buttons so it reads as part of the printer's "status" row. */}
        {printerQueue && (
          <RouterLink
            to={`/archives?printer=${printer.id}`}
            className="block text-xs text-bambu-gray hover:text-white pt-2 mt-2 border-t border-bambu-dark-tertiary transition-colors"
            title={t('queueCard.footer.viewArchivesTitle')}
          >
            {t('queueCard.footer.pending', { count: printerQueue.pending_count })}
            {' \u00B7 '}
            {t('queueCard.footer.done', { count: printerQueue.completed_count })}
            {printerQueue.failed_count > 0 && <>{' \u00B7 '}{t('queueCard.footer.failed', { count: printerQueue.failed_count })}</>}
            {printerQueue.cancelled_count > 0 && <>{' \u00B7 '}{t('queueCard.footer.cancelled', { count: printerQueue.cancelled_count })}</>}
          </RouterLink>
        )}

        {/* Connection Info & Actions - hidden in compact mode */}
        {viewMode === 'expanded' && (
          <div className="mt-2 pt-4 border-t border-bambu-dark-tertiary flex items-center justify-end gap-2 flex-wrap">
              {/* Chamber Light */}
              <Button
                variant="secondary"
                size="sm"
                onClick={() => chamberLightMutation.mutate(!status?.chamber_light)}
                disabled={!status?.connected || chamberLightMutation.isPending || !hasPermission('printers:control')}
                title={!hasPermission('printers:control') ? t('printers.permission.noControl') : (status?.chamber_light ? t('printers.chamberLightOff') : t('printers.chamberLightOn'))}
                className={status?.chamber_light ? '!border-yellow-500 !text-yellow-400 hover:!bg-yellow-500/20' : ''}
              >
                <ChamberLight on={status?.chamber_light ?? false} className={`w-5 h-5 ${status?.chamber_light ? 'text-yellow-400' : ''}`} />
              </Button>
              {/* Camera Button */}
              <Button
                variant="secondary"
                size="sm"
                onClick={() => {
                  if (cameraViewMode === 'embedded' && onOpenEmbeddedCamera) {
                    onOpenEmbeddedCamera(printer.id, printer.name);
                  } else {
                    // Use saved window state or defaults
                    const saved = localStorage.getItem('cameraWindowState');
                    const state = saved ? JSON.parse(saved) : { width: 640, height: 400 };
                    const features = [
                      `width=${state.width}`,
                      `height=${state.height}`,
                      state.left !== undefined ? `left=${state.left}` : '',
                      state.top !== undefined ? `top=${state.top}` : '',
                      'menubar=no,toolbar=no,location=no,status=no,noopener',
                    ].filter(Boolean).join(',');
                    window.open(`/camera/${printer.id}`, `camera-${printer.id}`, features);
                  }
                }}
                disabled={!status?.connected || !hasPermission('camera:view')}
                title={!hasPermission('camera:view') ? t('printers.permission.noCamera') : (cameraViewMode === 'embedded' ? t('printers.openCameraOverlay') : t('printers.openCameraWindow'))}
              >
                <Video className="w-5 h-5" />
              </Button>
              {/* Split button: main part toggles detection, chevron opens modal */}
              <div className={`inline-flex rounded-md ${printer.plate_detection_enabled ? 'ring-1 ring-green-500' : ''}`}>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={handleTogglePlateDetection}
                  disabled={!status?.connected || plateDetectionMutation.isPending || !hasPermission('printers:update')}
                  title={!hasPermission('printers:update') ? t('printers.plateDetection.noPermission') : (printer.plate_detection_enabled ? t('printers.plateDetection.enabledClick') : t('printers.plateDetection.disabledClick'))}
                  className={`!rounded-r-none !border-r-0 ${printer.plate_detection_enabled ? "!border-green-500 !text-green-400 hover:!bg-green-500/20" : ""}`}
                >
                  {plateDetectionMutation.isPending ? (
                    <Loader2 className="w-5 h-5 animate-spin" />
                  ) : (
                    <ScanSearch className="w-5 h-5" />
                  )}
                </Button>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={handleOpenPlateManagement}
                  disabled={!status?.connected || isCheckingPlate || !hasPermission('printers:update')}
                  title={!hasPermission('printers:update') ? t('printers.plateDetection.noPermission') : t('printers.plateDetection.manageCalibration')}
                  className={`!rounded-l-none !px-1.5 ${printer.plate_detection_enabled ? "!border-green-500 !text-green-400 hover:!bg-green-500/20" : ""}`}
                >
                  {isCheckingPlate ? (
                    <Loader2 className="w-3 h-3 animate-spin" />
                  ) : (
                    <ChevronDown className="w-3 h-3" />
                  )}
                </Button>
              </div>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setShowFileManager(true)}
                disabled={!isConnected || !hasPermission('printers:files')}
                title={!hasPermission('printers:files') ? t('printers.permission.noFiles') : t('printers.browseFiles')}
              >
                <HardDrive className="w-4 h-4" />
                {t('printers.files')}
              </Button>
              {isConnected && status?.state !== 'RUNNING' && status?.state !== 'PAUSE' && (
                <Button
                  size="sm"
                  onClick={() => setShowUploadForPrint(true)}
                  disabled={!hasPermission('printers:control')}
                  title={!hasPermission('printers:control') ? t('printers.permission.noControl') : t('common.print')}
                  className="!bg-bambu-green hover:!bg-bambu-green/80 !text-white"
                >
                  <PrinterIcon className="w-4 h-4" />
                  {t('common.print')}
                </Button>
              )}
          </div>
        )}
      </CardContent>

      {/* File Manager Modal */}
      {showFileManager && (
        <FileManagerModal
          printerId={printer.id}
          printerName={printer.name}
          onClose={() => setShowFileManager(false)}
        />
      )}

      {/* Upload for Print Modal */}
      {showUploadForPrint && (
        <FileUploadModal
          folderId={null}
          onClose={() => setShowUploadForPrint(false)}
          onUploadComplete={() => {}}
          autoUpload
          accept=".gcode,.3mf"
          validateFile={(file) => {
            const lower = file.name.toLowerCase();
            if (!lower.endsWith('.gcode') && !lower.includes('.gcode.')) {
              return t('printers.dropNotPrintable', 'Only .gcode and .gcode.3mf files can be printed');
            }
          }}
          onFileUploaded={(uploadedFile) => {
            // Check printer compatibility if sliced_for_model is available in metadata
            const slicedFor = (uploadedFile.metadata as Record<string, unknown>)?.sliced_for_model as string | undefined;
            const printerModel = mapModelCode(printer.model);
            if (slicedFor && printerModel && slicedFor.toLowerCase() !== printerModel.toLowerCase()) {
              api.deleteLibraryFile(uploadedFile.id).catch(() => {});
              return t('printers.incompatibleFile', 'This file was sliced for {{slicedFor}}, but this printer is a {{printerModel}}', { slicedFor, printerModel });
            }
            setPrintAfterUpload({ id: uploadedFile.id, filename: uploadedFile.filename });
          }}
        />
      )}

      {/* Print Modal (after upload) — Direct-Print flow: the upload is transient,
          so the library row + disk file get deleted after the print dispatches
          (upstream #730 / #1682b695). Every other api.printLibraryFile caller
          (File Manager Print, Project Detail Print) leaves the flag unset. */}
      {printAfterUpload && (
        <PrintModal
          mode="reprint"
          libraryFileId={printAfterUpload.id}
          archiveName={printAfterUpload.filename}
          initialSelectedPrinterIds={[printer.id]}
          onClose={() => setPrintAfterUpload(null)}
          onSuccess={() => setPrintAfterUpload(null)}
          cleanupLibraryAfterDispatch
        />
      )}

      {/* MQTT Debug Modal */}
      {showMQTTDebug && (
        <MQTTDebugModal
          printerId={printer.id}
          printerName={printer.name}
          onClose={() => setShowMQTTDebug(false)}
        />
      )}

      {/* Calibration Modal */}
      {showCalibration && (
        <CalibrationModal
          printerId={printer.id}
          printerName={printer.name}
          printerModel={printer.model}
          onClose={() => setShowCalibration(false)}
        />
      )}

      {showMacrosMenu && (
        <MacrosPanel
          printer={printer}
          macroExecuting={status?.macro_executing ?? null}
          onClose={() => setShowMacrosMenu(false)}
        />
      )}

      {showPrinterInfo && (
        <PrinterInfoModal
          printer={printer}
          status={status}
          totalPrintHours={maintenanceInfo?.total_print_hours}
          onClose={closePrinterInfo}
        />
      )}

      {/* Plate Check Result Modal */}
      {plateCheckResult && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4" onClick={() => closePlateCheckModal()}>
          <div className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-2xl max-w-lg w-full" onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
              <div className="flex items-center gap-2">
                {plateCheckResult.needs_calibration ? (
                  <ScanSearch className="w-5 h-5 text-blue-500" />
                ) : plateCheckResult.is_empty ? (
                  <CheckCircle className="w-5 h-5 text-green-500" />
                ) : (
                  <XCircle className="w-5 h-5 text-yellow-500" />
                )}
                <h2 className="text-lg font-semibold text-white">
                  {t('printers.plateDetection.title')}
                </h2>
                {plateCheckResult.reference_count !== undefined && plateCheckResult.max_references && (
                  <span className="text-xs text-bambu-gray bg-bambu-dark-tertiary px-2 py-1 rounded">
                    {t('printers.plateDetection.refsCount', { count: plateCheckResult.reference_count, max: plateCheckResult.max_references })}
                  </span>
                )}
              </div>
              <button
                onClick={() => closePlateCheckModal()}
                className="p-1 text-bambu-gray hover:text-white rounded transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
            </div>
            <div className="p-4 space-y-4">
              {plateCheckResult.needs_calibration ? (
                <>
                  <div className="p-3 rounded-lg bg-blue-500/20 border border-blue-500/50">
                    <p className="font-medium text-blue-400">
                      {t('printers.plateDetection.calibrationRequired')}
                    </p>
                    <p className="text-sm text-bambu-gray mt-1" dangerouslySetInnerHTML={{ __html: t('printers.plateDetection.calibrationInstructions') }} />
                  </div>
                  <div className="text-sm text-bambu-gray space-y-2">
                    <p>{t('printers.plateDetection.calibrationDescription')}</p>
                    <p dangerouslySetInnerHTML={{ __html: t('printers.plateDetection.calibrationTip') }} />
                  </div>
                </>
              ) : (
                <>
                  <div className={`p-3 rounded-lg ${plateCheckResult.is_empty ? 'bg-green-500/20 border border-green-500/50' : 'bg-yellow-500/20 border border-yellow-500/50'}`}>
                    <p className={`font-medium ${plateCheckResult.is_empty ? 'text-green-400' : 'text-yellow-400'}`}>
                      {plateCheckResult.is_empty ? t('printers.plateDetection.plateEmpty') : t('printers.plateDetection.objectsDetected')}
                    </p>
                    <p className="text-sm text-bambu-gray mt-1">
                      {t('printers.plateDetection.confidence')}: {Math.round(plateCheckResult.confidence * 100)}% | {t('printers.plateDetection.difference')}: {plateCheckResult.difference_percent.toFixed(1)}%
                    </p>
                  </div>
                  {plateCheckResult.debug_image_url && (
                    <div>
                      <p className="text-sm text-bambu-gray mb-2">{t('printers.plateDetection.analysisPreview')}</p>
                      <img
                        src={plateCheckResult.debug_image_url}
                        alt={t('printers.plateDetection.analysisPreview')}
                        className="w-full rounded-lg border border-bambu-dark-tertiary"
                      />
                      <p className="text-xs text-bambu-gray mt-2">
                        {t('printers.plateDetection.analysisLegend')}
                      </p>
                    </div>
                  )}
                  <p className="text-xs text-bambu-gray">
                    {plateCheckResult.message}
                  </p>
                </>
              )}

              {/* Saved References Grid */}
              {plateReferences && plateReferences.references.length > 0 && (
                <div className="mt-4 pt-4 border-t border-bambu-dark-tertiary">
                  <p className="text-sm font-medium text-white mb-2">
                    {t('printers.plateDetection.savedReferences', { count: plateReferences.references.length, max: plateReferences.max_references })}
                  </p>
                  <div className="grid grid-cols-5 gap-2">
                    {plateReferences.references.map((ref) => (
                      <div key={ref.index} className="relative group">
                        <img
                          src={api.getPlateReferenceThumbnailUrl(printer.id, ref.index)}
                          alt={ref.label || `Reference ${ref.index + 1}`}
                          className="w-full aspect-video object-cover rounded border border-bambu-dark-tertiary"
                        />
                        {/* Delete button */}
                        <button
                          onClick={() => handleDeleteRef(ref.index)}
                          className="absolute top-1 right-1 p-0.5 bg-red-500/80 rounded opacity-0 group-hover:opacity-100 transition-opacity"
                          title={t('printers.plateDetection.deleteReference')}
                        >
                          <X className="w-3 h-3 text-white" />
                        </button>
                        {/* Label */}
                        {editingRefLabel?.index === ref.index ? (
                          <input
                            type="text"
                            value={editingRefLabel.label}
                            onChange={(e) => setEditingRefLabel({ ...editingRefLabel, label: e.target.value })}
                            onBlur={() => handleUpdateRefLabel(ref.index, editingRefLabel.label)}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter') handleUpdateRefLabel(ref.index, editingRefLabel.label);
                              if (e.key === 'Escape') setEditingRefLabel(null);
                            }}
                            className="w-full mt-1 px-1 py-0.5 text-xs bg-bambu-dark-tertiary border border-bambu-green rounded text-white"
                            autoFocus
                            placeholder={t('printers.plateDetection.labelPlaceholder')}
                          />
                        ) : (
                          <p
                            className="text-xs text-bambu-gray mt-1 truncate cursor-pointer hover:text-white"
                            onClick={() => setEditingRefLabel({ index: ref.index, label: ref.label })}
                            title={ref.label ? t('printers.plateDetection.clickToEdit', { label: ref.label }) : t('printers.plateDetection.clickToAddLabel')}
                          >
                            {ref.label || <span className="italic opacity-50">{t('printers.noLabel')}</span>}
                          </p>
                        )}
                        {/* Timestamp — respect user's date_format choice */}
                        <p className="text-[10px] text-bambu-gray/60">
                          {ref.timestamp ? formatDateOnly(ref.timestamp, undefined, dateFormat) : ''}
                        </p>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* ROI Editor */}
              {!plateCheckResult.needs_calibration && (
                <div className="mt-4 pt-4 border-t border-bambu-dark-tertiary">
                  <div className="flex items-center justify-between mb-2">
                    <p className="text-sm font-medium text-white">{t('printers.roi.title')}</p>
                    {!editingRoi ? (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setEditingRoi(plateCheckResult.roi || { x: 0.15, y: 0.35, w: 0.70, h: 0.55 })}
                      >
                        <Pencil className="w-3 h-3 mr-1" />
                        {t('common.edit')}
                      </Button>
                    ) : (
                      <div className="flex gap-1">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => setEditingRoi(null)}
                          disabled={isSavingRoi}
                        >
                          {t('common.cancel')}
                        </Button>
                        <Button
                          size="sm"
                          onClick={handleSaveRoi}
                          disabled={isSavingRoi}
                        >
                          {isSavingRoi ? <Loader2 className="w-3 h-3 animate-spin" /> : t('common.save')}
                        </Button>
                      </div>
                    )}
                  </div>
                  {editingRoi ? (
                    <div className="space-y-3 bg-bambu-dark-tertiary/50 p-3 rounded-lg">
                      <div className="grid grid-cols-2 gap-3">
                        <div>
                          <label className="text-xs text-bambu-gray">{t('printers.roi.xStart')}</label>
                          <input
                            type="range"
                            min="0"
                            max="0.9"
                            step="0.01"
                            value={editingRoi.x}
                            onChange={(e) => setEditingRoi({ ...editingRoi, x: parseFloat(e.target.value) })}
                            className="w-full h-1.5 bg-bambu-dark-tertiary rounded-lg cursor-pointer accent-green-500"
                          />
                          <span className="text-xs text-bambu-gray">{Math.round(editingRoi.x * 100)}%</span>
                        </div>
                        <div>
                          <label className="text-xs text-bambu-gray">{t('printers.roi.yStart')}</label>
                          <input
                            type="range"
                            min="0"
                            max="0.9"
                            step="0.01"
                            value={editingRoi.y}
                            onChange={(e) => setEditingRoi({ ...editingRoi, y: parseFloat(e.target.value) })}
                            className="w-full h-1.5 bg-bambu-dark-tertiary rounded-lg cursor-pointer accent-green-500"
                          />
                          <span className="text-xs text-bambu-gray">{Math.round(editingRoi.y * 100)}%</span>
                        </div>
                        <div>
                          <label className="text-xs text-bambu-gray">{t('printers.width')}</label>
                          <input
                            type="range"
                            min="0.1"
                            max="1"
                            step="0.01"
                            value={editingRoi.w}
                            onChange={(e) => setEditingRoi({ ...editingRoi, w: parseFloat(e.target.value) })}
                            className="w-full h-1.5 bg-bambu-dark-tertiary rounded-lg cursor-pointer accent-green-500"
                          />
                          <span className="text-xs text-bambu-gray">{Math.round(editingRoi.w * 100)}%</span>
                        </div>
                        <div>
                          <label className="text-xs text-bambu-gray">{t('printers.height')}</label>
                          <input
                            type="range"
                            min="0.1"
                            max="1"
                            step="0.01"
                            value={editingRoi.h}
                            onChange={(e) => setEditingRoi({ ...editingRoi, h: parseFloat(e.target.value) })}
                            className="w-full h-1.5 bg-bambu-dark-tertiary rounded-lg cursor-pointer accent-green-500"
                          />
                          <span className="text-xs text-bambu-gray">{Math.round(editingRoi.h * 100)}%</span>
                        </div>
                      </div>
                      <p className="text-xs text-bambu-gray">
                        {t('printers.roi.instruction')}
                      </p>
                    </div>
                  ) : (
                    <p className="text-xs text-bambu-gray">
                      Current: X={Math.round((plateCheckResult.roi?.x || 0.15) * 100)}%, Y={Math.round((plateCheckResult.roi?.y || 0.35) * 100)}%,
                      W={Math.round((plateCheckResult.roi?.w || 0.70) * 100)}%, H={Math.round((plateCheckResult.roi?.h || 0.55) * 100)}%
                    </p>
                  )}
                </div>
              )}
            </div>
            <div className="flex justify-end gap-2 p-4 border-t border-bambu-dark-tertiary">
              {plateCheckResult.needs_calibration ? (
                <>
                  <Button variant="ghost" onClick={() => closePlateCheckModal()}>
                    {t('common.cancel')}
                  </Button>
                  <Button
                    onClick={() => handleCalibratePlate()}
                    disabled={isCalibrating}
                  >
                    {isCalibrating ? (
                      <>
                        <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                        {t('printers.plateDetection.calibrating')}
                      </>
                    ) : (
                      t('printers.plateDetection.calibrateEmptyPlate')
                    )}
                  </Button>
                </>
              ) : (
                <>
                  <Button variant="ghost" onClick={() => handleCalibratePlate()} disabled={isCalibrating}>
                    {isCalibrating ? t('printers.plateDetection.adding') : t('printers.plateDetection.addReference', { count: plateReferences?.references.length || 0, max: plateReferences?.max_references || 5 })}
                  </Button>
                  <Button onClick={() => closePlateCheckModal()}>
                    {t('common.close')}
                  </Button>
                </>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Power On Confirmation */}
      {showPowerOnConfirm && smartPlug && (
        <ConfirmModal
          title={t('printers.confirm.powerOnTitle')}
          message={t('printers.confirm.powerOnMessage', { name: printer.name })}
          confirmText={t('printers.confirm.powerOnButton')}
          variant="default"
          onConfirm={() => {
            powerControlMutation.mutate('on');
            setShowPowerOnConfirm(false);
          }}
          onCancel={() => setShowPowerOnConfirm(false)}
        />
      )}

      {/* HA entity toggle confirmation (switch/light/etc. on the printer card).
          script.* entities skip this — they're fire-once triggers and adding a
          confirm modal would annoy. Mirrors the power-off variant + warning
          copy when status.state === 'RUNNING'. */}
      {haToggleConfirm && (
        <ConfirmModal
          title={t('printers.confirm.haToggleTitle', { name: haToggleConfirm.name })}
          message={
            status?.state === 'RUNNING'
              ? t('printers.confirm.haToggleWarning', { name: printer.name, entity: haToggleConfirm.ha_entity_id || haToggleConfirm.name })
              : t('printers.confirm.haToggleMessage', { entity: haToggleConfirm.ha_entity_id || haToggleConfirm.name })
          }
          confirmText={t('printers.confirm.haToggleButton')}
          variant={status?.state === 'RUNNING' ? 'danger' : 'default'}
          onConfirm={() => {
            runScriptMutation.mutate({ id: haToggleConfirm.id, action: 'toggle' });
            setHaToggleConfirm(null);
          }}
          onCancel={() => setHaToggleConfirm(null)}
        />
      )}

      {/* Power Off Confirmation */}
      {showPowerOffConfirm && smartPlug && (
        <ConfirmModal
          title={t('printers.confirm.powerOffTitle')}
          message={
            status?.state === 'RUNNING'
              ? t('printers.confirm.powerOffWarning', { name: printer.name })
              : t('printers.confirm.powerOffMessage', { name: printer.name })
          }
          confirmText={t('printers.confirm.powerOffButton')}
          variant="danger"
          onConfirm={() => {
            powerControlMutation.mutate('off');
            setShowPowerOffConfirm(false);
          }}
          onCancel={() => setShowPowerOffConfirm(false)}
        />
      )}

      {/* Stop Print Confirmation */}
      {showStopConfirm && (
        <ConfirmModal
          title={t('printers.confirm.stopTitle')}
          message={t('printers.confirm.stopMessage', { name: printer.name })}
          confirmText={t('printers.confirm.stopButton')}
          variant="danger"
          onConfirm={() => {
            stopPrintMutation.mutate();
            setShowStopConfirm(false);
          }}
          onCancel={() => setShowStopConfirm(false)}
        />
      )}

      {/* Pause Print Confirmation */}
      {showPauseConfirm && (
        <ConfirmModal
          title={t('printers.confirm.pauseTitle')}
          message={t('printers.confirm.pauseMessage', { name: printer.name })}
          confirmText={t('printers.confirm.pauseButton')}
          variant="default"
          onConfirm={() => {
            pausePrintMutation.mutate();
            setShowPauseConfirm(false);
          }}
          onCancel={() => setShowPauseConfirm(false)}
        />
      )}

      {/* Resume Print Confirmation */}
      {showResumeConfirm && (
        <ConfirmModal
          title={t('printers.confirm.resumeTitle')}
          message={t('printers.confirm.resumeMessage', { name: printer.name })}
          confirmText={t('printers.confirm.resumeButton')}
          variant="default"
          onConfirm={() => {
            resumePrintMutation.mutate();
            setShowResumeConfirm(false);
          }}
          onCancel={() => setShowResumeConfirm(false)}
        />
      )}

      {/* Bed Jog — not-homed warning (Studio-style). Shown the first time a
          user tries to move the bed in a browser session; "Move anyway" wraps
          the jog with M211 S0/S1 to bypass soft endstops and remembers the
          choice in sessionStorage as bamdude.bedJog.warned.<printer_id>. */}
      {showNotHomedModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
          <div className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl w-full max-w-sm p-5">
            <div className="flex items-start gap-3 mb-4">
              <AlertTriangle className="w-5 h-5 text-yellow-400 flex-shrink-0 mt-0.5" />
              <div>
                <h3 className="text-sm font-semibold text-white mb-1">
                  {t('printers.bedJog.notHomedTitle')}
                </h3>
                <p className="text-xs text-bambu-gray leading-relaxed">
                  {t('printers.bedJog.notHomedMessage')}
                </p>
              </div>
            </div>
            <div className="flex flex-col gap-2">
              <button
                onClick={() => {
                  homeAxesMutation.mutate('all');
                  setShowNotHomedModal(null);
                }}
                className="w-full px-3 py-2 rounded-lg text-xs font-medium bg-bambu-green/20 text-bambu-green hover:bg-bambu-green/30 transition-colors"
              >
                {t('printers.bedJog.homeZ')}
              </button>
              <button
                onClick={() => {
                  const d = showNotHomedModal.distance;
                  try { sessionStorage.setItem(`bamdude.bedJog.warned.${printer.id}`, '1'); } catch { /* ignore */ }
                  bedJogMutation.mutate({ distance: d, force: true });
                  setShowNotHomedModal(null);
                }}
                className="w-full px-3 py-2 rounded-lg text-xs font-medium bg-yellow-500/20 text-yellow-400 hover:bg-yellow-500/30 transition-colors"
              >
                {t('printers.bedJog.moveAnyway')}
              </button>
              <button
                onClick={() => setShowNotHomedModal(null)}
                className="w-full px-3 py-2 rounded-lg text-xs font-medium bg-bambu-dark text-bambu-gray hover:bg-bambu-dark-tertiary transition-colors"
              >
                {t('common.cancel')}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Skip Objects Modal */}
      <SkipObjectsModal
        printerId={printer.id}
        isOpen={showSkipObjectsModal}
        onClose={() => setShowSkipObjectsModal(false)}
      />

      {/* HMS Error Modal */}
      {showHMSModal && (
        <HMSErrorModal
          printerName={printer.name}
          errors={status?.hms_errors || []}
          onClose={() => setShowHMSModal(false)}
          printerId={printer.id}
          hasPermission={hasPermission}
        />
      )}

      {/* AMS History Modal */}
      {amsHistoryModal && (
        <AMSHistoryModal
          isOpen={!!amsHistoryModal}
          onClose={() => setAmsHistoryModal(null)}
          printerId={printer.id}
          printerName={printer.name}
          amsId={amsHistoryModal.amsId}
          amsLabel={amsHistoryModal.amsLabel}
          initialMode={amsHistoryModal.mode}
          thresholds={amsThresholds}
        />
      )}

      {/* Link Spool Modal */}
      {linkSpoolModal && (
        <LinkSpoolModal
          isOpen={!!linkSpoolModal}
          onClose={() => setLinkSpoolModal(null)}
          tagUid={linkSpoolModal.tagUid}
          trayUuid={linkSpoolModal.trayUuid}
          printerId={linkSpoolModal.printerId}
          amsId={linkSpoolModal.amsId}
          trayId={linkSpoolModal.trayId}
        />
      )}

      {/* Assign Spool Modal */}
      {assignSpoolModal && (
        <AssignSpoolModal
          isOpen={!!assignSpoolModal}
          onClose={() => setAssignSpoolModal(null)}
          printerId={assignSpoolModal.printerId}
          amsId={assignSpoolModal.amsId}
          trayId={assignSpoolModal.trayId}
          trayInfo={assignSpoolModal.trayInfo}
          spoolmanEnabled={!!spoolmanEnabled}
        />
      )}

      {/* Configure AMS Slot Modal */}
      {configureSlotModal && (
        <ConfigureAmsSlotModal
          isOpen={!!configureSlotModal}
          onClose={() => setConfigureSlotModal(null)}
          printerId={printer.id}
          slotInfo={configureSlotModal}
          printerModel={mapModelCode(printer.model) || undefined}
          onSuccess={() => {
            // Refresh slot presets to show updated profile name
            queryClient.invalidateQueries({ queryKey: ['slotPresets', printer.id] });
            // Printer status will update automatically via WebSocket when AMS data changes
            queryClient.invalidateQueries({ queryKey: ['printerStatus', printer.id] });
          }}
        />
      )}

      {/* Edit Printer Modal */}
      {showEditModal && (
        <EditPrinterModal
          printer={printer}
          onClose={() => setShowEditModal(false)}
        />
      )}

      {/* Firmware Update Modal */}
      {showFirmwareModal && firmwareInfo && (
        <FirmwareUpdateModal
          printer={printer}
          firmwareInfo={firmwareInfo}
          onClose={() => setShowFirmwareModal(false)}
        />
      )}

      {/* AMS Slot Menu Backdrop - closes menu when clicking outside */}
      {amsSlotMenu && (
        <div
          className="fixed inset-0 z-40"
          onClick={() => setAmsSlotMenu(null)}
        />
      )}

      {/* AMS Drying Popover - fixed position to avoid overflow/z-index issues */}
      {dryingPopoverAmsId !== null && dryingPopoverPos && (() => {
        const maxTemp = dryingPopoverModuleType === 'n3s' ? 85 : 65;
        const sliderMin = 35;
        const sliderMax = maxTemp + 10;
        return (
          <>
            {/* Backdrop */}
            <div className="fixed inset-0 z-[100]" onClick={() => setDryingPopoverAmsId(null)} />
            {/* Popover */}
            <div
              className="fixed z-[101] w-[240px] bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-2xl overflow-hidden"
              style={{ top: dryingPopoverPos.top, left: dryingPopoverPos.left }}
              onClick={e => e.stopPropagation()}
            >
              {/* Header */}
              <div className="flex items-center gap-2 px-3 py-2.5 border-b border-bambu-dark-tertiary">
                <Flame className="w-3.5 h-3.5 text-amber-400" />
                <span className="text-xs text-white font-medium">{t('printers.drying.start')}</span>
              </div>
              {/* Body */}
              <div className="px-3 py-2.5 space-y-2.5">
                {/* Filament type select */}
                <div>
                  <label className="text-[10px] text-bambu-gray mb-1 block">{t('printers.filaments')}</label>
                  <select
                    value={dryingFilament}
                    onChange={e => {
                      const fil = e.target.value;
                      setDryingFilament(fil);
                      const preset = dryingPresets[fil];
                      if (preset) {
                        const key = dryingPopoverModuleType === 'n3s' ? 'n3s' : 'n3f';
                        setDryingTemp(preset[key]);
                        setDryingDuration(dryingPopoverModuleType === 'n3s' ? preset.n3s_hours : preset.n3f_hours);
                      }
                    }}
                    className="w-full px-2 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-xs focus:outline-none focus:border-amber-500/50"
                  >
                    {Object.keys(dryingPresets).map(fil => (
                      <option key={fil} value={fil}>{fil}</option>
                    ))}
                  </select>
                </div>
                {/* Temperature */}
                <div>
                  <div className="flex items-center justify-between mb-1">
                    <label className="text-[10px] text-bambu-gray">{t('printers.drying.temperature')}</label>
                    <div className="flex items-center gap-1">
                      <input
                        type="number"
                        min={45}
                        max={maxTemp}
                        value={dryingTemp}
                        onChange={e => setDryingTemp(Math.min(maxTemp, Math.max(45, Number(e.target.value) || 45)))}
                        className="w-12 px-1 py-0.5 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-[11px] text-center focus:outline-none focus:border-amber-500/50 [appearance:textfield] [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:appearance-none"
                      />
                      <span className="text-[10px] text-bambu-gray">°C</span>
                    </div>
                  </div>
                  <input
                    type="range"
                    min={sliderMin}
                    max={sliderMax}
                    value={dryingTemp}
                    onChange={e => setDryingTemp(Math.min(maxTemp, Math.max(45, Number(e.target.value))))}
                    className="w-full h-1 accent-amber-500 cursor-pointer"
                  />
                  <div className="flex justify-between text-[9px] text-bambu-gray/50 mt-0.5">
                    <span>45°C</span>
                    <span>{maxTemp}°C</span>
                  </div>
                </div>
                {/* Duration */}
                <div>
                  <div className="flex items-center justify-between mb-1">
                    <label className="text-[10px] text-bambu-gray">{t('printers.drying.duration')}</label>
                    <div className="flex items-center gap-1">
                      <input
                        type="number"
                        min={1}
                        max={24}
                        value={dryingDuration}
                        onChange={e => setDryingDuration(Math.min(24, Math.max(1, Number(e.target.value) || 1)))}
                        className="w-10 px-1 py-0.5 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-[11px] text-center focus:outline-none focus:border-amber-500/50 [appearance:textfield] [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:appearance-none"
                      />
                      <span className="text-[10px] text-bambu-gray">{t('printers.drying.hours')}</span>
                    </div>
                  </div>
                  <input
                    type="range"
                    min={1}
                    max={24}
                    value={dryingDuration}
                    onChange={e => setDryingDuration(Number(e.target.value))}
                    className="w-full h-1 accent-amber-500 cursor-pointer"
                  />
                  <div className="flex justify-between text-[9px] text-bambu-gray/50 mt-0.5">
                    <span>1h</span>
                    <span>24h</span>
                  </div>
                </div>
                {/* Rotate tray */}
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={dryingRotateTray}
                    onChange={e => setDryingRotateTray(e.target.checked)}
                    className="w-3.5 h-3.5 accent-amber-500 rounded cursor-pointer"
                  />
                  <span className="text-[11px] text-bambu-gray">{t('printers.drying.rotateTray')}</span>
                </label>
              </div>
              {/* Footer */}
              <div className="px-3 pb-3">
                <button
                  onClick={() => {
                    if (dryingPopoverAmsId !== null) {
                      startDryingMutation.mutate({ amsId: dryingPopoverAmsId, temp: dryingTemp, duration: dryingDuration, filament: dryingFilament, rotateTray: dryingRotateTray });
                    }
                  }}
                  disabled={startDryingMutation.isPending}
                  className="w-full py-1.5 bg-amber-500 hover:bg-amber-400 text-white text-xs font-medium rounded-lg transition-colors disabled:opacity-50"
                >
                  {startDryingMutation.isPending ? t('printers.drying.startingDrying') : t('printers.drying.start')}
                </button>
              </div>
            </div>
          </>
        );
      })()}
    </Card>
  );
}

function MacrosPanel({
  printer,
  macroExecuting,
  onClose,
}: {
  printer: Printer;
  macroExecuting: string | null;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const { showToast } = useToast();

  const { data: macros, isLoading } = useQuery({
    queryKey: ['macros'],
    queryFn: macrosApi.getMacros,
  });

  const executeMutation = useMutation({
    mutationFn: (macroId: number) => macrosApi.executeMacro(macroId, printer.id),
    onError: (error: Error) => {
      showToast(error.message, 'error');
    },
  });

  // Filter macros: match printer model + swap_mode requirement + swap_profile binding.
  const filteredMacros = (macros || []).filter((macro: Macro) => {
    if (!macro.enabled) return false;
    if (!macro.gcode || !macro.gcode.trim()) return false;
    const models = macro.printer_models;
    if (!models.includes('*') && (!printer.model || !models.includes(printer.model))) return false;
    if (macro.swap_mode_only && !printer.swap_mode_enabled) return false;
    // Profile-bound macro → only for printers with the same profile selected.
    // Generic (null) macros stay visible on every printer.
    if (macro.swap_profile && macro.swap_profile !== printer.swap_profile) return false;
    return true;
  });

  const isBusy = executeMutation.isPending || !!macroExecuting;

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div
        className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl w-full max-w-sm shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-bambu-dark-tertiary">
          <h3 className="text-sm font-semibold text-white">{t('printers.macros')} - {printer.name}</h3>
          <button onClick={onClose} className="text-bambu-gray hover:text-white">
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="p-3 max-h-64 overflow-y-auto">
          {isLoading ? (
            <div className="flex justify-center py-4">
              <Loader2 className="w-5 h-5 animate-spin text-bambu-gray" />
            </div>
          ) : filteredMacros.length === 0 ? (
            <p className="text-sm text-bambu-gray text-center py-4">{t('printers.noMacros')}</p>
          ) : (
            <div className="space-y-1">
              {filteredMacros.map((macro: Macro) => {
                const isSending = executeMutation.isPending && executeMutation.variables === macro.id;
                const isRunning = macroExecuting === macro.name;
                return (
                  <button
                    key={macro.id}
                    className="w-full px-3 py-2 text-left text-sm text-white hover:bg-bambu-dark-tertiary rounded-lg flex items-center justify-between gap-2 disabled:opacity-50"
                    onClick={() => executeMutation.mutate(macro.id)}
                    disabled={isBusy}
                  >
                    <div>
                      <div className="font-medium">{macro.name}</div>
                      <div className="text-xs text-bambu-gray">
                        {isRunning
                          ? t('printers.macroAwaitingResponse')
                          : `${macro.gcode.split('\n').length} ${t('printers.macroLines')}`}
                      </div>
                    </div>
                    {isSending || isRunning ? (
                      <Loader2 className="w-4 h-4 animate-spin text-bambu-green flex-shrink-0" />
                    ) : (
                      <Play className="w-4 h-4 text-bambu-gray flex-shrink-0" />
                    )}
                  </button>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}


function AddPrinterModal({
  onClose,
  onAdd,
  existingSerials,
}: {
  onClose: () => void;
  onAdd: (data: PrinterCreate) => void;
  existingSerials: string[];
}) {
  const { t } = useTranslation();
  const [form, setForm] = useState<PrinterCreate>({
    name: '',
    serial_number: '',
    ip_address: '',
    access_code: '',
    model: '',
    location: '',
    auto_archive: true,
    cleanup_after_print: false,
    mqtt_connection_timeout: 900,
    stagger_interval_minutes: 0,
    swap_mode_enabled: false,
    swap_profile: null,
    require_plate_clear: true,
  });

  // Swap profile catalog for dropdowns in the form.
  const { data: swapProfiles } = useQuery({
    queryKey: ['macros', 'swap-profiles'],
    queryFn: macrosApi.getSwapProfiles,
    staleTime: Infinity,
  });

  const [showAccessCode, setShowAccessCode] = useState(false);

  // Discovery state
  const [discovering, setDiscovering] = useState(false);
  const [discovered, setDiscovered] = useState<DiscoveredPrinter[]>([]);
  const [discoveryError, setDiscoveryError] = useState('');
  const [hasScanned, setHasScanned] = useState(false);
  const [isDocker, setIsDocker] = useState(false);
  const [detectedSubnets, setDetectedSubnets] = useState<string[]>([]);
  const [subnet, setSubnet] = useState('');
  const [scanProgress, setScanProgress] = useState({ scanned: 0, total: 0 });

  // Fetch discovery info on mount
  useEffect(() => {
    discoveryApi.getInfo().then(info => {
      setIsDocker(info.is_docker);
      if (info.subnets.length > 0) {
        setDetectedSubnets(info.subnets);
        setSubnet(info.subnets[0]);
      }
    }).catch(() => {
      // Ignore errors, assume not Docker
    });
  }, []);

  // Filter out already-added printers
  const newPrinters = discovered.filter(p => !existingSerials.includes(p.serial));

  const startDiscovery = async () => {
    setDiscoveryError('');
    setDiscovered([]);
    setDiscovering(true);
    setHasScanned(false);
    setScanProgress({ scanned: 0, total: 0 });

    try {
      if (isDocker) {
        // Use subnet scanning for Docker
        await discoveryApi.startSubnetScan(subnet);

        // Poll for scan status and results
        const pollInterval = setInterval(async () => {
          try {
            const status = await discoveryApi.getScanStatus();
            setScanProgress({ scanned: status.scanned, total: status.total });

            const printers = await discoveryApi.getDiscoveredPrinters();
            setDiscovered(printers);

            if (!status.running) {
              clearInterval(pollInterval);
              setDiscovering(false);
              setHasScanned(true);
            }
          } catch (e) {
            console.error('Failed to get scan status:', e);
          }
        }, 500);
      } else {
        // Use SSDP discovery for native installs
        await discoveryApi.startDiscovery(10);

        // Poll for discovered printers every second
        const pollInterval = setInterval(async () => {
          try {
            const printers = await discoveryApi.getDiscoveredPrinters();
            setDiscovered(printers);
          } catch (e) {
            console.error('Failed to get discovered printers:', e);
          }
        }, 1000);

        // Stop after 10 seconds
        setTimeout(async () => {
          clearInterval(pollInterval);
          try {
            await discoveryApi.stopDiscovery();
          } catch {
            // Ignore stop errors
          }
          setDiscovering(false);
          setHasScanned(true);
          // Final fetch
          try {
            const printers = await discoveryApi.getDiscoveredPrinters();
            setDiscovered(printers);
          } catch (e) {
            console.error('Failed to get final discovered printers:', e);
          }
        }, 10000);
      }
    } catch (e) {
      console.error('Failed to start discovery:', e);
      setDiscoveryError(e instanceof Error ? e.message : t('printers.discovery.failedToStart'));
      setDiscovering(false);
      setHasScanned(true);
    }
  };

  const selectPrinter = (printer: DiscoveredPrinter) => {
    // Don't pre-fill serial if it's a placeholder (unknown-*) - user needs to enter actual serial
    const serialNumber = printer.serial.startsWith('unknown-') ? '' : printer.serial;
    setForm({
      ...form,
      name: printer.name || '',
      serial_number: serialNumber,
      ip_address: printer.ip_address,
      model: mapModelCode(printer.model),
    });
    // Clear discovery results after selection
    setDiscovered([]);
  };

  // Cleanup discovery on unmount
  useEffect(() => {
    return () => {
      discoveryApi.stopDiscovery().catch(() => {});
      discoveryApi.stopSubnetScan().catch(() => {});
    };
  }, []);

  // Close on Escape key
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
      onClick={onClose}
    >
      <Card className="w-full max-w-2xl max-h-[90vh] overflow-y-auto" onClick={(e: React.MouseEvent) => e.stopPropagation()}>
        <CardContent>
          <h2 className="text-xl font-semibold mb-4">{t('printers.addPrinter')}</h2>

          {/* Discovery Section - full width */}
          <div className="mb-4 pb-4 border-b border-bambu-dark-tertiary">
            {isDocker && (
              <div className="mb-3">
                <label className="block text-sm text-bambu-gray mb-1">
                  {t('printers.discovery.subnetToScan')}
                </label>
                {detectedSubnets.length > 0 ? (
                  <select
                    className="w-full px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none text-sm"
                    value={subnet}
                    onChange={(e) => setSubnet(e.target.value)}
                    disabled={discovering}
                  >
                    {detectedSubnets.map(s => (
                      <option key={s} value={s}>{s}</option>
                    ))}
                  </select>
                ) : (
                  <input
                    type="text"
                    className="w-full px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none text-sm"
                    value={subnet}
                    onChange={(e) => setSubnet(e.target.value)}
                    placeholder="192.168.1.0/24"
                    disabled={discovering}
                  />
                )}
                <p className="mt-1 text-xs text-bambu-gray">
                  {t('printers.discovery.dockerNote')}
                </p>
              </div>
            )}

            <Button
              type="button"
              variant="secondary"
              onClick={startDiscovery}
              disabled={discovering}
              className="w-full"
            >
              {discovering ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  {isDocker && scanProgress.total > 0
                    ? t('printers.discovery.scanProgress', { scanned: scanProgress.scanned, total: scanProgress.total })
                    : t('printers.discovery.scanning')}
                </>
              ) : (
                <>
                  <Search className="w-4 h-4" />
                  {isDocker ? t('printers.discovery.scanSubnet') : t('printers.discovery.discoverNetwork')}
                </>
              )}
            </Button>

            {discoveryError && (
              <div className="mt-2 text-sm text-red-400">{discoveryError}</div>
            )}

            {newPrinters.length > 0 && (
              <div className="mt-3 space-y-2 max-h-40 overflow-y-auto">
                {newPrinters.map((printer) => (
                  <div
                    key={printer.serial}
                    className="flex items-center justify-between p-2 bg-bambu-dark rounded-lg hover:bg-bambu-dark-secondary cursor-pointer transition-colors"
                    onClick={() => selectPrinter(printer)}
                  >
                    <div className="min-w-0 flex-1">
                      <p className="font-medium text-white text-sm truncate">
                        {printer.name || printer.serial}
                      </p>
                      <p className="text-xs text-bambu-gray truncate">
                        {mapModelCode(printer.model) || t('printers.discovery.unknown')} • {printer.ip_address}
                        {printer.serial.startsWith('unknown-') && (
                          <span className="text-yellow-500"> • {t('printers.discovery.serialRequired')}</span>
                        )}
                      </p>
                    </div>
                    <ChevronDown className="w-4 h-4 text-bambu-gray -rotate-90 flex-shrink-0 ml-2" />
                  </div>
                ))}
              </div>
            )}

            {discovering && (
              <p className="mt-2 text-sm text-bambu-gray text-center">
                {isDocker ? t('printers.discovery.scanningSubnet') : t('printers.discovery.scanningNetwork')}
              </p>
            )}

            {hasScanned && !discovering && discovered.length === 0 && (
              <p className="mt-2 text-sm text-bambu-gray text-center">
                {isDocker ? t('printers.discovery.noPrintersFoundSubnet') : t('printers.discovery.noPrintersFoundNetwork')}
              </p>
            )}

            {hasScanned && !discovering && discovered.length > 0 && newPrinters.length === 0 && (
              <p className="mt-2 text-sm text-bambu-gray text-center">
                {t('printers.discovery.allConfigured')}
              </p>
            )}
          </div>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              onAdd(form);
            }}
          >
            {/* Two-column grid */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {/* Left column - Connection */}
              <div className="space-y-3">
                <div>
                  <label className="block text-sm text-bambu-gray mb-1">{t('printers.name')}</label>
                  <input
                    type="text"
                    required
                    className="w-full px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                    value={form.name}
                    onChange={(e) => setForm({ ...form, name: e.target.value })}
                    placeholder={t('printers.modal.myPrinter')}
                  />
                </div>
                <div>
                  <label className="block text-sm text-bambu-gray mb-1">{t('printers.ipAddress')}</label>
                  <input
                    type="text"
                    required
                    pattern="(\d{1,3}(\.\d{1,3}){3}|[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*)"
                    className="w-full px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                    value={form.ip_address}
                    onChange={(e) => setForm({ ...form, ip_address: e.target.value })}
                    placeholder="192.168.1.100 or printer.local"
                  />
                </div>
                <div>
                  <label className="block text-sm text-bambu-gray mb-1">{t('printers.serialNumber')}</label>
                  <input
                    type="text"
                    required
                    className="w-full px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                    value={form.serial_number}
                    onChange={(e) => setForm({ ...form, serial_number: e.target.value })}
                    placeholder="01P00A000000000"
                  />
                </div>
                <div>
                  <label className="block text-sm text-bambu-gray mb-1">{t('printers.accessCode')}</label>
                  <div className="relative">
                    <input
                      type={showAccessCode ? 'text' : 'password'}
                      required
                      className="w-full px-3 py-1.5 pr-10 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                      value={form.access_code}
                      onChange={(e) => setForm({ ...form, access_code: e.target.value })}
                      placeholder={t('printers.modal.fromPrinterSettings')}
                    />
                    <button
                      type="button"
                      onClick={() => setShowAccessCode(!showAccessCode)}
                      className="absolute right-2.5 top-1/2 -translate-y-1/2 text-bambu-gray hover:text-white transition-colors"
                      tabIndex={-1}
                    >
                      {showAccessCode ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                    </button>
                  </div>
                </div>
                <div>
                  <label className="block text-sm text-bambu-gray mb-1">{t('printers.modal.modelOptional')}</label>
                  <select
                    className="w-full px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                    value={form.model || ''}
                    onChange={(e) => setForm({ ...form, model: e.target.value })}
                  >
                    <option value="">{t('printers.modal.selectModel')}</option>
                    <optgroup label="H2 Series">
                      <option value="H2C">H2C</option>
                      <option value="H2D">H2D</option>
                      <option value="H2D Pro">H2D Pro</option>
                      <option value="H2S">H2S</option>
                    </optgroup>
                    <optgroup label="X2 Series">
                      <option value="X2D">X2D</option>
                    </optgroup>
                    <optgroup label="X1 Series">
                      <option value="X1E">X1E</option>
                      <option value="X1C">X1 Carbon</option>
                      <option value="X1">X1</option>
                    </optgroup>
                    <optgroup label="P Series">
                      <option value="P2S">P2S</option>
                      <option value="P1S">P1S</option>
                      <option value="P1P">P1P</option>
                    </optgroup>
                    <optgroup label="A1 Series">
                      <option value="A1">A1</option>
                      <option value="A1 Mini">A1 Mini</option>
                    </optgroup>
                  </select>
                </div>
                <div>
                  <label className="block text-sm text-bambu-gray mb-1">{t('printers.modal.locationGroup')}</label>
                  <input
                    type="text"
                    className="w-full px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                    value={form.location || ''}
                    onChange={(e) => setForm({ ...form, location: e.target.value })}
                    placeholder={t('printers.modal.locationPlaceholder')}
                  />
                  <p className="text-xs text-bambu-gray mt-1">{t('printers.locationHelp')}</p>
                </div>
              </div>

              {/* Right column - Settings */}
              <div className="space-y-3">
                <div>
                  <div className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      id="cleanup_after_print"
                      checked={form.cleanup_after_print}
                      onChange={(e) => setForm({ ...form, cleanup_after_print: e.target.checked })}
                      className="rounded border-bambu-dark-tertiary bg-bambu-dark text-bambu-green focus:ring-bambu-green"
                    />
                    <label htmlFor="cleanup_after_print" className="text-sm text-bambu-gray">
                      {t('printers.modal.cleanupAfterPrintLabel')}
                    </label>
                  </div>
                  <p className="text-xs text-bambu-gray mt-1 ml-6">{t('printers.modal.cleanupAfterPrintHint')}</p>
                </div>
                <div>
                  <label className="block text-sm font-medium text-bambu-gray mb-1">
                    {t('printers.modal.mqttConnectionTimeoutLabel')}
                  </label>
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      min="0"
                      max="3600"
                      value={form.mqtt_connection_timeout}
                      onChange={(e) => setForm({ ...form, mqtt_connection_timeout: parseInt(e.target.value) || 0 })}
                      className="w-24 px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:ring-1 focus:ring-bambu-green"
                    />
                    <span className="text-xs text-bambu-gray">{t('printers.modal.mqttConnectionTimeoutHint')}</span>
                  </div>
                </div>
                <div>
                  <label className="block text-sm text-bambu-gray mb-1">{t('printers.modal.staggerInterval')}</label>
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      min="0"
                      max="60"
                      value={form.stagger_interval_minutes ?? 0}
                      onChange={(e) => setForm({ ...form, stagger_interval_minutes: parseInt(e.target.value) || 0 })}
                      className="w-24 px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:ring-1 focus:ring-bambu-green"
                    />
                    <span className="text-xs text-bambu-gray">{t('printers.modal.staggerIntervalHint')}</span>
                  </div>
                </div>
                {(() => {
                  const modelProfiles = (swapProfiles ?? []).filter((p) =>
                    form.model ? p.models.includes(form.model) : false
                  );
                  if (modelProfiles.length === 0) return null;
                  return (
                    <>
                      <div>
                        <label className="flex items-center gap-2 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={form.swap_mode_enabled ?? false}
                            onChange={(e) => {
                              const enabled = e.target.checked;
                              setForm({
                                ...form,
                                swap_mode_enabled: enabled,
                                swap_profile: enabled
                                  ? (form.swap_profile ?? modelProfiles[0]?.id ?? null)
                                  : null,
                                ...(enabled ? { require_plate_clear: false } : {}),
                              });
                            }}
                            className="w-4 h-4 rounded border-bambu-dark-tertiary bg-bambu-dark text-bambu-green focus:ring-bambu-green"
                          />
                          <span className="text-sm text-white">{t('printers.modal.swapMode')}</span>
                        </label>
                        <p className="text-xs text-bambu-gray mt-1 ml-6">{t('printers.modal.swapModeHint')}</p>
                      </div>
                      {form.swap_mode_enabled && (
                        <div className="ml-6">
                          <label className="block text-xs text-bambu-gray mb-1">
                            {t('printers.modal.swapProfile')}
                          </label>
                          <select
                            value={form.swap_profile ?? ''}
                            onChange={(e) => setForm({ ...form, swap_profile: e.target.value || null })}
                            className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none text-sm"
                          >
                            {modelProfiles.map((p) => (
                              <option key={p.id} value={p.id}>{p.label}</option>
                            ))}
                          </select>
                          {form.swap_profile && (
                            <p className="text-xs text-bambu-gray mt-1">
                              {modelProfiles.find((p) => p.id === form.swap_profile)?.description}
                            </p>
                          )}
                        </div>
                      )}
                    </>
                  );
                })()}
                {!form.swap_mode_enabled && (
                <div>
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={form.require_plate_clear ?? true}
                      onChange={(e) => setForm({ ...form, require_plate_clear: e.target.checked })}
                      className="w-4 h-4 rounded border-bambu-dark-tertiary bg-bambu-dark text-bambu-green focus:ring-bambu-green"
                    />
                    <span className="text-sm text-white">{t('printers.modal.requirePlateClear')}</span>
                  </label>
                  <p className="text-xs text-bambu-gray mt-1 ml-6">{t('printers.modal.requirePlateClearHint')}</p>
                </div>
                )}
              </div>
            </div>

            {/* Buttons - full width */}
            <div className="flex gap-3 pt-4">
              <Button type="button" variant="secondary" onClick={onClose} className="flex-1">
                {t('common.cancel')}
              </Button>
              <Button type="submit" className="flex-1">
                {t('printers.addPrinter')}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}

function FirmwareUpdateModal({
  printer,
  firmwareInfo,
  onClose,
}: {
  printer: Printer;
  firmwareInfo: FirmwareUpdateInfo;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { hasPermission } = useAuth();
  const canUpdate = hasPermission('firmware:update');
  const [uploadStatus, setUploadStatus] = useState<FirmwareUploadStatus | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [pollInterval, setPollInterval] = useState<NodeJS.Timeout | null>(null);
  const [selectedVersion, setSelectedVersion] = useState<string | null>(
    firmwareInfo.update_available ? firmwareInfo.latest_version : null,
  );

  // Prepare check query — runs when a version is selected and user can update.
  // Keying on selectedVersion so switching targets re-runs the pre-flight.
  const { data: prepareInfo, isLoading: isPreparing } = useQuery({
    queryKey: ['firmwarePrepare', printer.id, selectedVersion],
    queryFn: () => firmwareApi.prepareUpload(printer.id, selectedVersion ?? undefined),
    staleTime: 30000,
    enabled: !!selectedVersion && canUpdate && !isUploading,
  });

  // Start upload mutation
  const uploadMutation = useMutation({
    mutationFn: () => firmwareApi.startUpload(printer.id, selectedVersion ?? undefined),
    onSuccess: () => {
      setIsUploading(true);
      // Start polling for status
      const interval = setInterval(async () => {
        try {
          const status = await firmwareApi.getUploadStatus(printer.id);
          setUploadStatus(status);
          if (status.status === 'complete' || status.status === 'error') {
            clearInterval(interval);
            setPollInterval(null);
            setIsUploading(false);
            if (status.status === 'complete') {
              showToast(t('printers.firmwareModal.uploadedToast'), 'success');
              queryClient.invalidateQueries({ queryKey: ['firmwareUpdate', printer.id] });
            }
          }
        } catch {
          // Ignore errors during polling
        }
      }, 2000);
      setPollInterval(interval);
    },
    onError: (error: Error) => {
      showToast(t('printers.firmwareModal.uploadFailed', { error: error.message }), 'error');
      setIsUploading(false);
    },
  });

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (pollInterval) clearInterval(pollInterval);
    };
  }, [pollInterval]);

  const handleStartUpload = () => {
    setUploadStatus(null);
    uploadMutation.mutate();
  };

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <Card className="w-full max-w-md mx-4">
        <CardContent>
          <div className="flex items-start gap-3 mb-4">
            <div className={`p-2 rounded-full ${firmwareInfo.update_available ? 'bg-orange-500/20' : 'bg-status-ok/20'}`}>
              {firmwareInfo.update_available
                ? <Download className="w-5 h-5 text-orange-400" />
                : <CheckCircle className="w-5 h-5 text-status-ok" />}
            </div>
            <div className="flex-1">
              <h3 className="text-lg font-semibold text-white">
                {firmwareInfo.update_available ? t('printers.firmwareModal.title') : t('printers.firmwareModal.titleUpToDate')}
              </h3>
              <p className="text-sm text-bambu-gray mt-1">
                {printer.name}
              </p>
            </div>
          </div>

          {/* Version Info — displays the currently-selected target's release notes,
              or the latest version's when nothing is selected. */}
          {(() => {
            const selectedEntry = selectedVersion
              ? firmwareInfo.available_versions?.find((v) => v.version === selectedVersion)
              : null;
            const displayVersion = selectedVersion ?? firmwareInfo.latest_version;
            const displayNotes = selectedEntry?.release_notes ?? firmwareInfo.release_notes;
            const showSecondLine = !!displayVersion && displayVersion !== firmwareInfo.current_version;
            return (
              <div className="bg-bambu-dark rounded-lg p-3 mb-4">
                <div className="flex justify-between items-center text-sm">
                  <span className="text-bambu-gray">{t('printers.firmwareModal.currentVersion')}</span>
                  <span className={`font-mono ${showSecondLine ? 'text-white' : 'text-status-ok'}`}>
                    {firmwareInfo.current_version || t('common.unknown')}
                  </span>
                </div>
                {showSecondLine && (
                  <div className="flex justify-between items-center text-sm mt-1">
                    <span className="text-bambu-gray">{t('printers.firmwareModal.latestVersion')}</span>
                    <span className="text-orange-400 font-mono">{displayVersion}</span>
                  </div>
                )}
                {displayNotes && (
                  <details className="mt-3 text-sm" open={!showSecondLine} key={displayVersion ?? 'none'}>
                    <summary className={`cursor-pointer hover:underline ${showSecondLine ? 'text-orange-400' : 'text-status-ok'}`}>
                      {t('printers.firmwareModal.releaseNotes')}
                    </summary>
                    <div className="mt-2 text-bambu-gray text-xs max-h-40 overflow-y-auto whitespace-pre-wrap">
                      {displayNotes}
                    </div>
                  </details>
                )}
              </div>
            );
          })()}

          {/* Available versions list — newest-first picker supporting rollback.
              Hidden once upload is running or completed. */}
          {firmwareInfo.available_versions && firmwareInfo.available_versions.length > 0 && !isUploading && uploadStatus?.status !== 'complete' && (
            <div className="mb-4">
              <div className="text-xs text-bambu-gray mb-2">{t('printers.firmwareModal.availableVersions')}</div>
              <div className="max-h-56 overflow-y-auto border border-bambu-dark-tertiary rounded-lg divide-y divide-bambu-dark-tertiary">
                {firmwareInfo.available_versions.map((v) => {
                  const isCurrent = firmwareInfo.current_version === v.version;
                  const isSelected = selectedVersion === v.version;
                  const cmp = firmwareInfo.current_version
                    ? compareFwVersions(v.version, firmwareInfo.current_version)
                    : 0;
                  const relLabel = isCurrent
                    ? t('printers.firmwareModal.currentBadge')
                    : cmp > 0
                      ? t('printers.firmwareModal.newerBadge')
                      : t('printers.firmwareModal.olderBadge');
                  const relClass = isCurrent
                    ? 'text-bambu-gray'
                    : cmp > 0
                      ? 'text-orange-400'
                      : 'text-blue-400';
                  return (
                    <button
                      key={v.version}
                      type="button"
                      disabled={!v.file_available || !canUpdate || isCurrent}
                      onClick={() => setSelectedVersion(v.version)}
                      className={`w-full text-left px-3 py-2 text-sm flex items-center justify-between gap-2 transition-colors ${
                        isSelected ? 'bg-orange-500/10' : 'hover:bg-bambu-dark'
                      } ${!v.file_available || !canUpdate || isCurrent ? 'opacity-60 cursor-not-allowed' : 'cursor-pointer'}`}
                    >
                      <div className="flex items-center gap-2 min-w-0">
                        <span className="font-mono text-white">{v.version}</span>
                        <span className={`text-xs ${relClass}`}>{relLabel}</span>
                      </div>
                      <span className={`text-xs px-2 py-0.5 rounded-full ${
                        isCurrent
                          ? 'bg-blue-500/15 text-blue-400 border border-blue-500/30'
                          : v.file_available
                            ? 'bg-bambu-green/15 text-bambu-green border border-bambu-green/30'
                            : 'bg-bambu-gray/10 text-bambu-gray border border-bambu-gray/30'
                      }`}>
                        {isCurrent
                          ? t('printers.firmwareModal.installed')
                          : v.file_available
                          ? t('printers.firmwareModal.usable')
                          : t('printers.firmwareModal.unavailable')}
                      </span>
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {/* Status / Progress (only when a version is selected) */}
          {!selectedVersion ? null : isPreparing ? (
            <div className="flex items-center gap-2 text-bambu-gray text-sm mb-4">
              <Loader2 className="w-4 h-4 animate-spin" />
              {t('printers.firmwareModal.checkingPrereqs')}
            </div>
          ) : prepareInfo && !isUploading && !uploadStatus ? (
            <div className="mb-4">
              {prepareInfo.can_proceed ? (
                <div className="flex items-center gap-2 text-bambu-green text-sm">
                  <Box className="w-4 h-4" />
                  {t('printers.firmwareModal.sdCardReady')}
                </div>
              ) : (
                <div className="space-y-1">
                  {prepareInfo.errors.map((error, i) => (
                    <div key={i} className="flex items-center gap-2 text-red-400 text-sm">
                      <AlertCircle className="w-4 h-4 flex-shrink-0" />
                      {error}
                    </div>
                  ))}
                </div>
              )}
            </div>
          ) : null}

          {/* Upload Progress */}
          {(isUploading || uploadStatus) && uploadStatus && (
            <div className="mb-4">
              <div className="flex items-center justify-between text-sm mb-1">
                <span className="text-bambu-gray capitalize">{uploadStatus.status}</span>
                <span className="text-white">{uploadStatus.progress}%</span>
              </div>
              <div className="w-full bg-bambu-dark-tertiary rounded-full h-2">
                <div
                  className={`h-2 rounded-full transition-all ${
                    uploadStatus.status === 'error' ? 'bg-status-error' :
                    uploadStatus.status === 'complete' ? 'bg-status-ok' : 'bg-orange-500'
                  } ${uploadStatus.status === 'uploading' ? 'animate-pulse' : ''}`}
                  style={{ width: `${uploadStatus.progress}%` }}
                />
              </div>
              <p className="text-xs text-bambu-gray mt-1">{uploadStatus.message}</p>
              {uploadStatus.error && (
                <p className="text-xs text-red-400 mt-1">{uploadStatus.error}</p>
              )}
            </div>
          )}

          {/* Success Message */}
          {uploadStatus?.status === 'complete' && (
            <div className="bg-bambu-green/10 border border-bambu-green/30 rounded-lg p-3 mb-4">
              <p className="text-sm text-bambu-green font-medium mb-2">
                {t('printers.firmwareModal.uploadedSuccess')}
              </p>
              <p className="text-xs text-bambu-gray">
                {t('printers.firmwareModal.applyInstructions')}
              </p>
              <ol className="text-xs text-bambu-gray mt-1 list-decimal list-inside space-y-1">
                <li dangerouslySetInnerHTML={{ __html: t('printers.firmwareModal.step1') }} />
                <li dangerouslySetInnerHTML={{ __html: t('printers.firmwareModal.step2') }} />
                <li dangerouslySetInnerHTML={{ __html: t('printers.firmwareModal.step3') }} />
                <li>{t('printers.firmwareModal.step4')}</li>
              </ol>
            </div>
          )}

          {/* Buttons */}
          <div className="flex gap-2 justify-end">
            <Button variant="secondary" onClick={onClose}>
              {uploadStatus?.status === 'complete' ? t('printers.firmwareModal.done') : t('common.cancel')}
            </Button>
            {prepareInfo?.can_proceed && !isUploading && uploadStatus?.status !== 'complete' && canUpdate && (
              <Button
                onClick={handleStartUpload}
                disabled={uploadMutation.isPending}
              >
                {uploadMutation.isPending ? (
                  <>
                    <Loader2 className="w-4 h-4 animate-spin mr-2" />
                    {t('printers.firmwareModal.starting')}
                  </>
                ) : (
                  <>
                    <Download className="w-4 h-4 mr-2" />
                    {t('printers.firmwareModal.uploadFirmware')}
                  </>
                )}
              </Button>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

function EditPrinterModal({
  printer,
  onClose,
}: {
  printer: Printer;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const [form, setForm] = useState({
    name: printer.name,
    ip_address: printer.ip_address,
    access_code: '',
    model: printer.model || '',
    location: printer.location || '',
    auto_archive: printer.auto_archive,
    cleanup_after_print: printer.cleanup_after_print ?? false,
    mqtt_connection_timeout: printer.mqtt_connection_timeout ?? 900,
    stagger_interval_minutes: printer.stagger_interval_minutes ?? 0,
    swap_mode_enabled: printer.swap_mode_enabled ?? false,
    swap_profile: (printer.swap_profile ?? null) as string | null,
    require_plate_clear: printer.require_plate_clear ?? true,
  });

  // Swap profile catalog for the dropdown (same query as add-form).
  const { data: swapProfiles } = useQuery({
    queryKey: ['macros', 'swap-profiles'],
    queryFn: macrosApi.getSwapProfiles,
    staleTime: Infinity,
  });

  const [showAccessCode, setShowAccessCode] = useState(false);

  const updateMutation = useMutation({
    mutationFn: (data: Partial<PrinterCreate>) => api.updatePrinter(printer.id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['printers'] });
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printer.id] });
      onClose();
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToUpdate'), 'error'),
  });

  // Close on Escape key
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const data: Partial<PrinterCreate> = {
      name: form.name,
      ip_address: form.ip_address,
      model: form.model || undefined,
      location: form.location || undefined,
      auto_archive: form.auto_archive,
      cleanup_after_print: form.cleanup_after_print,
      mqtt_connection_timeout: form.mqtt_connection_timeout,
      stagger_interval_minutes: form.stagger_interval_minutes,
      swap_mode_enabled: form.swap_mode_enabled,
      swap_profile: form.swap_mode_enabled ? form.swap_profile : null,
      require_plate_clear: form.swap_mode_enabled ? false : form.require_plate_clear,
    };
    // Only include access_code if it was changed
    if (form.access_code) {
      data.access_code = form.access_code;
    }
    updateMutation.mutate(data);
  };

  return (
    <div
      className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
      onClick={onClose}
    >
      <Card className="w-full max-w-2xl max-h-[90vh] overflow-y-auto" onClick={(e: React.MouseEvent) => e.stopPropagation()}>
        <CardContent>
          <h2 className="text-xl font-semibold mb-4">{t('printers.editPrinter')}</h2>
          <form onSubmit={handleSubmit}>
            {/* Two-column grid */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {/* Left column - Connection */}
              <div className="space-y-3">
                <div>
                  <label className="block text-sm text-bambu-gray mb-1">{t('printers.name')}</label>
                  <input
                    type="text"
                    required
                    className="w-full px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                    value={form.name}
                    onChange={(e) => setForm({ ...form, name: e.target.value })}
                    placeholder={t('printers.modal.myPrinter')}
                  />
                </div>
                <div>
                  <label className="block text-sm text-bambu-gray mb-1">{t('printers.ipAddress')}</label>
                  <input
                    type="text"
                    required
                    pattern="(\d{1,3}(\.\d{1,3}){3}|[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*)"
                    className="w-full px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                    value={form.ip_address}
                    onChange={(e) => setForm({ ...form, ip_address: e.target.value })}
                    placeholder="192.168.1.100 or printer.local"
                  />
                </div>
                <div>
                  <label className="block text-sm text-bambu-gray mb-1">{t('printers.serialNumber')}</label>
                  <input
                    type="text"
                    disabled
                    className="w-full px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-bambu-gray cursor-not-allowed"
                    value={printer.serial_number}
                  />
                  <p className="text-xs text-bambu-gray mt-1">{t('printers.serialCannotBeChanged')}</p>
                </div>
                <div>
                  <label className="block text-sm text-bambu-gray mb-1">{t('printers.accessCode')}</label>
                  <div className="relative">
                    <input
                      type={showAccessCode ? 'text' : 'password'}
                      className="w-full px-3 py-1.5 pr-10 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                      value={form.access_code}
                      onChange={(e) => setForm({ ...form, access_code: e.target.value })}
                      placeholder={t('printers.accessCodePlaceholder')}
                    />
                    <button
                      type="button"
                      onClick={() => setShowAccessCode(!showAccessCode)}
                      className="absolute right-2.5 top-1/2 -translate-y-1/2 text-bambu-gray hover:text-white transition-colors"
                      tabIndex={-1}
                    >
                      {showAccessCode ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                    </button>
                  </div>
                </div>
                <div>
                  <label className="block text-sm text-bambu-gray mb-1">{t('printers.model')}</label>
                  <select
                    className="w-full px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                    value={form.model}
                    onChange={(e) => setForm({ ...form, model: e.target.value })}
                  >
                    <option value="">{t('printers.modal.selectModel')}</option>
                    <optgroup label="H2 Series">
                      <option value="H2C">H2C</option>
                      <option value="H2D">H2D</option>
                      <option value="H2D Pro">H2D Pro</option>
                      <option value="H2S">H2S</option>
                    </optgroup>
                    <optgroup label="X2 Series">
                      <option value="X2D">X2D</option>
                    </optgroup>
                    <optgroup label="X1 Series">
                      <option value="X1E">X1E</option>
                      <option value="X1C">X1 Carbon</option>
                      <option value="X1">X1</option>
                    </optgroup>
                    <optgroup label="P Series">
                      <option value="P2S">P2S</option>
                      <option value="P1S">P1S</option>
                      <option value="P1P">P1P</option>
                    </optgroup>
                    <optgroup label="A1 Series">
                      <option value="A1">A1</option>
                      <option value="A1 Mini">A1 Mini</option>
                    </optgroup>
                  </select>
                </div>
                <div>
                  <label className="block text-sm text-bambu-gray mb-1">Location / Group</label>
                  <input
                    type="text"
                    className="w-full px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                    value={form.location}
                    onChange={(e) => setForm({ ...form, location: e.target.value })}
                    placeholder={t('printers.modal.locationPlaceholder')}
                  />
                  <p className="text-xs text-bambu-gray mt-1">{t('printers.locationHelp')}</p>
                </div>
              </div>

              {/* Right column - Settings */}
              <div className="space-y-3">
                <div className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    id="edit_cleanup_after_print"
                    checked={form.cleanup_after_print}
                    onChange={(e) => setForm({ ...form, cleanup_after_print: e.target.checked })}
                    className="rounded border-bambu-dark-tertiary bg-bambu-dark text-bambu-green focus:ring-bambu-green"
                  />
                  <label htmlFor="edit_cleanup_after_print" className="text-sm text-bambu-gray">
                    {t('printers.modal.cleanupAfterPrintLabel')}
                  </label>
                </div>
                <div>
                  <label className="block text-sm font-medium text-bambu-gray mb-1">
                    {t('printers.modal.mqttConnectionTimeoutLabel')}
                  </label>
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      min="0"
                      max="3600"
                      value={form.mqtt_connection_timeout}
                      onChange={(e) => setForm({ ...form, mqtt_connection_timeout: parseInt(e.target.value) || 0 })}
                      className="w-24 px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:ring-1 focus:ring-bambu-green"
                    />
                    <span className="text-xs text-bambu-gray">{t('printers.modal.mqttConnectionTimeoutHint')}</span>
                  </div>
                </div>
                <div>
                  <label className="block text-sm font-medium text-bambu-gray mb-1">{t('printers.modal.staggerInterval')}</label>
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      min="0"
                      max="60"
                      value={form.stagger_interval_minutes ?? 0}
                      onChange={(e) => setForm({ ...form, stagger_interval_minutes: parseInt(e.target.value) || 0 })}
                      className="w-24 px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:ring-1 focus:ring-bambu-green"
                    />
                    <span className="text-xs text-bambu-gray">{t('printers.modal.staggerIntervalHint')}</span>
                  </div>
                </div>
                {(() => {
                  const modelProfiles = (swapProfiles ?? []).filter((p) =>
                    form.model ? p.models.includes(form.model) : false
                  );
                  if (modelProfiles.length === 0) return null;
                  return (
                    <>
                      <div>
                        <label className="flex items-center gap-2 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={form.swap_mode_enabled ?? false}
                            onChange={(e) => {
                              const enabled = e.target.checked;
                              setForm({
                                ...form,
                                swap_mode_enabled: enabled,
                                swap_profile: enabled
                                  ? (form.swap_profile ?? modelProfiles[0]?.id ?? null)
                                  : null,
                                ...(enabled ? { require_plate_clear: false } : {}),
                              });
                            }}
                            className="w-4 h-4 rounded border-bambu-dark-tertiary bg-bambu-dark text-bambu-green focus:ring-bambu-green"
                          />
                          <span className="text-sm text-white">{t('printers.modal.swapMode')}</span>
                        </label>
                        <p className="text-xs text-bambu-gray mt-1 ml-6">{t('printers.modal.swapModeHint')}</p>
                      </div>
                      {form.swap_mode_enabled && (
                        <div className="ml-6">
                          <label className="block text-xs text-bambu-gray mb-1">
                            {t('printers.modal.swapProfile')}
                          </label>
                          <select
                            value={form.swap_profile ?? ''}
                            onChange={(e) => setForm({ ...form, swap_profile: e.target.value || null })}
                            className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none text-sm"
                          >
                            {modelProfiles.map((p) => (
                              <option key={p.id} value={p.id}>{p.label}</option>
                            ))}
                          </select>
                          {form.swap_profile && (
                            <p className="text-xs text-bambu-gray mt-1">
                              {modelProfiles.find((p) => p.id === form.swap_profile)?.description}
                            </p>
                          )}
                        </div>
                      )}
                    </>
                  );
                })()}
                {!form.swap_mode_enabled && (
                <div>
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={form.require_plate_clear ?? true}
                      onChange={(e) => setForm({ ...form, require_plate_clear: e.target.checked })}
                      className="w-4 h-4 rounded border-bambu-dark-tertiary bg-bambu-dark text-bambu-green focus:ring-bambu-green"
                    />
                    <span className="text-sm text-white">{t('printers.modal.requirePlateClear')}</span>
                  </label>
                  <p className="text-xs text-bambu-gray mt-1 ml-6">{t('printers.modal.requirePlateClearHint')}</p>
                </div>
                )}
              </div>
            </div>

            {/* Buttons - full width */}
            <div className="flex gap-3 pt-4">
              <Button type="button" variant="secondary" onClick={onClose} className="flex-1">
                {t('common.cancel')}
              </Button>
              <Button type="submit" className="flex-1" disabled={updateMutation.isPending}>
                {updateMutation.isPending ? t('common.saving') : t('printers.modal.saveChanges')}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}

// Component to check if a printer is offline (for power dropdown)
function usePrinterOfflineStatus(printerId: number) {
  const { data: status } = useQuery({
    queryKey: ['printerStatus', printerId],
    queryFn: () => api.getPrinterStatus(printerId),
    refetchInterval: 30000,
  });
  return !status?.connected;
}

// Power dropdown item for an offline printer
function PowerDropdownItem({
  printer,
  plug,
  onPowerOn,
  isPowering,
}: {
  printer: Printer;
  plug: { id: number; name: string };
  onPowerOn: (plugId: number) => void;
  isPowering: boolean;
}) {
  const isOffline = usePrinterOfflineStatus(printer.id);

  // Fetch plug status
  const { data: plugStatus } = useQuery({
    queryKey: ['smartPlugStatus', plug.id],
    queryFn: () => api.getSmartPlugStatus(plug.id),
    refetchInterval: 10000,
  });

  // Only show if printer is offline
  if (!isOffline) {
    return null;
  }

  return (
    <div className="flex items-center justify-between px-3 py-2 hover:bg-gray-100 dark:hover:bg-bambu-dark-tertiary">
      <div className="flex items-center gap-2 min-w-0">
        <span className="text-sm text-gray-900 dark:text-white truncate">{printer.name}</span>
        {plugStatus && (
          <span
            className={`text-xs px-1.5 py-0.5 rounded ${
              plugStatus.state === 'ON'
                ? 'bg-bambu-green/20 text-bambu-green'
                : 'bg-red-500/20 text-red-400'
            }`}
          >
            {plugStatus.state || '?'}
          </span>
        )}
      </div>
      <button
        onClick={() => onPowerOn(plug.id)}
        disabled={isPowering || plugStatus?.state === 'ON'}
        className={`px-2 py-1 text-xs rounded transition-colors flex items-center gap-1 ${
          plugStatus?.state === 'ON'
            ? 'bg-bambu-green/20 text-bambu-green cursor-default'
            : 'bg-bambu-green/20 text-bambu-green hover:bg-bambu-green hover:text-white'
        }`}
      >
        <Power className="w-3 h-3" />
        {isPowering ? '...' : 'On'}
      </button>
    </div>
  );
}

export function PrintersPage() {
  const { t } = useTranslation();
  const [showAddModal, setShowAddModal] = useState(false);
  const [hideDisconnected, setHideDisconnected] = useState(() => {
    return localStorage.getItem('hideDisconnectedPrinters') === 'true';
  });
  const [showPowerDropdown, setShowPowerDropdown] = useState(false);
  const [poweringOn, setPoweringOn] = useState<number | null>(null);
  const [sortBy, setSortBy] = useState<SortOption>(() => {
    return (localStorage.getItem('printerSortBy') as SortOption) || 'name';
  });
  const [sortAsc, setSortAsc] = useState<boolean>(() => {
    return localStorage.getItem('printerSortAsc') !== 'false';
  });
  // Card size: 1=small, 2=medium, 3=large, 4=xl
  const [cardSize, setCardSize] = useState<number>(() => {
    const saved = localStorage.getItem('printerCardSize');
    return saved ? parseInt(saved, 10) : 2; // Default to medium
  });
  // Derive viewMode from cardSize: S=compact, M/L/XL=expanded
  const viewMode: ViewMode = cardSize === 1 ? 'compact' : 'expanded';
  // Search/filter state (upstream #852)
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [locationFilter, setLocationFilter] = useState<string>('all');
  const [statusCacheVersion, setStatusCacheVersion] = useState(0);
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { hasPermission } = useAuth();

  // Bulk printer selection
  const [selectedPrinterIds, setSelectedPrinterIds] = useState<Set<number>>(new Set());
  // Anchor for Shift-click range selection — the last id the user clicked
  // without a Shift modifier. Standard file-manager pattern: Shift-click
  // selects every card from the anchor through the just-clicked card in
  // the current visual order; plain Ctrl/Cmd-click both selects and updates
  // the anchor. Cleared whenever the selection set is emptied.
  const [lastSelectedId, setLastSelectedId] = useState<number | null>(null);
  const [bulkConfirmAction, setBulkConfirmAction] = useState<'stop' | 'pause' | 'clearPlate' | null>(null);
  const [bulkActionPending, setBulkActionPending] = useState(false);
  const selectionMode = selectedPrinterIds.size > 0;
  // Compact-card "expand into popup" — single printer id, null = closed.
  // The popup re-mounts <PrinterCard> with viewMode='expanded' cardSize=2
  // for the picked printer so all M-card affordances work identically.
  const [expandedPrinterId, setExpandedPrinterId] = useState<number | null>(null);
  // Global Escape handler for the expand popup. Lives at page-scope (not
  // on the popup div) so it fires regardless of which child element has
  // focus when the user presses Escape.
  useEffect(() => {
    if (expandedPrinterId === null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setExpandedPrinterId(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [expandedPrinterId]);

  // Embedded camera viewer state - supports multiple simultaneous viewers
  // Persisted to localStorage so cameras reopen after navigation
  const [embeddedCameraPrinters, setEmbeddedCameraPrinters] = useState<Map<number, { id: number; name: string }>>(() => {
    // Initialize from localStorage if camera_view_mode is embedded
    const saved = localStorage.getItem('openEmbeddedCameras');
    if (saved) {
      try {
        const cameras = JSON.parse(saved) as Array<{ id: number; name: string }>;
        return new Map(cameras.map(c => [c.id, c]));
      } catch {
        return new Map();
      }
    }
    return new Map();
  });

  // Persist open cameras to localStorage when they change
  useEffect(() => {
    const cameras = Array.from(embeddedCameraPrinters.values());
    if (cameras.length > 0) {
      localStorage.setItem('openEmbeddedCameras', JSON.stringify(cameras));
    } else {
      localStorage.removeItem('openEmbeddedCameras');
    }
  }, [embeddedCameraPrinters]);

  const { data: printers, isLoading } = useQuery({
    queryKey: ['printers'],
    queryFn: api.getPrinters,
  });

  // Hash-scroll: links from other pages (queue card / project detail /
  // archives "printing" badge) hit /#printer-<id> — the printers list lives
  // at the index route, so the path stays "/" with a hash anchor pointing
  // at the target card's id={`printer-${id}`}. The card only mounts once
  // `printers` is loaded, so re-run this when the data arrives.
  useEffect(() => {
    if (!printers?.length) return;
    const hash = window.location.hash;
    if (!hash || !hash.startsWith('#printer-')) return;
    // Defer one tick so the freshly-mounted card exists in the DOM.
    const handle = window.setTimeout(() => {
      document.getElementById(hash.slice(1))?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, 50);
    return () => window.clearTimeout(handle);
  }, [printers]);

  // Fetch app settings for AMS thresholds
  const { data: settings } = useQuery({
    queryKey: ['settings'],
    queryFn: api.getSettings,
  });

  // Compute drying presets: user-configured (from settings) merged over built-in defaults
  const effectiveDryingPresets = useMemo(() => {
    if (settings?.drying_presets) {
      try {
        const userPresets = JSON.parse(settings.drying_presets);
        if (typeof userPresets === 'object' && userPresets !== null && Object.keys(userPresets).length > 0) {
          return { ...DRYING_PRESETS, ...userPresets };
        }
      } catch { /* ignore parse errors, use defaults */ }
    }
    return DRYING_PRESETS;
  }, [settings?.drying_presets]);

  // Close embedded cameras if mode changes to 'window'
  useEffect(() => {
    if (settings?.camera_view_mode === 'window' && embeddedCameraPrinters.size > 0) {
      setEmbeddedCameraPrinters(new Map());
    }
  }, [settings?.camera_view_mode, embeddedCameraPrinters.size]);

  // Fetch all smart plugs to know which printers have them
  const { data: smartPlugs } = useQuery({
    queryKey: ['smart-plugs'],
    queryFn: api.getSmartPlugs,
  });

  // Fetch maintenance overview for all printers to show badges
  const { data: maintenanceOverview } = useQuery({
    queryKey: ['maintenanceOverview'],
    queryFn: api.getMaintenanceOverview,
    staleTime: 60 * 1000, // 1 minute
  });

  // Fetch Spoolman status to enable link spool feature
  const { data: spoolmanStatus } = useQuery({
    queryKey: ['spoolman-status'],
    queryFn: api.getSpoolmanStatus,
    staleTime: 60 * 1000, // 1 minute
  });
  const spoolmanEnabled = spoolmanStatus?.enabled && spoolmanStatus?.connected;

  // Fetch Spoolman settings to get sync mode
  const { data: spoolmanSettings } = useQuery({
    queryKey: ['spoolman-settings'],
    queryFn: api.getSpoolmanSettings,
    enabled: !!spoolmanEnabled,
    staleTime: 60 * 1000, // 1 minute
  });
  const spoolmanSyncMode = spoolmanSettings?.spoolman_sync_mode;

  // Fetch unlinked spools to know if link button should be enabled
  const { data: unlinkedSpools } = useQuery({
    queryKey: ['unlinked-spools'],
    queryFn: api.getUnlinkedSpools,
    enabled: !!spoolmanEnabled,
    staleTime: 30 * 1000, // 30 seconds
  });
  const hasUnlinkedSpools = unlinkedSpools && unlinkedSpools.length > 0;

  // Fetch linked spools map (tag -> spool_id) to know which spools are already in Spoolman
  const { data: linkedSpoolsData } = useQuery({
    queryKey: ['linked-spools'],
    queryFn: api.getLinkedSpools,
    enabled: !!spoolmanEnabled,
    staleTime: 30 * 1000, // 30 seconds
  });
  const linkedSpools = linkedSpoolsData?.linked;

  // Fetch spool assignments for inventory feature
  const { data: spoolAssignments } = useQuery({
    queryKey: ['spool-assignments'],
    queryFn: () => api.getAssignments(),
    enabled: hasPermission('inventory:view_assignments'),
    staleTime: 30 * 1000,
  });

  const unassignMutation = useMutation({
    mutationFn: ({ printerId, amsId, trayId }: { printerId: number; amsId: number; trayId: number }) =>
      api.unassignSpool(printerId, amsId, trayId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['spool-assignments'] });
      queryClient.invalidateQueries({ queryKey: ['spoolman-slot-assignments'] });
    },
  });

  // Spoolman inventory feed (upstream PR #1241): the bulk Spoolman spool list +
  // slot-assignment table drive the per-slot fill bar / linked-spool name /
  // Assign/Unassign buttons on the printer card. Gated only on
  // ``spoolmanEnabled`` (matches upstream Bambuddy applied/PrintersPage.tsx).
  // An earlier port iteration introduced an extra ``=== 'inventory'`` check
  // describing an "iframe-mode deployment" — that mode does not exist in
  // BamDude (and didn't exist upstream either), and ``spoolman_sync_mode``
  // only ever takes ``'auto' | 'manual'`` from SpoolmanSettings.tsx, so the
  // gate was permanently closed for every install — the inventory queries
  // never fired and the printer-card Spoolman controls never rendered.
  const { data: spoolmanSpoolsData, isLoading: spoolmanSpoolsLoading } = useQuery({
    queryKey: ['spoolman-inventory-spools'],
    queryFn: () => api.getSpoolmanInventorySpools(false),
    enabled: !!spoolmanEnabled,
    staleTime: 30 * 1000,
  });
  const { data: spoolmanSlotAssignmentsData, isLoading: spoolmanSlotsLoading } = useQuery({
    queryKey: ['spoolman-slot-assignments'],
    queryFn: () => api.getSpoolmanSlotAssignments(),
    enabled: !!spoolmanEnabled,
    staleTime: 30 * 1000,
  });
  const spoolmanLoading = !!spoolmanEnabled && (spoolmanSpoolsLoading || spoolmanSlotsLoading);
  const spoolmanSlotAssignments: SpoolmanSlotAssignmentRow[] | undefined = useMemo(
    () => spoolmanSlotAssignmentsData?.map((row) => ({
      printer_id: row.printer_id,
      ams_id: row.ams_id,
      tray_id: row.tray_id,
      spoolman_spool_id: row.spoolman_spool_id,
    })),
    [spoolmanSlotAssignmentsData],
  );

  const unassignSpoolmanMutation = useMutation({
    mutationFn: (spoolmanSpoolId: number) => api.unassignSpoolmanSlot(spoolmanSpoolId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['spoolman-slot-assignments'] });
      queryClient.invalidateQueries({ queryKey: ['spoolman-inventory-spools'] });
    },
    onError: (error: Error) => {
      showToast(error.message || t('inventory.unassignFailed'), 'error');
    },
  });

  // Helper to find assignment for a specific slot
  const getAssignment = (printerId: number, amsId: number | string, trayId: number | string): SpoolAssignment | undefined => {
    return spoolAssignments?.find(
      (a) => a.printer_id === printerId && a.ams_id === Number(amsId) && a.tray_id === Number(trayId)
    );
  };

  // Create a map of printer_id -> maintenance info for quick lookup
  const maintenanceByPrinter = maintenanceOverview?.reduce(
    (acc, overview) => {
      acc[overview.printer_id] = {
        due_count: overview.due_count,
        warning_count: overview.warning_count,
        total_print_hours: overview.total_print_hours,
      };
      return acc;
    },
    {} as Record<number, PrinterMaintenanceInfo>
  ) || {};

  // Create a map of printer_id -> smart plug. Memoised so the reference is
  // stable across renders when ``smartPlugs`` doesn't change — without this,
  // the fallback ``|| {}`` allocates a fresh object every render and would
  // trigger downstream effects/memos that depend on the map identity.
  const smartPlugByPrinter = useMemo(
    () =>
      smartPlugs?.reduce(
        (acc, plug) => {
          if (plug.printer_id) {
            acc[plug.printer_id] = plug;
          }
          return acc;
        },
        {} as Record<number, typeof smartPlugs[0]>,
      ) || {},
    [smartPlugs],
  );

  const addMutation = useMutation({
    mutationFn: api.createPrinter,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['printers'] });
      queryClient.invalidateQueries({ queryKey: ['maintenanceOverview'] });
      setShowAddModal(false);
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToAdd'), 'error'),
  });

  const powerOnMutation = useMutation({
    mutationFn: (plugId: number) => api.controlSmartPlug(plugId, 'on'),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['smart-plugs'] });
      setPoweringOn(null);
    },
    onError: () => {
      setPoweringOn(null);
    },
  });

  const toggleHideDisconnected = () => {
    const newValue = !hideDisconnected;
    setHideDisconnected(newValue);
    localStorage.setItem('hideDisconnectedPrinters', String(newValue));
  };

  const handleSortChange = (newSort: SortOption) => {
    setSortBy(newSort);
    localStorage.setItem('printerSortBy', newSort);
  };

  const toggleSortDirection = () => {
    const newAsc = !sortAsc;
    setSortAsc(newAsc);
    localStorage.setItem('printerSortAsc', String(newAsc));
  };

  // Grid classes based on card size (1=small, 2=medium, 3=large, 4=xl)
  const getGridClasses = () => {
    switch (cardSize) {
      case 1: return 'grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5'; // S: many small cards
      case 2: return 'grid-cols-1 md:grid-cols-2 xl:grid-cols-3'; // M: medium cards
      case 3: return 'grid-cols-1 lg:grid-cols-2'; // L: large cards, 2 columns max
      case 4: return 'grid-cols-1'; // XL: single column, full width
      default: return 'grid-cols-1 md:grid-cols-2 xl:grid-cols-3';
    }
  };

  const cardSizeLabels = ['S', 'M', 'L', 'XL'];

  // Responsive toolbar state (upstream PR #1203). When the inline expanded
  // controls overflow the available width, collapse them under three
  // overflow menus (Filters / View / Actions). The decision is reactive
  // via ResizeObserver on the toolbar wrapper + a hidden mirror of the
  // expanded control row that we measure off-screen so we always know its
  // natural width regardless of the active layout.
  const toolbarRef = useRef<HTMLDivElement | null>(null);
  const expandedToolbarControlsRef = useRef<HTMLDivElement | null>(null);
  const [compactToolbar, setCompactToolbar] = useState(false);

  const measureToolbar = useCallback(() => {
    const toolbar = toolbarRef.current;
    const expanded = expandedToolbarControlsRef.current;
    if (!toolbar || !expanded) return;
    // Reserve some room for the search input on the same row (min 240 px)
    // before deciding whether the inline controls fit.
    const available = toolbar.clientWidth - 240;
    const needed = expanded.scrollWidth;
    setCompactToolbar(needed > available);
  }, []);

  // Sort printers based on selected option
  // Bulk selection helpers — modifier-aware ``handleSelectPrinter`` is
  // declared further down (after ``sortedPrinters``, which it needs for
  // range-select). The ``selectAll`` / ``selectByLocation`` / ``selectByState``
  // helpers below replace the whole set in one shot, so they don't need
  // modifier handling.

  const clearSelection = useCallback(() => {
    setSelectedPrinterIds(new Set());
    setLastSelectedId(null);
  }, []);

  const selectAll = useCallback(() => {
    if (!printers) return;
    setSelectedPrinterIds(new Set(printers.map(p => p.id)));
  }, [printers]);

  const selectByLocation = useCallback((location: string) => {
    if (!printers) return;
    setSelectedPrinterIds(new Set(printers.filter(p => p.location === location).map(p => p.id)));
  }, [printers]);

  const selectByState = useCallback((state: string) => {
    if (!printers) return;
    const ids = printers.filter(p => {
      const st = queryClient.getQueryData<{ connected: boolean; state: string | null; hms_errors?: HMSError[] }>(['printerStatus', p.id]);
      if (!st?.connected) return state === 'offline';
      const hmsErrors = st.hms_errors ? filterKnownHMSErrors(st.hms_errors) : [];
      switch (state) {
        case 'printing': return st.state === 'RUNNING';
        case 'paused': return st.state === 'PAUSE';
        // FAILED without active HMS is the post-cancel terminal state —
        // group with FINISH. Only known-HMS escalates to "error".
        case 'finished': return st.state === 'FINISH' || (st.state === 'FAILED' && hmsErrors.length === 0);
        case 'error': return hmsErrors.length > 0;
        case 'idle': return st.state === 'IDLE';
        default: return false;
      }
    }).map(p => p.id);
    setSelectedPrinterIds(new Set(ids));
  }, [printers, queryClient]);

  const executeBulkAction = useCallback(async (action: string) => {
    const ids = Array.from(selectedPrinterIds);
    setBulkActionPending(true);
    setBulkConfirmAction(null);
    let successCount = 0;
    for (const id of ids) {
      try {
        if (action === 'stop') await api.stopPrint(id);
        else if (action === 'pause') await api.pausePrint(id);
        else if (action === 'resume') await api.resumePrint(id);
        else if (action === 'clearPlate') await api.clearPlate(id);
        else if (action === 'clearHMS') await api.clearHMSErrors(id);
        successCount++;
      } catch { /* skip failed */ }
    }
    setBulkActionPending(false);
    showToast(t('printers.bulk.actionComplete', { count: successCount }), 'success');
    clearSelection();
  }, [selectedPrinterIds, showToast, t, clearSelection]);

  const handleBulkAction = useCallback((action: string) => {
    if (action === 'stop' || action === 'pause' || action === 'clearPlate') {
      setBulkConfirmAction(action as 'stop' | 'pause' | 'clearPlate');
    } else {
      executeBulkAction(action);
    }
  }, [executeBulkAction]);

  // Increment version counter whenever a printerStatus cache entry is updated so
  // filteredPrinters re-computes reactively on WebSocket-driven status changes (#852).
  useEffect(() => {
    const unsubscribe = queryClient.getQueryCache().subscribe((event) => {
      if (
        event.type === 'updated' &&
        Array.isArray(event.query.queryKey) &&
        event.query.queryKey[0] === 'printerStatus'
      ) {
        setStatusCacheVersion(v => v + 1);
      }
    });
    return unsubscribe;
  }, [queryClient]);

  // Filter printers by search term, status, and location (#852).
  const filteredPrinters = useMemo(() => {
    if (!printers) return [];
    let result = printers;

    if (search.trim()) {
      const q = search.trim().toLowerCase();
      result = result.filter(p =>
        p.name.toLowerCase().includes(q) ||
        (p.model || '').toLowerCase().includes(q) ||
        (p.location || '').toLowerCase().includes(q) ||
        (p.serial_number || '').toLowerCase().includes(q)
      );
    }

    if (locationFilter !== 'all') {
      result = result.filter(p => (p.location || '') === locationFilter);
    }

    if (statusFilter !== 'all') {
      result = result.filter(p => {
        const status = queryClient.getQueryData<{ connected: boolean; state: string | null; hms_errors?: HMSError[] }>(['printerStatus', p.id]);
        if (!status?.connected) return statusFilter === 'offline';
        const hmsErrors = status.hms_errors ? filterKnownHMSErrors(status.hms_errors) : [];
        switch (statusFilter) {
          case 'printing': return status.state === 'RUNNING';
          case 'paused':   return status.state === 'PAUSE';
          // FAILED without active HMS is the post-cancel terminal state —
          // group with FINISH. Only known-HMS escalates to "error".
          case 'finished': return status.state === 'FINISH' || (status.state === 'FAILED' && hmsErrors.length === 0);
          case 'error':    return hmsErrors.length > 0;
          case 'idle':     return status.state !== 'RUNNING' && status.state !== 'PAUSE' && status.state !== 'FINISH' && status.state !== 'FAILED' && hmsErrors.length === 0;
          case 'offline':  return false; // Connected printers are never offline
          default:         return true;
        }
      });
    }

    return result;
  // eslint-disable-next-line react-hooks/exhaustive-deps -- statusCacheVersion is intentional: it forces recompute when WebSocket updates printer status cache
  }, [printers, search, statusFilter, locationFilter, queryClient, statusCacheVersion]);

  // Modifier-aware single-printer selection. Behaves like a file-manager:
  //
  //   Plain click            — no-op (selection is opt-in; plain click on
  //                            the card body must not interfere with the
  //                            buttons / hover cards inside).
  //   Ctrl/Cmd-click         — toggle this printer; update the range anchor.
  //   Shift-click            — select every printer from the anchor through
  //                            the just-clicked one, in the current sorted /
  //                            filtered order. Anchor stays put so a chain
  //                            of Shift-clicks reflows the same range.
  //   Plain checkbox click   — toggle this printer (fast path for one-off
  //                            picks; equivalent to Ctrl-click).
  //
  // Derive unique locations for the location filter dropdown
  const availableLocations = useMemo(() => {
    if (!printers) return [];
    return [...new Set(printers.map(p => p.location || '').filter(Boolean))].sort();
  }, [printers]);

  const sortedPrinters = useMemo(() => {
    const sorted = [...filteredPrinters];

    switch (sortBy) {
      case 'name':
        sorted.sort((a, b) => a.name.localeCompare(b.name));
        break;
      case 'model':
        sorted.sort((a, b) => (a.model || '').localeCompare(b.model || ''));
        break;
      case 'location':
        // Sort by location, with ungrouped printers last
        sorted.sort((a, b) => {
          const locA = a.location || '';
          const locB = b.location || '';
          if (!locA && locB) return 1;
          if (locA && !locB) return -1;
          return locA.localeCompare(locB) || a.name.localeCompare(b.name);
        });
        break;
      case 'status':
        // Sort by status: HMS errors > printing > idle > offline
        sorted.sort((a, b) => {
          const statusA = queryClient.getQueryData<{ connected: boolean; state: string | null; hms_errors?: HMSError[] }>(['printerStatus', a.id]);
          const statusB = queryClient.getQueryData<{ connected: boolean; state: string | null; hms_errors?: HMSError[] }>(['printerStatus', b.id]);

          const getPriority = (s: typeof statusA) => {
            if (!s?.connected) return 3; // offline
            const hmsErrors = s.hms_errors ? filterKnownHMSErrors(s.hms_errors) : [];
            if (hmsErrors.length > 0) return 0; // HMS errors - top priority
            if (s.state === 'RUNNING') return 1; // printing
            return 2; // idle
          };

          return getPriority(statusA) - getPriority(statusB);
        });
        break;
    }

    // Apply ascending/descending
    if (!sortAsc) {
      sorted.reverse();
    }

    return sorted;
  }, [filteredPrinters, sortBy, sortAsc, queryClient]);

  // Modifier-aware single-printer selection. Behaves like a file-manager:
  //
  //   Plain click            — no-op (selection is opt-in; plain click on
  //                            the card body must not interfere with the
  //                            buttons / hover cards inside).
  //   Ctrl/Cmd-click         — toggle this printer; update the range anchor.
  //   Shift-click            — select every printer from the anchor through
  //                            the just-clicked one, in the current sorted /
  //                            filtered order. Anchor stays put so a chain
  //                            of Shift-clicks reflows the same range.
  //   Plain checkbox click   — toggle this printer (fast path for one-off
  //                            picks; equivalent to Ctrl-click).
  //
  // Defined HERE — after both ``filteredPrinters`` and ``sortedPrinters``
  // — because Shift-range needs the current visible order. Hoisting up
  // would trip a temporal-dead-zone (`Cannot access 'sortedPrinters'
  // before initialization`).
  const handleSelectPrinter = useCallback(
    (id: number, e: React.MouseEvent) => {
      const ids = sortedPrinters.map((p) => p.id);
      setSelectedPrinterIds((prev) => {
        const next = new Set(prev);
        if (e.shiftKey && lastSelectedId !== null && lastSelectedId !== id) {
          const fromIdx = ids.indexOf(lastSelectedId);
          const toIdx = ids.indexOf(id);
          if (fromIdx >= 0 && toIdx >= 0) {
            const [start, end] = fromIdx <= toIdx ? [fromIdx, toIdx] : [toIdx, fromIdx];
            for (let i = start; i <= end; i++) next.add(ids[i]);
            return next;
          }
        }
        if (next.has(id)) next.delete(id);
        else next.add(id);
        return next;
      });
      // Anchor advances on every non-Shift action so the next Shift-click
      // ranges from the just-clicked id, not from some forgotten earlier pick.
      if (!e.shiftKey) setLastSelectedId(id);
    },
    [lastSelectedId, sortedPrinters],
  );

  // Group printers by location when sorted by location
  const groupedPrinters = useMemo(() => {
    if (sortBy !== 'location') return null;

    const groups: Record<string, typeof sortedPrinters> = {};
    sortedPrinters.forEach(printer => {
      const location = printer.location || 'Ungrouped';
      if (!groups[location]) groups[location] = [];
      groups[location].push(printer);
    });
    return groups;
  }, [sortBy, sortedPrinters]);

  // ResizeObserver for the responsive toolbar: re-measure on layout changes
  // (window resize, printer list grows/shrinks, smart-plug power dropdown
  // appears/disappears, an extra location filter shows up).
  useLayoutEffect(() => {
    measureToolbar();
  });

  // ESLint react-hooks/exhaustive-deps cannot statically check expressions in
  // the deps array, so we hoist the smart-plug count into a memo before the
  // useEffect — same trigger semantics, no warning.
  const smartPlugCount = useMemo(() => Object.keys(smartPlugByPrinter).length, [smartPlugByPrinter]);
  useEffect(() => {
    const toolbar = toolbarRef.current;
    if (!toolbar) return;

    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', measureToolbar);
      return () => window.removeEventListener('resize', measureToolbar);
    }

    const resizeObserver = new ResizeObserver(() => measureToolbar());
    resizeObserver.observe(toolbar);
    window.addEventListener('resize', measureToolbar);

    return () => {
      resizeObserver.disconnect();
      window.removeEventListener('resize', measureToolbar);
    };
  }, [
    measureToolbar,
    printers?.length,
    availableLocations.length,
    hideDisconnected,
    smartPlugCount,
  ]);

  // Toolbar control sections — render the same control set inline on wide
  // viewports and grouped under 3 overflow menus on narrow viewports. The
  // ``inMenu`` flag flips per-control styling so dropdowns and buttons go
  // full-width inside the menu but stay compact in the inline ribbon.
  const renderFilterControls = (inMenu = false) => (
    <>
      {/* Status filter */}
      {printers && printers.length > 0 && (
        <ToolbarDropdown
          value={statusFilter}
          onChange={setStatusFilter}
          fullWidth={inMenu}
          options={[
            { value: 'all', label: t('printers.filter.allStatuses') },
            { value: 'printing', label: t('printers.status.printing') },
            { value: 'paused', label: t('printers.status.paused') },
            { value: 'idle', label: t('printers.status.idle') },
            { value: 'finished', label: t('printers.status.finished') },
            { value: 'error', label: t('printers.status.error') },
            { value: 'offline', label: t('printers.status.offline') },
          ]}
        />
      )}

      {/* Location filter — only shown when at least one printer has a location */}
      {printers && printers.length > 0 && availableLocations.length > 0 && (
        <ToolbarDropdown
          value={locationFilter}
          onChange={setLocationFilter}
          fullWidth={inMenu}
          options={[
            { value: 'all', label: t('printers.filter.allLocations') },
            ...availableLocations.map((loc) => ({ value: loc, label: loc })),
          ]}
        />
      )}

      <button
        type="button"
        onClick={toggleHideDisconnected}
        aria-pressed={hideDisconnected}
        className={`h-8 px-2 rounded-lg border text-sm font-medium transition-colors ${inMenu ? 'w-full' : ''} ${
          hideDisconnected
            ? 'bg-bambu-green border-bambu-green text-white'
            : 'bg-bambu-dark border-bambu-dark-tertiary text-white hover:bg-bambu-dark-tertiary'
        }`}
      >
        {t('printers.hideOffline')}
      </button>
    </>
  );

  const renderViewControls = (inMenu = false) => (
    <>
      {/* Sort dropdown + direction */}
      <div className={`flex items-center gap-1 ${inMenu ? 'w-full' : ''}`}>
        <ToolbarDropdown<SortOption>
          value={sortBy}
          onChange={handleSortChange}
          fullWidth={inMenu}
          options={[
            { value: 'name', label: t('printers.sort.name') },
            { value: 'status', label: t('printers.sort.status') },
            { value: 'model', label: t('printers.sort.model') },
            { value: 'location', label: t('printers.sort.location') },
          ]}
        />
        <button
          type="button"
          onClick={toggleSortDirection}
          className="h-8 shrink-0 px-2 rounded-lg border bg-bambu-dark border-bambu-dark-tertiary text-white hover:bg-bambu-dark-tertiary transition-colors flex items-center justify-center"
          title={sortAsc ? t('printers.sort.descending') : t('printers.sort.ascending')}
        >
          {sortAsc ? (
            <ArrowUpNarrowWide className="w-4 h-4 text-white" />
          ) : (
            <ArrowDownWideNarrow className="w-4 h-4 text-white" />
          )}
        </button>
      </div>

      {/* Card size selector */}
      <div className={`flex h-8 items-center bg-bambu-dark rounded-lg border border-bambu-dark-tertiary ${inMenu ? 'w-full' : ''}`}>
        {cardSizeLabels.map((label, index) => {
          const size = index + 1;
          const isSelected = cardSize === size;
          return (
            <button
              key={label}
              type="button"
              onClick={() => {
                setCardSize(size);
                localStorage.setItem('printerCardSize', String(size));
              }}
              className={`h-full px-2 text-xs font-medium transition-colors ${inMenu ? 'flex-1' : ''} ${
                index === 0 ? 'rounded-l-lg' : ''
              } ${
                index === cardSizeLabels.length - 1 ? 'rounded-r-lg' : ''
              } ${
                isSelected
                  ? 'bg-bambu-green text-white'
                  : 'text-white hover:bg-bambu-dark-tertiary'
              }`}
              title={
                label === 'S'
                  ? t('printers.cardSize.small')
                  : label === 'M'
                    ? t('printers.cardSize.medium')
                    : label === 'L'
                      ? t('printers.cardSize.large')
                      : t('printers.cardSize.extraLarge')
              }
            >
              {label}
            </button>
          );
        })}
      </div>
    </>
  );

  const renderActionControls = (inMenu = false) => (
    <>
      {/* Bulk select — BamDude 2-stage flow: "Select all" → "N selected" + clearSelection.
          Only shown when more than one printer exists (single-printer farms have no use for bulk). */}
      {printers && printers.length > 1 && (
        selectionMode ? (
          <Button
            variant="outline"
            size="sm"
            onClick={clearSelection}
            className={`!h-8 !min-h-8 !bg-bambu-green/20 !border-bambu-green/50 !text-bambu-green ${inMenu ? 'w-full' : ''}`}
          >
            {t('printers.bulk.selected', { count: selectedPrinterIds.size })}
          </Button>
        ) : (
          <Button
            variant="outline"
            size="sm"
            onClick={() => { if (printers) setSelectedPrinterIds(new Set(printers.map((p) => p.id))); }}
            disabled={!hasPermission('printers:control')}
            className={`!h-8 !min-h-8 ${inMenu ? 'w-full' : ''}`}
          >
            {t('printers.bulk.selectAll')}
          </Button>
        )
      )}

      {/* Power dropdown — only shown when offline printers with smart plugs are filtered out */}
      {hideDisconnected && Object.keys(smartPlugByPrinter).length > 0 && (
        <div className={`relative ${inMenu ? 'w-full' : ''}`}>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setShowPowerDropdown(!showPowerDropdown)}
            className={`!h-8 !min-h-8 ${inMenu ? 'w-full justify-between' : ''}`}
          >
            <Power className="w-4 h-4" />
            {t('printers.powerOn')}
            <ChevronDown className={`w-3 h-3 transition-transform ${showPowerDropdown ? 'rotate-180' : ''}`} />
          </Button>
          {showPowerDropdown && (
            <>
              <div className="fixed inset-0 z-10" onClick={() => setShowPowerDropdown(false)} />
              <div className="absolute right-0 mt-2 w-56 bg-white dark:bg-bambu-dark-secondary border border-gray-200 dark:border-bambu-dark-tertiary rounded-lg shadow-lg z-20 py-1">
                <div className="px-3 py-2 text-xs text-gray-500 dark:text-bambu-gray border-b border-gray-200 dark:border-bambu-dark-tertiary">
                  {t('printers.offlinePrintersWithPlugs')}
                </div>
                {printers?.filter((p) => smartPlugByPrinter[p.id]).map((printer) => (
                  <PowerDropdownItem
                    key={printer.id}
                    printer={printer}
                    plug={smartPlugByPrinter[printer.id]}
                    onPowerOn={(plugId) => {
                      setPoweringOn(plugId);
                      powerOnMutation.mutate(plugId);
                    }}
                    isPowering={poweringOn === smartPlugByPrinter[printer.id]?.id}
                  />
                ))}
                {printers?.filter((p) => smartPlugByPrinter[p.id]).length === 0 && (
                  <div className="px-3 py-2 text-sm text-bambu-gray">
                    No printers with smart plugs
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      )}

      <Button
        onClick={() => setShowAddModal(true)}
        disabled={!hasPermission('printers:create')}
        title={!hasPermission('printers:create') ? t('printers.permission.noAdd') : undefined}
        className={`!h-8 !min-h-8 px-2 py-0 ${inMenu ? 'w-full' : ''}`}
      >
        <Plus className="w-4 h-4" />
        {t('printers.addPrinter')}
      </Button>
    </>
  );

  return (
    <div className="p-4 md:p-6">
      {/* Header section: title with PrinterIcon + StatusSummaryBar (upstream PR #1203). */}
      <div className="space-y-3 mb-6">
        <div>
          <h1 className="text-2xl font-bold text-white flex items-center gap-3">
            <PrinterIcon className="w-7 h-7 text-bambu-green" />
            {t('printers.title')}
          </h1>
          <StatusSummaryBar printers={printers} />
        </div>

        {/* Responsive toolbar: search input + filter/view/action controls
            inline on wide viewports, grouped under 3 overflow menus on narrow.
            ResizeObserver decides which layout fits at any given width. */}
        <div ref={toolbarRef} className="relative flex items-center gap-2">
          {/* Search bar (always inline; takes remaining space) */}
          {printers && printers.length > 0 && (
            <div className="relative min-w-0 flex-1">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-bambu-gray/50" />
              <input
                type="search"
                name="printer-search"
                autoComplete="off"
                data-1p-ignore
                data-lpignore="true"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder={t('printers.search')}
                aria-label={t('printers.search')}
                className="w-full h-8 pl-9 pr-8 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm placeholder:text-bambu-gray/50 focus:outline-none focus:border-bambu-green"
              />
              {search && (
                <button
                  type="button"
                  aria-label={t('common.clear')}
                  onClick={() => setSearch('')}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-bambu-gray hover:text-white"
                >
                  <X className="w-4 h-4" />
                </button>
              )}
            </div>
          )}

          {/* Expanded inline controls (visible when wide enough) — also kept
              mounted off-screen when ``compactToolbar`` is true so we can
              measure its scrollWidth on every layout pass without race
              conditions. ``inert`` keeps it out of the focus/AT tree. */}
          <div
            ref={expandedToolbarControlsRef}
            aria-hidden={compactToolbar}
            inert={compactToolbar}
            className={`${compactToolbar ? 'absolute -left-[9999px] top-0 flex w-max pointer-events-none opacity-0' : 'flex'} ml-auto items-center justify-end gap-2 flex-nowrap [&>*]:shrink-0`}
          >
            <div className="h-6 w-px bg-bambu-dark-tertiary" />
            <div className="flex items-center gap-2">{renderFilterControls()}</div>
            <div className="h-6 w-px bg-bambu-dark-tertiary" />
            <div className="flex items-center gap-2">{renderViewControls()}</div>
            <div className="h-6 w-px bg-bambu-dark-tertiary" />
            <div className="flex items-center gap-2">{renderActionControls()}</div>
          </div>

          {/* Compact overflow menus (visible only when measureToolbar decides
              the inline ribbon overflows). 3 grouped icons: Filters / View /
              Actions, each opening a panel with the same controls re-rendered
              with ``inMenu=true`` so they go full-width inside the panel. */}
          {compactToolbar && (
            <div className="ml-auto flex items-center justify-end gap-1">
              <ToolbarMenu label={t('printers.toolbar.filters')} icon={<Filter className="w-4 h-4" />}>
                <div className="flex w-48 flex-col gap-2">{renderFilterControls(true)}</div>
              </ToolbarMenu>
              <ToolbarMenu label={t('printers.toolbar.view')} icon={<SlidersHorizontal className="w-4 h-4" />}>
                <div className="flex w-48 flex-col gap-2">{renderViewControls(true)}</div>
              </ToolbarMenu>
              <ToolbarMenu label={t('printers.toolbar.actions')} icon={<MoreHorizontal className="w-4 h-4" />}>
                <div className="flex w-48 flex-col gap-2">{renderActionControls(true)}</div>
              </ToolbarMenu>
            </div>
          )}
        </div>
      </div>


      {isLoading ? (
        <div className="text-center py-12 text-bambu-gray">{t('common.loading')}</div>
      ) : printers?.length === 0 ? (
        <Card>
          <CardContent className="text-center py-12">
            <p className="text-bambu-gray mb-4">{t('printers.noPrintersConfigured')}</p>
            <Button
              onClick={() => setShowAddModal(true)}
              disabled={!hasPermission('printers:create')}
              title={!hasPermission('printers:create') ? t('printers.permission.noAdd') : undefined}
            >
              <Plus className="w-4 h-4" />
              {t('printers.addPrinter')}
            </Button>
          </CardContent>
        </Card>
      ) : sortedPrinters.length === 0 && (search.trim() || statusFilter !== 'all' || locationFilter !== 'all') ? (
        <Card>
          <CardContent className="text-center py-12">
            <p className="text-bambu-gray">{t('printers.noSearchResults')}</p>
          </CardContent>
        </Card>
      ) : groupedPrinters ? (
        /* Grouped by location view */
        <div className="space-y-6">
          {Object.entries(groupedPrinters).map(([location, locationPrinters]) => (
            <div key={location}>
              <h2 className="text-lg font-semibold text-white mb-3 flex items-center gap-2">
                <span className="w-2 h-2 rounded-full bg-bambu-green" />
                {location}
                <span className="text-sm font-normal text-bambu-gray">({locationPrinters.length})</span>
              </h2>
              <div className={`grid gap-4 items-start ${cardSize >= 3 ? 'gap-6' : ''} ${getGridClasses()}`}>
                {locationPrinters.map((printer) => (
                  <PrinterCard
                    key={printer.id}
                    printer={printer}
                    hideIfDisconnected={hideDisconnected}
                    maintenanceInfo={maintenanceByPrinter[printer.id]}
                    viewMode={viewMode}
                    cardSize={cardSize}
                    amsThresholds={settings ? {
                      humidityGood: Number(settings.ams_humidity_good) || 40,
                      humidityFair: Number(settings.ams_humidity_fair) || 60,
                      tempGood: Number(settings.ams_temp_good) || 28,
                      tempFair: Number(settings.ams_temp_fair) || 35,
                    } : undefined}
                    spoolmanEnabled={spoolmanEnabled}
                    hasUnlinkedSpools={hasUnlinkedSpools}
                    linkedSpools={linkedSpools}
                    spoolmanUrl={spoolmanStatus?.url}
                    spoolmanSyncMode={spoolmanSyncMode}
                    spoolmanSpools={spoolmanSpoolsData}
                    spoolmanSlotAssignments={spoolmanSlotAssignments}
                    spoolmanLoading={spoolmanLoading}
                    onUnassignSpoolmanSpool={(spoolId) => unassignSpoolmanMutation.mutate(spoolId)}
                    onGetAssignment={getAssignment}
                    onUnassignSpool={(pid, aid, tid) => unassignMutation.mutate({ printerId: pid, amsId: aid, trayId: tid })}
                    timeFormat={settings?.time_format || 'system'}
                    dateFormat={settings?.date_format || 'system'}
                    cameraViewMode={settings?.camera_view_mode || 'window'}
                    onOpenEmbeddedCamera={(id, name) => setEmbeddedCameraPrinters(prev => new Map(prev).set(id, { id, name }))}
                    checkPrinterFirmware={settings?.check_printer_firmware !== false}
                    dryingPresets={effectiveDryingPresets}
                    isSelected={selectedPrinterIds.has(printer.id)}
                    onSelect={handleSelectPrinter}
                    onExpand={(id) => setExpandedPrinterId(id)}
                    spoolDisplayTemplate={settings?.spool_display_template || undefined}
                  />
                ))}
              </div>
            </div>
          ))}
        </div>
      ) : (
        /* Regular grid view */
        <div className={`grid gap-4 items-start ${cardSize >= 3 ? 'gap-6' : ''} ${getGridClasses()}`}>
          {sortedPrinters.map((printer) => (
            <PrinterCard
              key={printer.id}
              printer={printer}
              hideIfDisconnected={hideDisconnected}
              maintenanceInfo={maintenanceByPrinter[printer.id]}
              viewMode={viewMode}
              cardSize={cardSize}
              spoolmanEnabled={spoolmanEnabled}
              hasUnlinkedSpools={hasUnlinkedSpools}
              linkedSpools={linkedSpools}
              spoolmanUrl={spoolmanStatus?.url}
              spoolmanSyncMode={spoolmanSyncMode}
              spoolmanSpools={spoolmanSpoolsData}
              spoolmanSlotAssignments={spoolmanSlotAssignments}
              spoolmanLoading={spoolmanLoading}
              onUnassignSpoolmanSpool={(spoolId) => unassignSpoolmanMutation.mutate(spoolId)}
              onGetAssignment={getAssignment}
              onUnassignSpool={(pid, aid, tid) => unassignMutation.mutate({ printerId: pid, amsId: aid, trayId: tid })}
              amsThresholds={settings ? {
                humidityGood: Number(settings.ams_humidity_good) || 40,
                humidityFair: Number(settings.ams_humidity_fair) || 60,
                tempGood: Number(settings.ams_temp_good) || 28,
                tempFair: Number(settings.ams_temp_fair) || 35,
              } : undefined}
              timeFormat={settings?.time_format || 'system'}
              cameraViewMode={settings?.camera_view_mode || 'window'}
              onOpenEmbeddedCamera={(id, name) => setEmbeddedCameraPrinters(prev => new Map(prev).set(id, { id, name }))}
              checkPrinterFirmware={settings?.check_printer_firmware !== false}
              dryingPresets={effectiveDryingPresets}
              isSelected={selectedPrinterIds.has(printer.id)}
              onSelect={handleSelectPrinter}
              onExpand={(id) => setExpandedPrinterId(id)}
              spoolDisplayTemplate={settings?.spool_display_template || undefined}
            />
          ))}
        </div>
      )}

      {showAddModal && (
        <AddPrinterModal
          onClose={() => setShowAddModal(false)}
          onAdd={(data) => addMutation.mutate(data)}
          existingSerials={printers?.map(p => p.serial_number) || []}
        />
      )}

      {/* Embedded Camera Viewers - multiple viewers can be open simultaneously */}
      {Array.from(embeddedCameraPrinters.values()).map((camera, index) => (
        <EmbeddedCameraViewer
          key={camera.id}
          printerId={camera.id}
          printerName={camera.name}
          viewerIndex={index}
          onClose={() => setEmbeddedCameraPrinters(prev => {
            const next = new Map(prev);
            next.delete(camera.id);
            return next;
          })}
        />
      ))}

      {/* Bulk confirm modals */}
      {bulkConfirmAction === 'stop' && (
        <ConfirmModal
          title={t('printers.bulk.actions.stop')}
          message={t('printers.bulk.selected', { count: selectedPrinterIds.size })}
          confirmText={t('printers.bulk.actions.stop')}
          variant="danger"
          onConfirm={() => executeBulkAction('stop')}
          onCancel={() => setBulkConfirmAction(null)}
        />
      )}
      {bulkConfirmAction === 'pause' && (
        <ConfirmModal
          title={t('printers.bulk.actions.pause')}
          message={t('printers.bulk.selected', { count: selectedPrinterIds.size })}
          onConfirm={() => executeBulkAction('pause')}
          onCancel={() => setBulkConfirmAction(null)}
        />
      )}
      {bulkConfirmAction === 'clearPlate' && (
        <ConfirmModal
          title={t('printers.bulk.actions.clearPlate')}
          message={t('printers.bulk.selected', { count: selectedPrinterIds.size })}
          onConfirm={() => executeBulkAction('clearPlate')}
          onCancel={() => setBulkConfirmAction(null)}
        />
      )}

      {/* Compact-card "expand into popup": re-mount the same PrinterCard
          with M-size sizing for the picked printer. Backdrop click +
          Escape close. The popup card itself does NOT receive ``onExpand``
          (no nested popup) and is forced to ``viewMode='expanded'`` /
          ``cardSize=2`` regardless of the page's current sizing.

          Why a re-mount instead of cloning: the outer page still has the
          live-updating React-Query subscriptions for printer status,
          smart-plug state, AMS data etc. — re-using PrinterCard piggybacks
          on those subscriptions automatically, no extra wiring. */}
      {expandedPrinterId !== null && (() => {
        const expandedPrinter = sortedPrinters.find((p) => p.id === expandedPrinterId);
        if (!expandedPrinter) return null;
        return (
          <div
            // ``items-center`` centers the card in the viewport so the
            // three-dot menu dropdown — which opens BELOW the kebab button
            // — has half the screen of room downward. With the previous
            // ``items-start + my-8`` the card sat at the top, putting the
            // kebab right under viewport top; the dropdown still rendered
            // downward but felt visually clipped because the surrounding
            // chrome (card header) was right at the screen edge with no
            // breathing room. ``overflow-y-auto`` keeps tall cards reachable
            // via scroll on the backdrop itself; the inner div uses ``my-4``
            // so a tall card has visible top/bottom gutters when scrolled.
            className="fixed inset-0 bg-black/60 z-40 flex items-center justify-center p-4 overflow-y-auto"
            onClick={() => setExpandedPrinterId(null)}
            onKeyDown={(e) => { if (e.key === 'Escape') setExpandedPrinterId(null); }}
            role="dialog"
            tabIndex={-1}
          >
            <div
              className="w-full max-w-xl my-4"
              onClick={(e) => e.stopPropagation()}
            >
              <PrinterCard
                printer={expandedPrinter}
                hideIfDisconnected={false}
                maintenanceInfo={maintenanceByPrinter[expandedPrinter.id]}
                viewMode="expanded"
                cardSize={2}
                spoolmanEnabled={spoolmanEnabled}
                hasUnlinkedSpools={hasUnlinkedSpools}
                linkedSpools={linkedSpools}
                spoolmanUrl={spoolmanStatus?.url}
                spoolmanSyncMode={spoolmanSyncMode}
                spoolmanSpools={spoolmanSpoolsData}
                spoolmanSlotAssignments={spoolmanSlotAssignments}
                spoolmanLoading={spoolmanLoading}
                onUnassignSpoolmanSpool={(spoolId) => unassignSpoolmanMutation.mutate(spoolId)}
                onGetAssignment={getAssignment}
                onUnassignSpool={(pid, aid, tid) => unassignMutation.mutate({ printerId: pid, amsId: aid, trayId: tid })}
                amsThresholds={settings ? {
                  humidityGood: Number(settings.ams_humidity_good) || 40,
                  humidityFair: Number(settings.ams_humidity_fair) || 60,
                  tempGood: Number(settings.ams_temp_good) || 28,
                  tempFair: Number(settings.ams_temp_fair) || 35,
                } : undefined}
                timeFormat={settings?.time_format || 'system'}
                dateFormat={settings?.date_format || 'system'}
                cameraViewMode={settings?.camera_view_mode || 'window'}
                onOpenEmbeddedCamera={(id, name) => setEmbeddedCameraPrinters(prev => new Map(prev).set(id, { id, name }))}
                checkPrinterFirmware={settings?.check_printer_firmware !== false}
                dryingPresets={effectiveDryingPresets}
                isSelected={selectedPrinterIds.has(expandedPrinter.id)}
                onSelect={handleSelectPrinter}
                spoolDisplayTemplate={settings?.spool_display_template || undefined}
              />
            </div>
          </div>
        );
      })()}

      {/* Bulk Printer Toolbar */}
      {selectionMode && printers && (
        <BulkPrinterToolbar
          selectedIds={selectedPrinterIds}
          printers={printers}
          onClose={clearSelection}
          onSelectAll={selectAll}
          onSelectByLocation={selectByLocation}
          onSelectByState={selectByState}
          onAction={handleBulkAction}
          actionPending={bulkActionPending}
        />
      )}
    </div>
  );
}
