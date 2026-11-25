import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Cpu } from 'lucide-react';
import { ApiError, api, firmwareApi } from '../api/client';
import { Button } from '../components/Button';
import { Select } from '../components/Select';
import { useToast } from '../contexts/ToastContext';
import { useActiveFirmwareBatches } from '../hooks/useActiveFirmwareBatches';
import { firmwareBatchLabel } from '../utils/firmwareBatchLabel';
import { compareFwVersions } from '../utils/firmwareVersion';

/**
 * Bulk ("mass") firmware update page (#mass-firmware). Printers are grouped by
 * model into tabs; each tab picks one version (newer or older — rollback is a
 * first-class case) and pushes it to the selected printers in parallel. Printers
 * mid-print are skipped. The backend's current run and byte progress are
 * polled, and WebSocket events bring forward re-reads on stage changes.
 */
export function FirmwareUpdatePage() {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const [view, setView] = useState<'update' | 'log'>('update');
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [activeModel, setActiveModel] = useState<string | null>(null);
  const [versionByModel, setVersionByModel] = useState<Record<string, string>>({});
  const [startedRunId, setStartedRunId] = useState<number | null>(null);
  const [didPreselect, setDidPreselect] = useState(false);

  const { data: printers } = useQuery({ queryKey: ['printers'], queryFn: api.getPrinters });
  const { data: updates } = useQuery({ queryKey: ['firmware-updates'], queryFn: firmwareApi.checkUpdates });
  const { data: logRuns } = useQuery({
    queryKey: ['firmware-batches'],
    queryFn: () => firmwareApi.listBatches(),
  });
  const { data: activeRuns } = useActiveFirmwareBatches();
  const latestBulk = logRuns?.find((batch) => batch.source === 'bulk');
  const runId = startedRunId ?? activeRuns?.[0]?.id ?? latestBulk?.id ?? null;
  const { data: runDetails } = useQuery({
    queryKey: ['firmware-batch', runId],
    queryFn: () => firmwareApi.getBatch(runId as number),
    enabled: runId !== null,
    refetchInterval: (query) => !query.state.data || query.state.data.status === 'running' ? 2000 : false,
  });
  const run = runDetails ?? activeRuns?.find((batch) => batch.id === runId) ??
    logRuns?.find((batch) => batch.id === runId);
  const busy = run?.status === 'running' || (startedRunId !== null && !run);

  const allIds = useMemo(() => (printers ?? []).map((p) => p.id), [printers]);

  const { data: preview } = useQuery({
    queryKey: ['firmware-preview', allIds],
    queryFn: () => firmwareApi.previewBatch(allIds.map((id) => ({ printer_id: id }))),
    enabled: allIds.length > 0,
  });
  const groups = useMemo(() => preview?.groups ?? [], [preview]);
  const upgradableIds = useMemo(() => (updates?.updates ?? [])
    .filter((u) => {
      const group = groups.find((g) => g.printer_ids.includes(u.printer_id));
      return u.update_available && !!u.current_version && !!group?.default_version &&
        compareFwVersions(group.default_version, u.current_version) > 0;
    })
    .map((u) => u.printer_id), [updates, groups]);

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
    if (didPreselect || !updates || groups.length === 0) return;
    setSelected(new Set(upgradableIds));
    setDidPreselect(true);
  }, [updates, groups, upgradableIds, didPreselect]);

  const toggle = (id: number) =>
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  const updateAllAvailable = () => setSelected(new Set(upgradableIds));

  const launch = useMutation({
    mutationFn: () => {
      const targets = [...selected]
        .filter((id) => {
          const group = groups.find((g) => g.printer_ids.includes(id));
          return !skippedIds.has(id) && !!group && group.available_versions.includes(versionByModel[group.model] ?? '');
        })
        .map((id) => ({ printer_id: id, version: versionByModel[modelOf(id)] }));
      return firmwareApi.startBatch(targets);
    },
    onSuccess: (res) => {
      setStartedRunId(res.run_id);
      queryClient.invalidateQueries({ queryKey: ['firmware-active-batches'] });
      queryClient.invalidateQueries({ queryKey: ['firmware-batches'] });
      showToast(t('firmware.batchStarted'), 'success');
    },
    onError: (error) => showToast(
      t(error instanceof ApiError && error.code === 'firmware_file_unavailable'
        ? 'firmware.offlineUnavailableError'
        : 'firmware.batchError'),
      'error',
    ),
  });

  const downloadToStore = useMutation({
    mutationFn: ({ model, version }: { model: string; version: string }) =>
      firmwareApi.downloadToStore(model, version),
    onSuccess: () => {
      showToast(t('firmware.downloadedToStore'), 'success');
      queryClient.invalidateQueries({ queryKey: ['firmware-preview'] });
    },
    onError: () => showToast(t('firmware.downloadStoreError'), 'error'),
  });

  const activeGroup = groups.find((g) => g.model === activeModel) ?? null;
  const launchableCount = [...selected].filter((id) => {
    const group = groups.find((g) => g.printer_ids.includes(id));
    return !skippedIds.has(id) && !!group && group.available_versions.includes(versionByModel[group.model] ?? '');
  }).length;
  const activeVersion = activeGroup ? versionByModel[activeGroup.model] : undefined;
  const activeVersionCached =
    !!activeGroup && !!activeVersion && (activeGroup.cached_versions ?? []).includes(activeVersion);
  const unavailableAnnouncement = activeGroup && updates?.updates.find((u) =>
    activeGroup.printer_ids.includes(u.printer_id) && u.update_available &&
    !!u.latest_version && !activeGroup.available_versions.includes(u.latest_version),
  )?.latest_version;

  return (
    <div className="p-4">
      <div className="flex items-center justify-between gap-3 mb-4">
        <h1 className="text-2xl font-bold text-white flex items-center gap-3">
          <Cpu className="w-6 h-6 text-bambu-green" />
          {t('firmware.bulkTitle')}
        </h1>
        {view === 'update' && (
          <div className="flex items-center gap-2">
            <Button variant="secondary" size="sm" onClick={updateAllAvailable}>
              {t('firmware.updateAll')}
            </Button>
            <Button
              variant="primary"
              size="sm"
              onClick={() => launch.mutate()}
              disabled={launchableCount === 0 || launch.isPending || busy}
            >
              {t('firmware.upgrade')} ({launchableCount})
            </Button>
          </div>
        )}
      </div>

      {/* Top-level view switcher: Update vs Update Log */}
      <div className="flex gap-1 border-b border-bambu-dark-tertiary mb-4">
        <button
          onClick={() => setView('update')}
          className={`px-3 py-2 text-sm ${view === 'update' ? 'text-white border-b-2 border-bambu-green' : 'text-bambu-gray hover:text-white'}`}
        >
          {t('firmware.tabUpdate')}
        </button>
        <button
          onClick={() => setView('log')}
          className={`px-3 py-2 text-sm ${view === 'log' ? 'text-white border-b-2 border-bambu-green' : 'text-bambu-gray hover:text-white'}`}
        >
          {t('firmware.tabLog')}
        </button>
      </div>

      {view === 'log' && (
        <div className="space-y-3">
          {(logRuns ?? []).length === 0 && (
            <p className="text-bambu-gray text-sm">{t('firmware.logEmpty')}</p>
          )}
          {(logRuns ?? []).map((run) => (
            <div key={run.id} className="rounded-lg border border-bambu-dark-tertiary p-3">
              <div className="flex items-center gap-2 mb-2 text-sm">
                <span className="text-white">
                  {run.created_at ? new Date(run.created_at).toLocaleString() : `#${run.id}`}
                </span>
                <span
                  className={`text-xs px-2 py-0.5 rounded ${
                    run.source === 'single' ? 'bg-bambu-dark-tertiary text-bambu-gray' : 'bg-bambu-green/20 text-bambu-green'
                  }`}
                >
                  {run.source === 'single' ? t('firmware.sourceSingle') : t('firmware.sourceBulk')}
                </span>
                <span className="text-bambu-gray text-xs">
                  ✓{run.succeeded} · ⏭{run.skipped} · ✗{run.failed}
                </span>
              </div>
              <table className="w-full text-xs">
                <tbody>
                  {run.items.map((it) => (
                    <tr key={`${run.id}-${it.printer_id}`} className="text-bambu-gray">
                      <td className="py-1 pr-3 text-white">{nameOf(it.printer_id)}</td>
                      <td className="py-1 pr-3">
                        {it.from_version ?? '—'} {'→'} {it.to_version}
                      </td>
                      <td className="py-1">
                        {firmwareBatchLabel(it, t)}
                        {it.error && <p className="text-status-error">{it.error}</p>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </div>
      )}

      {view === 'update' && (
      <>
      {run && (
        <section className="mb-4 rounded-lg border border-bambu-dark-tertiary p-3">
          <h2 className="mb-2 font-medium text-white">{t('firmware.upgradeStatus')} · #{run.id}</h2>
          <ul className="divide-y divide-bambu-dark-tertiary text-sm">
            {run.items.map((item) => (
              <li key={item.printer_id} className="py-2">
                <div className="flex flex-wrap justify-between gap-x-3 gap-y-1">
                  <span className="text-white">{nameOf(item.printer_id)} · {item.to_version}</span>
                  <span className="text-bambu-gray">{firmwareBatchLabel(item, t)}</span>
                </div>
                {item.status === 'uploading' && (
                  <progress
                    className="h-2 w-full accent-bambu-green"
                    aria-label={`${nameOf(item.printer_id)}: ${t('firmware.upgradeStatus')}`}
                    max={100}
                    value={item.percent ?? undefined}
                  />
                )}
                {item.error && <p className="text-status-error">{item.error}</p>}
                {item.status === 'uploaded' && item.message?.startsWith('firmware.manualApply') && (
                  <p className="text-xs text-bambu-gray">{t(item.message)}</p>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
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
          {unavailableAnnouncement && (
            <p className="mb-3 rounded-lg border border-status-warning/40 px-3 py-2 text-sm text-status-warning" role="status">
              {t('firmware.offlineUnavailable', { version: unavailableAnnouncement })}
            </p>
          )}
          <div className="flex items-center gap-3 mb-3">
            <label className="text-sm text-bambu-gray">{t('firmware.version')}</label>
            <Select
              value={versionByModel[activeGroup.model] ?? ''}
              onChange={(e) =>
                setVersionByModel((v) => ({ ...v, [activeGroup.model]: e.target.value }))
              }
            >
              {activeGroup.available_versions.map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </Select>
            <span
              className={`text-xs px-2 py-1 rounded ${
                activeGroup.remote_apply ? 'bg-bambu-green/20 text-bambu-green' : 'bg-bambu-dark-tertiary text-bambu-gray'
              }`}
            >
              {activeGroup.remote_apply ? t('firmware.remoteApply') : t('firmware.manualApplyBadge')}
            </span>

            {/* Store status for the selected version + pre-cache without touching a printer. */}
            {activeVersionCached ? (
              <span className="text-xs px-2 py-1 rounded bg-bambu-green/20 text-bambu-green">
                ✓ {t('firmware.inStore')}
              </span>
            ) : (
              <button
                onClick={() =>
                  activeVersion && downloadToStore.mutate({ model: activeGroup.model, version: activeVersion })
                }
                disabled={!activeVersion || downloadToStore.isPending}
                className="text-xs px-2 py-1 rounded bg-bambu-dark-tertiary text-bambu-gray hover:text-white disabled:opacity-50"
                title={t('firmware.notInStore')}
              >
                ↓ {t('firmware.downloadToStore')}
              </button>
            )}
          </div>

          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-bambu-gray border-b border-bambu-dark-tertiary">
                <th className="py-2 w-8"></th>
                <th className="py-2">{t('firmware.printer')}</th>
                <th className="py-2">{t('firmware.firmware')}</th>
                <th className="py-2">{t('firmware.printStatus')}</th>
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
                        disabled={skipped || busy || activeGroup.available_versions.length === 0}
                        onChange={() => toggle(id)}
                      />
                    </td>
                    <td className="accent-bambu-green py-2 text-white">{nameOf(id)}</td>
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
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {groups.length === 0 && (
        <p className="text-bambu-gray text-sm mt-4">{t('firmware.noPrinters')}</p>
      )}
      </>
      )}
    </div>
  );
}
