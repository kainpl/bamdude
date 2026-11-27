import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Loader2, Package } from 'lucide-react';
import { api } from '../api/client';
import type { AMSTray, InventorySpool } from '../api/client';
import { Button } from './Button';
import { Modal } from './Modal';
import { Select } from './Select';
import { AssignSpoolModal } from './AssignSpoolModal';
import { formatSlotLabel, getAmsLabel, isAmsHtUnit, isBambuLabSpool } from '../utils/amsHelpers';
import { getSwatchStyle } from '../utils/colors';
import { DEFAULT_SPOOL_DISPLAY_TEMPLATE, formatSpoolDisplayName } from '../utils/spoolName';

/**
 * «Assign to slot» from a spool's own form (upstream 632930f02 leg 3): the
 * operator has the spool and picks where it goes — printer → AMS unit → slot.
 *
 * ⚠️ This is a chooser, not an assignment writer. A picked slot opens
 * `AssignSpoolModal` with the spool already decided, and THAT dialog assigns:
 * the same routes, the archived / freshness check, the material-mismatch
 * confirmation and — mid-print — the «replacement or correction?» question.
 * Calling the assign API from here would skip every one of them.
 */

interface AssignToSlotModalProps {
  spool: { id: number; displayName: string; source: 'inventory' | 'spoolman'; labelKey?: string };
  spoolmanMode: boolean;
  onClose: () => void;
  /** After the assignment went through. */
  onAssigned?: () => void;
}

interface Unit {
  /** AMS id as the assignment routes take it; 255 = the external holder(s). */
  amsId: number;
  label: string;
  isHt: boolean;
  external: boolean;
  /** `trayId` is the route's tray id — for an external holder, 0 or 1. */
  slots: { trayId: number; tray: AMSTray }[];
}

type Target = {
  amsId: number;
  trayId: number;
  trayInfo: { type: string; material?: string; profile?: string; color: string; location: string };
  currentSpool?: { id: number; displayName: string; source: 'inventory' | 'spoolman' };
};

