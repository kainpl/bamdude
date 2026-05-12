import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { CheckCircle2, AlertTriangle, XCircle } from 'lucide-react';
import { api } from '../../api/client';
import type {
  AutoResultEditIn,
  CalibrationSessionOut,
  ExtrusionCaliResultOut,
} from '../../api/client';

interface Props {
  session: CalibrationSessionOut;
  onSubmit: (body: { results: AutoResultEditIn[] }) => Promise<unknown>;
  isSubmitting: boolean;
}

interface EditState {
  tray_id: number;
  save: boolean;
  name: string;
  k_value: number;
  n_coef: number;
  flow_ratio: number;
}

function confidenceBadge(c: number) {
  if (c === 0) return { icon: CheckCircle2, cls: 'text-bambu-green', label: 'Success' };
  if (c === 1) return { icon: AlertTriangle, cls: 'text-yellow-500', label: 'Uncertain' };
  return { icon: XCircle, cls: 'text-red-500', label: 'Failed' };
}

export function CalibrationAutoSavePage({ session, onSubmit, isSubmitting }: Props) {
  const { t } = useTranslation();
  const isFlow = session.cali_mode === 'flow_rate';

  const resultsQuery = useQuery<ExtrusionCaliResultOut[]>({
    queryKey: ['calibration', 'auto-results', session.printer_id],
    queryFn: () => api.getCalibrationAutoResults(session.printer_id),
    staleTime: 1_000,
    refetchInterval: 3_000,
  });

  const [edits, setEdits] = useState<Record<number, EditState>>({});

  useEffect(() => {
    if (!resultsQuery.data) return;
    setEdits((prev) => {
      const next = { ...prev };
      for (const r of resultsQuery.data!) {
        if (next[r.tray_id]) continue;
        next[r.tray_id] = {
          tray_id: r.tray_id,
          save: r.confidence === 0,
          name: isFlow
            ? `${r.filament_id} flow ${r.k_value.toFixed(3)}`
            : `${r.filament_id} PA ${r.k_value.toFixed(4)}`,
          k_value: r.k_value,
          n_coef: r.n_coef,
          flow_ratio: r.k_value,
        };
      }
      return next;
    });
  }, [resultsQuery.data, isFlow]);

  const patch = (tray: number, p: Partial<EditState>) =>
    setEdits((prev) => ({ ...prev, [tray]: { ...prev[tray], ...p } }));

  const submit = async () => {
    const body: AutoResultEditIn[] = Object.values(edits).map((e) => ({
      tray_id: e.tray_id,
      save: e.save,
      name: e.name,
      ...(isFlow ? { flow_ratio: e.flow_ratio } : { k_value: e.k_value, n_coef: e.n_coef }),
    }));
    await onSubmit({ results: body });
  };

  return (
    <div className="space-y-4">
      <h3 className="text-base font-semibold text-white">{t('filamentCali.autoSave.heading')}</h3>
      <p className="text-sm text-bambu-gray">{t('filamentCali.autoSave.instruction')}</p>

      {resultsQuery.isLoading && (
        <div className="text-sm text-bambu-gray">{t('filamentCali.autoSave.waiting')}</div>
      )}

      {resultsQuery.data?.map((r) => {
        const conf = confidenceBadge(r.confidence);
        const Icon = conf.icon;
        const e = edits[r.tray_id];
        if (!e) return null;
        return (
          <div
            key={r.tray_id}
            className="p-3 bg-bambu-dark rounded border border-bambu-dark-tertiary space-y-2"
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Icon className={`h-4 w-4 ${conf.cls}`} />
                <span className="text-sm text-white">
                  AMS {r.ams_id + 1} Slot {r.slot_id + 1} · {r.filament_id}
                </span>
              </div>
              <label className="text-xs text-bambu-gray flex items-center gap-1">
                <input
                  type="checkbox"
                  checked={e.save}
                  onChange={(ev) => patch(r.tray_id, { save: ev.target.checked })}
                />
                {t('filamentCali.autoSave.apply')}
              </label>
            </div>

            {isFlow ? (
              <div className="grid grid-cols-2 gap-2 text-sm">
                <label>
                  <span className="text-xs text-bambu-gray">{t('filamentCali.autoSave.flowRatio')}</span>
                  <input
                    type="number"
                    step="0.001"
                    value={e.flow_ratio}
                    onChange={(ev) =>
                      patch(r.tray_id, { flow_ratio: parseFloat(ev.target.value) || 0 })
                    }
                    className="w-full bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded px-2 py-1 text-white font-mono"
                  />
                </label>
              </div>
            ) : (
              <div className="grid grid-cols-2 gap-2 text-sm">
                <label>
                  <span className="text-xs text-bambu-gray">K</span>
                  <input
                    type="number"
                    step="0.0001"
                    value={e.k_value}
                    onChange={(ev) =>
                      patch(r.tray_id, { k_value: parseFloat(ev.target.value) || 0 })
                    }
                    className="w-full bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded px-2 py-1 text-white font-mono"
                  />
                </label>
                <label>
                  <span className="text-xs text-bambu-gray">N</span>
                  <input
                    type="number"
                    step="0.01"
                    value={e.n_coef}
                    onChange={(ev) =>
                      patch(r.tray_id, { n_coef: parseFloat(ev.target.value) || 0 })
                    }
                    className="w-full bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded px-2 py-1 text-white font-mono"
                  />
                </label>
              </div>
            )}

            <label className="block text-sm">
              <span className="text-xs text-bambu-gray">{t('filamentCali.autoSave.name')}</span>
              <input
                type="text"
                value={e.name}
                onChange={(ev) => patch(r.tray_id, { name: ev.target.value })}
                className="w-full bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded px-2 py-1 text-white"
              />
            </label>
          </div>
        );
      })}

      <div className="flex justify-end pt-2 border-t border-bambu-dark-tertiary">
        <button
          type="button"
          onClick={submit}
          disabled={isSubmitting || resultsQuery.isLoading}
          className="px-4 py-2 rounded bg-bambu-green text-white text-sm font-medium disabled:opacity-40"
        >
          {t('filamentCali.autoSave.saveSelected')}
        </button>
      </div>
    </div>
  );
}
