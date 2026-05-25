import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Download } from 'lucide-react';
import { api, firmwareApi } from '../api/client';
import { useToast } from '../contexts/ToastContext';

interface ItemProgress {
  status: string;
  percent: number;
  message: string;
}

/**
 * Bulk ("mass") firmware update page (#mass-firmware). Printers are grouped by
 * model into tabs; each tab picks one version (newer or older — rollback is a
 * first-class case) and pushes it to the selected printers in parallel. Printers
 * mid-print are skipped. Live per-printer status comes from the
 * ``firmware-batch-progress`` WebSocket CustomEvent, with a 2s poll fallback.
 */
export function FirmwareUpdatePage() {
  const { t } = useTranslation();
  const { showToast } = useToast();

  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [activeModel, setActiveModel] = useState<string | null>(null);
  const [versionByModel, setVersionByModel] = useState<Record<string, string>>({});
  const [runId, setRunId] = useState<number | null>(null);
  const [progress, setProgress] = useState<Record<number, ItemProgress>>({});
  const [didPreselect, setDidPreselect] = useState(false);

  const { data: printers } = useQuery({ queryKey: ['printers'], queryFn: api.getPrinters });
  const { data: updates } = useQuery({ queryKey: ['firmware-updates'], queryFn: firmwareApi.checkUpdates });

  const allIds = useMemo(() => (printers ?? []).map((p) => p.id), [printers]);

  const { data: preview } = useQuery({
    queryKey: ['firmware-preview', allIds],
    queryFn: () => firmwareApi.previewBatch(allIds.map((id) => ({ printer_id: id }))),
    enabled: allIds.length > 0,
  });
  const groups = useMemo(() => preview?.groups ?? [], [preview]);

  const skippedIds = useMemo(
    () => new Set(groups.flatMap((g) => g.skipped_printer_ids)),
    [groups],
  );
  const nameOf = (id: number) => printers?.find((p) => p.id === id)?.name ?? `#${id}`;
  const currentVersionOf = (id: number) =>
    updates?.updates.find((u) => u.printer_id === id)?.current_version ?? null;
  const modelOf = (id: number) => groups.find((g) => g.printer_ids.includes(id))?.model ?? 'Unknown';

  // Default active tab + per-model default version once preview arrives.
  useEffect(() => {
    if (groups.length && !activeModel) setActiveModel(groups[0].model);
    setVersionByModel((prev) => {
      const next = { ...prev };
      for (const g of groups) if (!(g.model in next) && g.default_version) next[g.model] = g.default_version;
      return next;
    });
  }, [groups, activeModel]);

  // Preselect printers that have an update available (once, when data loads).
  useEffect(() => {
    if (didPreselect || !updates) return;
    const ids = updates.updates.filter((u) => u.update_available).map((u) => u.printer_id);
    setSelected(new Set(ids));
    setDidPreselect(true);
  }, [updates, didPreselect]);

  // Live progress via the WebSocket CustomEvent bridge.
  useEffect(() => {
    const handler = (e: Event) => {
      const d = (e as CustomEvent<{ run_id: number; printer_id: number; status: string; percent?: number; message?: string }>).detail;
      if (runId == null || d.run_id !== runId) return;
      setProgress((p) => ({
        ...p,
        [d.printer_id]: { status: d.status, percent: d.percent ?? 0, message: d.message ?? '' },
      }));
    };
    window.addEventListener('firmware-batch-progress', handler);
    return () => window.removeEventListener('firmware-batch-progress', handler);
  }, [runId]);

  // Poll fallback while a run is active.
  const { data: run } = useQuery({
    queryKey: ['firmware-batch', runId],
    queryFn: () => firmwareApi.getBatch(runId as number),
    enabled: runId != null,
    refetchInterval: runId != null ? 2000 : false,
  });
  useEffect(() => {
    if (!run) return;
    setProgress((p) => {
      const next = { ...p };
      for (const it of run.items) {
        if (!next[it.printer_id]) {
          next[it.printer_id] = { status: it.status, percent: 0, message: it.message ?? '' };
        }
      }
      return next;
    });
  }, [run]);

  const toggle = (id: number) =>
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  const updateAllAvailable = () => {
    const ids = (updates?.updates ?? []).filter((u) => u.update_available).map((u) => u.printer_id);
    setSelected(new Set(ids));
  };

  const launch = useMutation({
    mutationFn: () => {
      const targets = [...selected]
        .filter((id) => !skippedIds.has(id))
        .map((id) => ({ printer_id: id, version: versionByModel[modelOf(id)] }));
      return firmwareApi.startBatch(targets);
    },
    onSuccess: (res) => {
      setRunId(res.run_id);
      setProgress({});
      showToast(t('firmware.batchStarted'), 'success');
    },
    onError: () => showToast(t('firmware.batchError'), 'error'),
  });

  const activeGroup = groups.find((g) => g.model === activeModel) ?? null;
  const launchableCount = [...selected].filter((id) => !skippedIds.has(id)).length;

  const statusLabel = (id: number) => {
    const pr = progress[id];
    if (!pr) return '';
    if (pr.status === 'uploading' || pr.status === 'applying') return `${t(`firmware.status.${pr.status}`)} ${pr.percent}%`;
    return t(`firmware.status.${pr.status}`, { defaultValue: pr.status });
  };

  return (
    <div className="p-4 md:p-6 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-xl font-semibold text-white flex items-center gap-2">
          <Download className="w-5 h-5 text-bambu-green" />
          {t('firmware.bulkTitle')}
        </h1>
        <div className="flex items-center gap-2">
          <button
            onClick={updateAllAvailable}
            className="px-3 py-2 rounded-lg bg-bambu-dark-tertiary text-white hover:bg-bambu-dark-tertiary/80"
          >
            {t('firmware.updateAll')}
          </button>
          <button
            onClick={() => launch.mutate()}
            disabled={launchableCount === 0 || launch.isPending || runId != null}
            className="px-4 py-2 rounded-lg bg-bambu-green text-white disabled:opacity-50"
          >
            {t('firmware.upgrade')} ({launchableCount})
          </button>
        </div>
      </div>

      {/* Model tabs */}
      <div className="flex gap-1 border-b border-bambu-dark-tertiary mb-3 overflow-x-auto">
        {groups.map((g) => (
          <button
            key={g.model}
            onClick={() => setActiveModel(g.model)}
            className={`px-3 py-2 whitespace-nowrap text-sm ${
              activeModel === g.model
                ? 'text-white border-b-2 border-bambu-green'
                : 'text-bambu-gray hover:text-white'
            }`}
          >
            {g.model} ({g.printer_ids.length})
          </button>
        ))}
      </div>

      {activeGroup && (
        <div>
          <div className="flex items-center gap-3 mb-3">
            <label className="text-sm text-bambu-gray">{t('firmware.version')}</label>
            <select
              value={versionByModel[activeGroup.model] ?? ''}
              onChange={(e) =>
                setVersionByModel((v) => ({ ...v, [activeGroup.model]: e.target.value }))
              }
              className="px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white"
            >
              {activeGroup.available_versions.map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </select>
            <span
              className={`text-xs px-2 py-1 rounded ${
                activeGroup.remote_apply ? 'bg-bambu-green/20 text-bambu-green' : 'bg-bambu-dark-tertiary text-bambu-gray'
              }`}
            >
              {activeGroup.remote_apply ? t('firmware.remoteApply') : t('firmware.manualApplyBadge')}
            </span>
          </div>

          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-bambu-gray border-b border-bambu-dark-tertiary">
                <th className="py-2 w-8"></th>
                <th className="py-2">{t('firmware.printer')}</th>
                <th className="py-2">{t('firmware.firmware')}</th>
                <th className="py-2">{t('firmware.printStatus')}</th>
                <th className="py-2">{t('firmware.upgradeStatus')}</th>
              </tr>
            </thead>
            <tbody>
              {activeGroup.printer_ids.map((id) => {
                const skipped = skippedIds.has(id);
                return (
                  <tr key={id} className="border-b border-bambu-dark-tertiary/50">
                    <td className="py-2">
                      <input
                        type="checkbox"
                        checked={selected.has(id) && !skipped}
                        disabled={skipped || runId != null}
                        onChange={() => toggle(id)}
                      />
                    </td>
                    <td className="py-2 text-white">{nameOf(id)}</td>
                    <td className="py-2 text-bambu-gray">
                      {currentVersionOf(id) ?? '—'} {'→'} {versionByModel[activeGroup.model] ?? '—'}
                    </td>
                    <td className="py-2">
                      {skipped ? (
                        <span className="text-status-warning">{t('firmware.skippedPrinting')}</span>
                      ) : (
                        <span className="text-bambu-gray">{t('firmware.idle')}</span>
                      )}
                    </td>
                    <td className="py-2 text-bambu-gray">{statusLabel(id)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {groups.length === 0 && (
        <p className="text-bambu-gray text-sm mt-6">{t('firmware.noPrinters')}</p>
      )}
    </div>
  );
}