export function AssignToSlotModal({ spool, spoolmanMode, onClose, onAssigned }: AssignToSlotModalProps) {
  const { t } = useTranslation();
  const [printerId, setPrinterId] = useState<number | null>(null);
  const [unitId, setUnitId] = useState<number | null>(null);
  const [target, setTarget] = useState<Target | null>(null);

  const { data: printers, isLoading: printersLoading } = useQuery({
    queryKey: ['printers'],
    queryFn: api.getPrinters,
  });
  const { data: status, isLoading: statusLoading } = useQuery({
    queryKey: ['printer-status', printerId],
    queryFn: ({ signal }) => api.getPrinterStatus(printerId!, signal),
    enabled: printerId !== null,
  });
  const { data: settings } = useQuery({ queryKey: ['ui-preferences'], queryFn: () => api.getUiPreferences() });
  // Who sits where now — to name a slot's current spool (replace mode).
  const { data: assignments } = useQuery({
    queryKey: ['spool-assignments'],
    queryFn: () => api.getAssignments(),
    enabled: !spoolmanMode,
  });
  const { data: spoolmanAssignments } = useQuery({
    queryKey: ['spoolman-slot-assignments', 'all'],
    queryFn: () => api.getSpoolmanSlotAssignments(),
    enabled: spoolmanMode,
  });
  const { data: spoolmanSpools } = useQuery({
    queryKey: ['spoolman-inventory-spools'],
    queryFn: () => api.getSpoolmanInventorySpools(false),
    enabled: spoolmanMode,
  });

  const template = settings?.spool_display_template || DEFAULT_SPOOL_DISPLAY_TEMPLATE;

  const units = useMemo<Unit[]>(() => {
    if (!status) return [];
    const list: Unit[] = (status.ams ?? []).map((ams) => {
      const isHt = isAmsHtUnit(ams.id);
      return {
        amsId: ams.id,
        label: getAmsLabel(ams.id, ams.tray.length),
        isHt,
        external: false,
        slots: ams.tray.map((tray, index) => ({ trayId: tray.id ?? index, tray })),
      };
    });
    const external = [...(status.vt_tray ?? [])].sort((a, b) => (a.id ?? 254) - (b.id ?? 254));
    if (external.length > 0) {
      list.push({
        amsId: 255,
        label: getAmsLabel(255, external.length),
        isHt: false,
        external: true,
        // The printer card's addressing: the holder's id 254 / 255 → tray 0 / 1.
        slots: external.map((tray) => ({ trayId: (tray.id ?? 254) - 254, tray })),
      });
    }
    return list;
  }, [status]);

  const unit = units.find((u) => u.amsId === unitId) ?? units[0] ?? null;

  const currentSpoolOf = (amsId: number, trayId: number): Target['currentSpool'] => {
    if (printerId === null) return undefined;
    if (spoolmanMode) {
      const row = spoolmanAssignments?.find(
        (a) => a.printer_id === printerId && a.ams_id === amsId && a.tray_id === trayId,
      );
      if (!row) return undefined;
      const held = spoolmanSpools?.find((s: InventorySpool) => s.id === row.spoolman_spool_id);
      return {
        id: row.spoolman_spool_id,
        displayName: held ? formatSpoolDisplayName(held, template) : `#${row.spoolman_spool_id}`,
        source: 'spoolman',
      };
    }
    const row = assignments?.find((a) => a.printer_id === printerId && a.ams_id === amsId && a.tray_id === trayId);
    if (!row) return undefined;
    return {
      id: row.spool_id,
      displayName: row.spool ? formatSpoolDisplayName(row.spool, template) : `#${row.spool_id}`,
      source: 'inventory',
    };
  };

  const slotLabel = (u: Unit, trayId: number) => formatSlotLabel(u.amsId, trayId, u.isHt, u.external);

  const choose = (u: Unit, trayId: number, tray: AMSTray) => {
    const profile = tray.tray_sub_brands || tray.tray_type || '';
    setTarget({
      amsId: u.amsId,
      trayId,
      trayInfo: {
        type: tray.tray_type || profile,
        material: tray.tray_type ?? undefined,
        profile,
        color: tray.tray_color || '',
        location: u.external ? u.label : t('configureAmsSlot.slotLabel', { ams: u.label, slot: trayId + 1 }),
      },
      currentSpool: currentSpoolOf(u.amsId, trayId),
    });
  };

  return (
    <>
      <Modal
        onClose={onClose}
        title={t('inventory.assignToSlot')}
        icon={<Package className="w-5 h-5 text-bambu-green" />}
        size="lg"
      >
        <div className="p-4 space-y-4 overflow-y-auto">
          <div className="p-3 bg-bambu-dark rounded-lg border border-bambu-dark-tertiary">
            <p className="text-xs text-bambu-gray mb-1">{t('inventory.assignThisSpool')}:</p>
            <div className="flex items-baseline gap-2">
              <span className="text-white font-medium break-words min-w-0">{spool.displayName}</span>
              <span className="text-[10px] font-mono text-bambu-gray shrink-0">#{spool.id}</span>
            </div>
          </div>

          <div>
            <label htmlFor="assign-to-slot-printer" className="block text-sm font-medium text-bambu-gray mb-1">
              {t('common.printer')}
            </label>
            <Select
              id="assign-to-slot-printer"
              value={printerId ?? ''}
              disabled={printersLoading}
              onChange={(e) => {
                setPrinterId(e.target.value ? Number(e.target.value) : null);
                setUnitId(null);
              }}
            >
              <option value="">{t('inventory.assignToSlotPickPrinter')}</option>
              {(printers ?? []).map((printer) => (
                <option key={printer.id} value={printer.id}>{printer.name}</option>
              ))}
            </Select>
          </div>

          {printerId !== null && statusLoading && (
            <div className="flex justify-center py-6">
              <Loader2 className="w-6 h-6 text-bambu-green animate-spin" />
            </div>
          )}

          {printerId !== null && !statusLoading && units.length === 0 && (
            <p className="text-sm text-bambu-gray text-center py-4">{t('inventory.assignToSlotNoSlots')}</p>
          )}

          {printerId !== null && unit && (
            <>
              <div>
                <label htmlFor="assign-to-slot-unit" className="block text-sm font-medium text-bambu-gray mb-1">
                  {t('inventory.assignToSlotUnit')}
                </label>
                <Select
                  id="assign-to-slot-unit"
                  value={unit.amsId}
                  onChange={(e) => setUnitId(Number(e.target.value))}
                >
                  {units.map((u) => <option key={u.amsId} value={u.amsId}>{u.label}</option>)}
                </Select>
              </div>

              <div>
                <p className="text-sm font-medium text-bambu-gray mb-1">{t('inventory.assignToSlotSlot')}</p>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                  {unit.slots.map(({ trayId, tray }) => {
                    const rfid = isBambuLabSpool(tray as { tray_uuid?: string | null; tag_uid?: string | null });
                    const holds = currentSpoolOf(unit.amsId, trayId);
                    const reported = tray.tray_sub_brands || tray.tray_type;
                    return (
                      <button
                        key={trayId}
                        type="button"
                        disabled={rfid}
                        onClick={() => choose(unit, trayId, tray)}
                        className="min-h-[44px] p-2.5 rounded-lg border text-left transition-colors bg-bambu-dark border-bambu-dark-tertiary hover:border-bambu-gray disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:border-bambu-dark-tertiary"
                      >
                        <span className="flex items-center gap-2 text-sm text-white">
                          <span className="font-semibold shrink-0">{slotLabel(unit, trayId)}</span>
                          {tray.tray_color && (
                            <span
                              className="w-3 h-3 rounded-full border border-black/20 shrink-0"
                              style={getSwatchStyle(tray.tray_color)}
                            />
                          )}
                          <span className="truncate min-w-0">{reported || t('ams.emptySlot')}</span>
                        </span>
                        {holds && (
                          <span className="block text-xs text-bambu-gray mt-1 truncate">
                            {t('inventory.currentlyAssigned')}: {holds.displayName}
                          </span>
                        )}
                        {rfid && (
                          <span className="block text-xs text-bambu-gray mt-1">{t('inventory.assignToSlotRfid')}</span>
                        )}
                      </button>
                    );
                  })}
                </div>
              </div>
            </>
          )}
        </div>

        <div className="flex justify-end p-4 border-t border-bambu-dark-tertiary">
          <Button variant="secondary" onClick={onClose}>{t('common.cancel')}</Button>
        </div>
      </Modal>

      {target && printerId !== null && (
        <AssignSpoolModal
          isOpen
          onClose={() => setTarget(null)}
          onAssigned={() => {
            onAssigned?.();
            onClose();
          }}
          printerId={printerId}
          amsId={target.amsId}
          trayId={target.trayId}
          trayInfo={target.trayInfo}
          spoolmanEnabled={spoolmanMode}
          currentSpool={target.currentSpool}
          fixedSpool={spool}
        />
      )}
    </>
  );
}
