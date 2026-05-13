import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { X, RefreshCw, Trash2, CheckCircle2 } from 'lucide-react';

import { useCalibrationHistory } from '../hooks/useCalibrationHistory';
import type { FilamentCalibrationOut, PACalibHistoryEntryOut } from '../api/client';

interface Props {
  isOpen: boolean;
  onClose: () => void;
  printerId: number;
}

function groupByNozzle<T extends { nozzle_diameter: number; nozzle_volume_type: string }>(
  rows: T[],
) {
  const groups = new Map<string, T[]>();
  for (const r of rows) {
    const key = `${r.nozzle_diameter}-${r.nozzle_volume_type}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(r);
  }
  return groups;
}

export function CalibrationHistoryModal({ isOpen, onClose, printerId }: Props) {
  const { t } = useTranslation();
  const h = useCalibrationHistory(printerId, isOpen);
  const [refreshDia, setRefreshDia] = useState<number>(0.4);

  if (!isOpen) return null;

  const bamdudeGroups = groupByNozzle(h.bamdude);
  const printerGroups = groupByNozzle(h.printerSide);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-2xl w-full max-w-3xl mx-4 max-h-[90vh] overflow-y-auto">
        <div className="flex justify-between items-center p-4 border-b border-bambu-dark-tertiary">
          <h2 className="text-lg font-semibold text-white">{t('filamentCali.history.title')}</h2>
          <button
            onClick={onClose}
            aria-label="Close"
            className="p-1 text-bambu-gray hover:text-white rounded transition-colors"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="p-4 space-y-6">
          <section>
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-sm font-semibold text-white">
                {t('filamentCali.history.bamdudeSide')}
              </h3>
            </div>
            {bamdudeGroups.size === 0 && (
              <p className="text-sm text-bambu-gray">{t('filamentCali.history.empty')}</p>
            )}
            {Array.from(bamdudeGroups.entries()).map(([key, rows]) => {
              const [dia, type] = key.split('-');
              return (
                <div key={key} className="mb-4">
                  <h4 className="text-xs text-bambu-gray mb-2">
                    {t('filamentCali.history.groupNozzle', { diameter: dia, type })}
                  </h4>
                  <div className="space-y-1">
                    {rows.map((r) => (
                      <BamDudeRow key={r.id} r={r} h={h} />
                    ))}
                  </div>
                </div>
              );
            })}
          </section>

          <section>
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-sm font-semibold text-white">
                {t('filamentCali.history.printerSide')}
              </h3>
              <div className="flex items-center gap-2">
                <select
                  value={refreshDia}
                  onChange={(e) => setRefreshDia(parseFloat(e.target.value))}
                  className="bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1 text-xs text-white"
                >
                  {[0.2, 0.4, 0.6, 0.8].map((d) => (
                    <option key={d} value={d}>
                      {d} mm
                    </option>
                  ))}
                </select>
                <button
                  onClick={() => h.refreshFromPrinter(refreshDia)}
                  disabled={h.isRefreshing}
                  className="px-2 py-1 text-xs rounded border border-bambu-dark-tertiary text-bambu-gray hover:text-white flex items-center gap-1"
                  title={t('filamentCali.history.refreshHint')}
                >
                  <RefreshCw className={`h-3 w-3 ${h.isRefreshing ? 'animate-spin' : ''}`} />
                  {t('filamentCali.history.refresh')}
                </button>
              </div>
            </div>
            {printerGroups.size === 0 && (
              <p className="text-sm text-bambu-gray">{t('filamentCali.history.empty')}</p>
            )}
            {Array.from(printerGroups.entries()).map(([key, rows]) => {
              const [dia, type] = key.split('-');
              return (
                <div key={key} className="mb-4">
                  <h4 className="text-xs text-bambu-gray mb-2">
                    {t('filamentCali.history.groupNozzle', { diameter: dia, type })}
                  </h4>
                  <div className="space-y-1">
                    {rows.map((r) => (
                      <PrinterSideRow key={r.cali_idx} r={r} />
                    ))}
                  </div>
                </div>
              );
            })}
          </section>
        </div>
      </div>
    </div>
  );
}

function BamDudeRow({
  r,
  h,
}: {
  r: FilamentCalibrationOut;
  h: ReturnType<typeof useCalibrationHistory>;
}) {
  const { t } = useTranslation();
  const onDelete = async () => {
    if (window.confirm(t('filamentCali.history.deleteConfirm'))) {
      await h.delete(r.id);
    }
  };
  return (
    <div
      className={`p-2 rounded border flex items-center justify-between ${
        r.is_active
          ? 'border-bambu-green bg-bambu-green/10'
          : 'border-bambu-dark-tertiary bg-bambu-dark'
      }`}
    >
      <div className="flex-1">
        <div className="flex items-center gap-2">
          {r.is_active && <CheckCircle2 className="h-3 w-3 text-bambu-green" />}
          <span className="text-sm text-white">{r.name}</span>
          <span className="text-xs text-bambu-gray">· {r.filament_id}</span>
        </div>
        <div className="text-xs text-bambu-gray font-mono mt-0.5">
          {r.pa_k_value != null && `K = ${r.pa_k_value.toFixed(4)}  `}
          {r.flow_ratio != null && `flow = ${r.flow_ratio.toFixed(4)}  `}
          {r.nozzle_id && `nozzle = ${r.nozzle_id}  `}
          {r.source} · {new Date(r.created_at).toLocaleDateString()}
        </div>
      </div>
      <div className="flex items-center gap-1">
        {!r.is_active && (
          <button
            onClick={() => h.setActive(r.id)}
            className="px-2 py-1 text-xs rounded border border-bambu-dark-tertiary text-bambu-gray hover:text-white"
          >
            {t('filamentCali.history.setActive')}
          </button>
        )}
        <button
          onClick={onDelete}
          className="p-1 text-bambu-gray hover:text-red-400"
          aria-label="Delete"
        >
          <Trash2 className="h-3 w-3" />
        </button>
      </div>
    </div>
  );
}

function PrinterSideRow({ r }: { r: PACalibHistoryEntryOut }) {
  return (
    <div className="p-2 rounded border border-bambu-dark-tertiary bg-bambu-dark flex items-center justify-between text-sm">
      <span className="text-white">
        [{r.cali_idx}] {r.name}
      </span>
      <span className="text-xs text-bambu-gray font-mono">
        K = {r.k_value.toFixed(4)} · {r.filament_id}
      </span>
    </div>
  );
}
