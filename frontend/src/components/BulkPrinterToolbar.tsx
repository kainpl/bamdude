import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../contexts/AuthContext';
import { X, Square, Pause, Play, ChevronDown, BellOff, Eraser, Home, Wrench, SlidersHorizontal, Lightbulb, Clock, Power, MapPin } from 'lucide-react';
import { Button } from './Button';
import { CardActionMenu } from './CardActionMenu';
import type { Printer, Permission } from '../api/client';
import { byLocationName } from '../utils/locationOrder';

export type BulkAction = 'stop' | 'pause' | 'resume' | 'clearPlate' | 'clearHMS'
  | 'home' | 'calibrate' | 'maintenance' | 'queuePause' | 'queueResume'
  | 'serviceOn' | 'serviceOff' | 'lightOn' | 'lightOff';
type PrinterStateFilter = 'printing' | 'paused' | 'finished' | 'idle' | 'error' | 'offline';

interface PrinterStatus {
  connected: boolean;
  state: string | null;
  hms_errors?: Array<{ attr: number; code: number }>;
  awaiting_plate_clear?: boolean;
}

interface BulkPrinterToolbarProps {
  selectedIds: Set<number>;
  printers: Printer[];
  onClose: () => void;
  onSelectAll: () => void;
  /** `null` selects the printers that stand in no location. */
  onSelectByLocation: (locationId: number | null) => void;
  onSelectByState: (state: PrinterStateFilter) => void;
  onAction: (action: BulkAction) => void;
  /** Opens the location-and-tags dialog for the selection. */
  onPlacement: () => void;
  actionPending: boolean;
}

const STATE_OPTIONS: { key: PrinterStateFilter; dot: string }[] = [
  { key: 'printing', dot: 'bg-bambu-green' },
  { key: 'paused', dot: 'bg-status-warning' },
  { key: 'finished', dot: 'bg-blue-400' },
  { key: 'idle', dot: 'bg-bambu-green' },
  { key: 'error', dot: 'bg-status-error' },
  { key: 'offline', dot: 'bg-gray-400' },
];

