import { useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { invalidatePrinterFleet } from '../utils/printerFleetInvalidation';
import { useTranslation } from 'react-i18next';
import { api, type Printer, type PrinterStatus, type MaintenanceStatus, type PrinterQueue, type WaitingPrint, type Permission } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { ConfirmModal } from './ConfirmModal';
import { Modal } from './Modal';
import { Button } from './Button';
import type { BulkAction } from './BulkPrinterToolbar';

const CALIBRATIONS = [
  ['bed_leveling', 'bedLeveling'], ['vibration', 'vibration'], ['motor_noise', 'motorNoise'],
  ['nozzle_offset', 'nozzleOffset'], ['high_temp_heatbed', 'highTempHeatbed'],
  ['lidar', 'lidar'], ['clump_pos', 'clumpPos'],
] as const;
type CalibrationKey = typeof CALIBRATIONS[number][0];
type CalibrationOptions = Awaited<ReturnType<typeof api.getCalibrationOptions>>;
type Preview = {
  printer: Printer;
  status?: PrinterStatus;
  options?: CalibrationOptions;
  maintenance?: MaintenanceStatus[];
  queue?: PrinterQueue;
  waiting?: WaitingPrint;
  error?: string;
};
type Result = { name: string; detail?: string; outcome: 'sent' | 'updated' | 'skipped' | 'failed'; reason?: string };
const BUSY = new Set(['RUNNING', 'PAUSE', 'SLICING', 'PREPARE']);

function bulkActionPermission(action: BulkAction): Permission {
  if (action === 'maintenance') return 'maintenance:update';
  if (action === 'queuePause' || action === 'queueResume') return 'queue:update_all';
  if (action === 'serviceOn' || action === 'serviceOff') return 'printers:update';
  if (action === 'clearPlate') return 'printers:clear_plate';
  return 'printers:control';
}

/** One immutable selection, one explicit confirmation; existing per-printer APIs
 * keep their authorization and motion guards. No retry of uncertain commands. */
export function BulkPrinterActionDialog({ action, printers, onClose }: {
  action: BulkAction;
  printers: Printer[];
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const queryClient = useQueryClient();
  const [calibrations, setCalibrations] = useState<CalibrationKey[]>([]);
  const [maintenanceTypes, setMaintenanceTypes] = useState<number[]>([]);
  const [notes, setNotes] = useState('');
  const [emptyConfirmed, setEmptyConfirmed] = useState(false);
  const [running, setRunning] = useState(false);
  const [results, setResults] = useState<Result[] | null>(null);
  const inFlight = useRef(false);
  const motion = action === 'home' || action === 'calibrate';
  const title = t(`printers.bulk.actions.${action}`);
  const calibrationLabel = (key: CalibrationKey) => t(`printers.calibration.${CALIBRATIONS.find(c => c[0] === key)![1]}`);

  const { data: rows = [], isPending, error: previewError } = useQuery({
    queryKey: ['bulk-printer-preview', action, printers.map(p => p.id)],
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: 0,
    queryFn: async (): Promise<Preview[]> => {
      const queues = action === 'queuePause' || action === 'queueResume' ? await api.getQueues() : [];
      return Promise.all(printers.map(async printer => {
        try {
          if (action === 'maintenance') {
            const overview = await api.getPrinterMaintenance(printer.id);
            return { printer, maintenance: overview.maintenance_items.filter(item => item.enabled) };
          }
          if (action === 'queuePause' || action === 'queueResume') return { printer, queue: queues.find(q => q.printer_id === printer.id) };
          const status = await api.getPrinterStatus(printer.id);
          const options = action === 'calibrate' ? await api.getCalibrationOptions(printer.id) : undefined;
          const waiting = action === 'clearPlate' ? await api.getWaitingPrint(printer.id) : undefined;
          const current = action === 'serviceOn' || action === 'serviceOff' ? await api.getPrinter(printer.id) : printer;
          return { printer: current, status, options, waiting };
        } catch (error) {
          return { printer, error: error instanceof Error ? error.message : t('printers.bulk.failed') };
        }
      }));
    },
  });

  function reason(row: Preview): string | undefined {
    if (row.error) return row.error;
    if (row.printer.archived) return t('printers.bulk.changed');
    if (!hasPermission(bulkActionPermission(action))) return t('printers.bulk.noPermission');
    if (action === 'maintenance') {
      if (!row.maintenance?.some(item => maintenanceTypes.includes(item.maintenance_type_id))) return t('printers.bulk.noTasks');
      return;
    }
    if (action === 'queuePause' || action === 'queueResume') {
      if (!row.queue) return t('printers.bulk.noQueue');
      if (row.queue.is_paused === (action === 'queuePause')) return t('printers.bulk.alreadySet');
      return;
    }
    if (action === 'serviceOff') return row.printer.is_active ? t('printers.bulk.alreadySet') : undefined;
    if (!row.status?.connected) return t('printers.bulk.offline');
    const state = row.status.state;
    if (motion || action === 'serviceOn') {
      if (BUSY.has(state ?? '')) return t('printers.bulk.busy');
      if (!['IDLE', 'FINISH', 'FAILED'].includes(state ?? '')) return t('printers.bulk.unknownState');
      if (motion && row.status.hms_errors?.length) return t('printers.bulk.printerError');
    }
    if (action === 'serviceOn' && !row.printer.is_active) return t('printers.bulk.alreadySet');
    if (action === 'calibrate') {
      if (!calibrations.length) return t('printers.bulk.chooseCalibration');
      const unsupported = calibrations.filter(key => !row.options?.[key]);
      if (unsupported.length) return t('printers.bulk.unsupported', { options: unsupported.map(calibrationLabel).join(', ') });
    }
    if (action === 'stop' && !['RUNNING', 'PAUSE'].includes(state ?? '')) return t('printers.bulk.notPrinting');
    if (action === 'pause' && state !== 'RUNNING') return t('printers.bulk.notPrinting');
    if (action === 'resume' && state !== 'PAUSE') return t('printers.bulk.notPaused');
    if (action === 'clearHMS' && !row.status.hms_errors?.length) return t('printers.bulk.noHms');
    if (action === 'clearPlate' && (!row.status.awaiting_plate_clear || !row.waiting?.archive_id)) return t('printers.bulk.noHeldPlate');
  }
  const targets = rows.filter(row => !reason(row));
  const tasks = [...new Map(rows.flatMap(row => row.maintenance ?? []).map(item => [item.maintenance_type_id, item])).values()];
  const availableCalibrationKeys = CALIBRATIONS.filter(([key]) => rows.some(row => row.options?.[key]));

  async function execute() {
    if (inFlight.current || !targets.length || (motion && !emptyConfirmed)) return;
    inFlight.current = true;
    setRunning(true);
    const output: Result[] = [];
    const confirmed = [...targets];
    for (const row of rows) {
      const skip = reason(row);
      if (skip) output.push({ name: row.printer.name, outcome: 'skipped', reason: skip });
    }
    try {
      for (const row of confirmed) {
        const name = row.printer.name;
        try {
          if (!hasPermission(bulkActionPermission(action))) throw new Error(t('printers.bulk.noPermission'));
          if (action === 'maintenance') {
            const selected = row.maintenance!.filter(item => maintenanceTypes.includes(item.maintenance_type_id));
            const current = (await api.getPrinterMaintenance(row.printer.id)).maintenance_items;
            if (selected.some(item => !current.some(now => now.id === item.id && now.enabled && now.maintenance_type_id === item.maintenance_type_id))) {
              output.push({ name, outcome: 'skipped', reason: t('printers.bulk.changed') });
              continue;
            }
            for (const item of selected) {
              try {
                await api.performMaintenance(item.id, notes.trim() || undefined);
                output.push({ name, detail: item.maintenance_type_name, outcome: 'updated' });
              } catch (error) {
                output.push({ name, detail: item.maintenance_type_name, outcome: 'failed', reason: error instanceof Error ? error.message : t('printers.bulk.failed') });
              }
            }
            continue;
          }
          if (action === 'queuePause' || action === 'queueResume') {
            const queue = (await api.getQueues()).find(q => q.id === row.queue!.id && q.printer_id === row.printer.id);
            if (!queue || queue.is_paused !== row.queue!.is_paused) {
              output.push({ name, outcome: 'skipped', reason: t('printers.bulk.changed') });
              continue;
            }
            await api.updateQueue(queue.id, { is_paused: action === 'queuePause' });
          } else if (action === 'serviceOn' || action === 'serviceOff') {
            const current = await api.getPrinter(row.printer.id);
            const fresh: Preview = { ...row, printer: current, status: await api.getPrinterStatus(current.id) };
            const skip = reason(fresh);
            if (skip) { output.push({ name, outcome: 'skipped', reason: skip }); continue; }
            await api.updatePrinter(current.id, { is_active: action === 'serviceOff' });
          } else {
            const fresh: Preview = { ...row, status: await api.getPrinterStatus(row.printer.id) };
            if (action === 'calibrate') fresh.options = await api.getCalibrationOptions(row.printer.id);
            if (action === 'clearPlate') {
              fresh.waiting = await api.getWaitingPrint(row.printer.id);
              if (fresh.waiting.archive_id !== row.waiting!.archive_id || fresh.waiting.gate_token !== row.waiting!.gate_token) {
                output.push({ name, outcome: 'skipped', reason: t('printers.bulk.changed') });
                continue;
              }
            }
            const skip = reason(fresh);
            if (skip) { output.push({ name, outcome: 'skipped', reason: skip }); continue; }
            let sent: { success: boolean };
            switch (action) {
              case 'home': sent = await api.homeAxes(row.printer.id, 'all'); break;
              case 'calibrate': sent = await api.startCalibration(row.printer.id, Object.fromEntries(calibrations.map(key => [key, true]))); break;
              case 'lightOn': case 'lightOff': sent = await api.setChamberLight(row.printer.id, action === 'lightOn'); break;
              case 'stop': sent = await api.stopPrint(row.printer.id); break;
              case 'pause': sent = await api.pausePrint(row.printer.id); break;
              case 'resume': sent = await api.resumePrint(row.printer.id); break;
              case 'clearHMS': sent = await api.clearHMSErrors(row.printer.id); break;
              case 'clearPlate':
                await api.clearPlate(row.printer.id, { expected_archive_id: row.waiting!.archive_id, expected_gate_token: row.waiting!.gate_token ?? undefined });
                output.push({ name, outcome: 'updated' });
                continue;
            }
            if (!sent.success) throw new Error(t('printers.bulk.commandRefused'));
          }
          output.push({ name, outcome: ['queuePause', 'queueResume', 'serviceOn', 'serviceOff'].includes(action) ? 'updated' : 'sent' });
        } catch (error) {
          output.push({ name, outcome: 'failed', reason: error instanceof Error ? error.message : t('printers.bulk.failed') });
        }
      }
    } finally {
      setResults(output);
      setRunning(false);
      void invalidatePrinterFleet(queryClient);
      void queryClient.invalidateQueries({ queryKey: ['queues'] });
      void queryClient.invalidateQueries({ queryKey: ['maintenanceOverview'] });
      void queryClient.invalidateQueries({ queryKey: ['maintenanceSummary'] });
      void queryClient.invalidateQueries({ queryKey: ['maintenanceHistory'] });
      for (const row of confirmed) void queryClient.invalidateQueries({ queryKey: ['printerStatus', row.printer.id] });
    }
  }

  if (results) return (
    <Modal title={t('printers.bulk.resultsTitle', { action: title })} onClose={onClose} size="lg">
      <div className="p-4 space-y-3">
        <p className="text-sm text-bambu-gray">{t('printers.bulk.sentHelp')}</p>
        <ul className="space-y-2 max-h-80 overflow-y-auto">
          {results.map((result, index) => <li key={index} className="rounded border border-bambu-dark-tertiary p-2 text-sm">
            <span className="font-medium text-white">{result.name}{result.detail ? ` · ${result.detail}` : ''}</span>
            <span className={`block ${result.outcome === 'failed' ? 'text-red-700 dark:text-red-400' : result.outcome === 'skipped' ? 'text-bambu-gray' : 'text-bambu-green'}`}>
              {t(`printers.bulk.${result.outcome}`)}{result.reason ? ` · ${result.reason}` : ''}
            </span>
          </li>)}
        </ul>
        <Button variant="secondary" onClick={onClose}>{t('common.close')}</Button>
      </div>
    </Modal>
  );
  return (
    <ConfirmModal
      title={t('printers.bulk.confirmTitle', { action: title, count: targets.length })}
      message={t(`printers.bulk.help.${action}`)}
      confirmText={t('printers.bulk.confirmAction', { action: title, count: targets.length })}
      variant={action === 'stop' ? 'danger' : motion || action === 'clearPlate' ? 'warning' : 'default'}
      confirmDisabled={isPending || !targets.length || (motion && !emptyConfirmed)}
      isLoading={running}
      onConfirm={() => { void execute(); }}
      onCancel={onClose}
    >
      {isPending && <p role="status" className="text-sm text-bambu-gray">{t('common.loading')}</p>}
      {previewError && <p role="alert" className="text-sm text-red-700 dark:text-red-400">{previewError.message}</p>}
      {action === 'calibrate' && <fieldset disabled={running} className="space-y-2 mb-3">
        <legend className="text-sm font-medium text-white mb-2">{t('printers.bulk.chooseCalibration')}</legend>
        {availableCalibrationKeys.map(([key]) => <label key={key} className="flex gap-2 text-sm text-white">
          <input type="checkbox" checked={calibrations.includes(key)} onChange={e => setCalibrations(old => e.target.checked ? [...old, key] : old.filter(k => k !== key))} />
          {calibrationLabel(key)}
        </label>)}
      </fieldset>}
      {action === 'maintenance' && <fieldset disabled={running} className="space-y-2 mb-3">
        <legend className="text-sm font-medium text-white mb-2">{t('printers.bulk.chooseTasks')}</legend>
        {tasks.map(item => <label key={item.maintenance_type_id} className="flex gap-2 text-sm text-white">
          <input type="checkbox" checked={maintenanceTypes.includes(item.maintenance_type_id)} onChange={e => setMaintenanceTypes(old => e.target.checked ? [...old, item.maintenance_type_id] : old.filter(id => id !== item.maintenance_type_id))} />
          {item.maintenance_type_name}
        </label>)}
        <label className="block text-sm text-bambu-gray">{t('printers.bulk.notes')}
          <textarea value={notes} onChange={e => setNotes(e.target.value)} maxLength={2000} className="mt-1 block w-full rounded border border-bambu-dark-tertiary bg-bambu-dark p-2 text-white" />
        </label>
      </fieldset>}
      <p className="text-sm text-bambu-gray mb-2">{t('printers.bulk.previewCount', { selected: printers.length, ready: targets.length, skipped: rows.length - targets.length })}</p>
      <ul className="max-h-52 overflow-y-auto space-y-2">
        {rows.map(row => <li key={row.printer.id} className="text-sm border-b border-bambu-dark-tertiary pb-2">
          <span className="font-medium text-white">{row.printer.name}</span>
          <span className="block text-bambu-gray">{reason(row) ?? (action === 'maintenance'
            ? row.maintenance!.filter(item => maintenanceTypes.includes(item.maintenance_type_id)).map(item => item.maintenance_type_name).join(', ')
            : action === 'calibrate' ? calibrations.map(calibrationLabel).join(', ') : t('printers.bulk.ready'))}</span>
        </li>)}
      </ul>
      {motion && <label className="flex items-start gap-2 mt-3 text-sm text-white">
        <input type="checkbox" disabled={running} checked={emptyConfirmed} onChange={e => setEmptyConfirmed(e.target.checked)} />
        {t('printers.bulk.confirmEmpty')}
      </label>}
    </ConfirmModal>
  );
}