export function BulkPrinterToolbar({
  selectedIds,
  printers,
  onClose,
  onSelectAll,
  onSelectByLocation,
  onSelectByState,
  onAction,
  onPlacement,
  actionPending,
}: BulkPrinterToolbarProps) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const queryClient = useQueryClient();
  const [showLocationDropdown, setShowLocationDropdown] = useState(false);
  const [showStateDropdown, setShowStateDropdown] = useState(false);

  const selectedStatuses = Array.from(selectedIds).map(id => ({
    id,
    status: queryClient.getQueryData<PrinterStatus>(['printerStatus', id]),
  }));

  const anyRunning = selectedStatuses.some(({ status }) => status?.connected && status.state === 'RUNNING');
  const anyPaused = selectedStatuses.some(({ status }) => status?.connected && status.state === 'PAUSE');
  const anyStoppable = anyRunning || anyPaused;
  const anyNeedsClearPlate = selectedStatuses.some(
    ({ status }) => status?.connected && (status.state === 'FINISH' || status.state === 'FAILED') && !!status.awaiting_plate_clear,
  );
  const anyWithHMS = selectedStatuses.some(({ status }) =>
    status?.connected && status.hms_errors && status.hms_errors.length > 0,
  );

  const canControl = hasPermission('printers:control');
  const canClearPlate = hasPermission('printers:clear_plate');

  // Deduped by id, labelled by name — two printers in the same place share a
  // row now instead of being two entries that merely look alike.
  const locations = [...new Map(printers.filter(p => p.location).map(p => [p.location!.id, p.location!])).values()]
    .sort(byLocationName(loc => loc.name));
  const unplaced = printers.filter(p => !p.location).length;

  const stateCounts: Record<PrinterStateFilter, number> = { printing: 0, paused: 0, finished: 0, idle: 0, error: 0, offline: 0 };
  printers.forEach(p => {
    const status = queryClient.getQueryData<PrinterStatus>(['printerStatus', p.id]);
    if (!status || !status.connected) { stateCounts.offline++; return; }
    const hasActiveHms = !!(status.hms_errors && status.hms_errors.length > 0);
    if (hasActiveHms) stateCounts.error++;
    switch (status.state) {
      case 'RUNNING': stateCounts.printing++; break;
      case 'PAUSE': stateCounts.paused++; break;
      case 'FINISH': stateCounts.finished++; break;
      // FAILED without an active HMS error is the post-cancel terminal state —
      // group with FINISH. When HMS is active the error bucket is already
      // incremented above; don't double-count.
      case 'FAILED': if (!hasActiveHms) stateCounts.finished++; break;
      default: stateCounts.idle++; break;
    }
  });

  const stateLabels: Record<PrinterStateFilter, string> = {
    printing: t('printers.status.printing', 'Printing'),
    paused: t('printers.status.paused', 'Paused'),
    finished: t('printers.status.finished', 'Finished'),
    idle: t('printers.status.available', 'Available'),
    error: t('printers.status.problem', 'Error'),
    offline: t('printers.status.offline', 'Offline'),
  };

  return (
    <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-40 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl px-4 py-3 flex items-center gap-3 flex-wrap">
      <Button variant="secondary" size="sm" onClick={onClose}>
        <X className="w-4 h-4" />
      </Button>

      <div className="w-px h-6 bg-bambu-dark-tertiary" />

      <span className="text-white font-medium text-sm">
        {t('printers.bulk.selected', { count: selectedIds.size })}
      </span>

      <div className="w-px h-6 bg-bambu-dark-tertiary" />

      <Button variant="secondary" size="sm" onClick={onSelectAll}>
        {t('printers.bulk.selectAll')}
      </Button>

      {/* Select by State */}
      <div className="relative">
        <Button
          variant="secondary"
          size="sm"
          onClick={() => { setShowStateDropdown(!showStateDropdown); setShowLocationDropdown(false); }}
        >
          {t('printers.bulk.selectByState')}
          <ChevronDown className={`w-3 h-3 transition-transform ${showStateDropdown ? 'rotate-180' : ''}`} />
        </Button>
        {showStateDropdown && (
          <>
            {/* not-a-modal: menu */}
            <div className="fixed inset-0 z-10" onClick={() => setShowStateDropdown(false)} />
            <div className="absolute bottom-full mb-2 left-0 w-48 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-lg z-20 py-1">
              {STATE_OPTIONS.filter(({ key }) => stateCounts[key] > 0).map(({ key, dot }) => (
                <button
                  key={key}
                  onClick={() => { onSelectByState(key); setShowStateDropdown(false); }}
                  className="w-full text-left px-3 py-2 text-sm text-bambu-gray hover:bg-bambu-dark-tertiary hover:text-white transition-colors flex items-center gap-2"
                >
                  <div className={`w-2 h-2 rounded-full ${dot}`} />
                  {stateLabels[key]}
                  <span className="ml-auto text-bambu-gray text-xs">{stateCounts[key]}</span>
                </button>
              ))}
            </div>
          </>
        )}
      </div>

      {/* Select by Location */}
      {(locations.length > 0 || unplaced > 0) && (
        <div className="relative">
          <Button
            variant="secondary"
            size="sm"
            onClick={() => { setShowLocationDropdown(!showLocationDropdown); setShowStateDropdown(false); }}
          >
            {t('printers.bulk.selectByLocation')}
            <ChevronDown className={`w-3 h-3 transition-transform ${showLocationDropdown ? 'rotate-180' : ''}`} />
          </Button>
          {showLocationDropdown && (
            <>
              {/* not-a-modal: menu */}
              <div className="fixed inset-0 z-10" onClick={() => setShowLocationDropdown(false)} />
              <div className="absolute bottom-full mb-2 left-0 w-48 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-lg z-20 py-1">
                {locations.map(location => (
                  <button
                    key={location.id}
                    onClick={() => { onSelectByLocation(location.id); setShowLocationDropdown(false); }}
                    className="w-full text-left px-3 py-2 text-sm text-bambu-gray hover:bg-bambu-dark-tertiary hover:text-white transition-colors"
                  >
                    {location.name}
                  </button>
                ))}
                {unplaced > 0 && (
                  <button
                    onClick={() => { onSelectByLocation(null); setShowLocationDropdown(false); }}
                    className="w-full text-left px-3 py-2 text-sm text-bambu-gray hover:bg-bambu-dark-tertiary hover:text-white transition-colors flex items-center gap-2"
                  >
                    {t('printers.ungrouped')}
                    <span className="ml-auto text-bambu-gray text-xs">{unplaced}</span>
                  </button>
                )}
              </div>
            </>
          )}
        </div>
      )}

      <div className="w-px h-6 bg-bambu-dark-tertiary" />

      {/* Actions */}
      <CardActionMenu
        label={t('printers.bulk.moreActions')}
        icon={<><span>{t('printers.bulk.moreActions')}</span><ChevronDown className="w-3 h-3" /></>}
        triggerClassName="flex items-center gap-2 rounded-md px-3 py-2 bg-bambu-dark-tertiary text-white text-sm font-medium hover:bg-bambu-dark"
        width={280}
        estimatedHeight={400}
        disabled={actionPending || selectedIds.size === 0}
      >{close => [
        <button key="placement" role="menuitem" type="button"
          disabled={!hasPermission('printers:update')}
          onClick={() => { close(); onPlacement(); }}
          className="flex w-full items-center gap-2 px-3 py-2 text-sm text-bambu-gray hover:bg-bambu-dark-tertiary hover:text-white disabled:opacity-40"
        >
          <MapPin className="w-4 h-4" />{t('printers.bulk.placement.action')}
        </button>,
        ...([
        { action: 'home', icon: Home, permission: 'printers:control' },
        { action: 'calibrate', icon: SlidersHorizontal, permission: 'printers:control' },
        { action: 'maintenance', icon: Wrench, permission: 'maintenance:update', read: 'maintenance:read' },
        { action: 'queuePause', icon: Clock, permission: 'queue:update_all', read: 'queue:read' },
        { action: 'queueResume', icon: Clock, permission: 'queue:update_all', read: 'queue:read' },
        { action: 'serviceOn', icon: Power, permission: 'printers:update' },
        { action: 'serviceOff', icon: Power, permission: 'printers:update' },
        { action: 'lightOn', icon: Lightbulb, permission: 'printers:control' },
        { action: 'lightOff', icon: Lightbulb, permission: 'printers:control' },
      ] satisfies { action: BulkAction; icon: typeof Home; permission: Permission; read?: Permission }[]).map(({ action, icon: Icon, permission, read }) => (
        <button key={action} role="menuitem" type="button"
          disabled={!hasPermission(permission) || (!!read && !hasPermission(read))}
          onClick={() => { close(); onAction(action); }}
          className="flex w-full items-center gap-2 px-3 py-2 text-sm text-bambu-gray hover:bg-bambu-dark-tertiary hover:text-white disabled:opacity-40"
        >
          <Icon className="w-4 h-4" />{t(`printers.bulk.actions.${action}`)}
        </button>
      )),
      ]}</CardActionMenu>
      <Button size="sm" className="bg-red-500 hover:bg-red-600" onClick={() => onAction('stop')}
        disabled={actionPending || !canControl || !anyStoppable}
      >
        <Square className="w-3.5 h-3.5" /> {t('printers.bulk.actions.stop')}
      </Button>
      <Button variant="secondary" size="sm" onClick={() => onAction('pause')}
        disabled={actionPending || !canControl || !anyRunning}
      >
        <Pause className="w-3.5 h-3.5" /> {t('printers.bulk.actions.pause')}
      </Button>
      <Button variant="secondary" size="sm" onClick={() => onAction('resume')}
        disabled={actionPending || !canControl || !anyPaused}
      >
        <Play className="w-3.5 h-3.5" /> {t('printers.bulk.actions.resume')}
      </Button>
      <Button variant="secondary" size="sm" onClick={() => onAction('clearHMS')}
        disabled={actionPending || !canControl || !anyWithHMS}
      >
        <BellOff className="w-3.5 h-3.5" /> {t('printers.bulk.actions.clearHMS')}
      </Button>
      <Button variant="secondary" size="sm" onClick={() => onAction('clearPlate')}
        disabled={actionPending || !canClearPlate || !anyNeedsClearPlate}
      >
        <Eraser className="w-3.5 h-3.5" /> {t('printers.bulk.actions.clearPlate')}
      </Button>
    </div>
  );
}
