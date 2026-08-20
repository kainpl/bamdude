/**
 * Settings → Printing → Label printers.
 *
 * A label printer hangs off somebody's desktop, where a server in a container
 * cannot reach it. So a bridge app there polls us, introduces itself, and this
 * panel is where that introduction is answered.
 *
 * ⚠️ **Adopting is the point of this screen.** A device arrives listed and
 * switched off, and stays that way until a person enables it — authenticating
 * proves the app is ours, not that the printer behind it should be given our
 * labels. Everything else here is reported by the device and read-only.
 *
 * Lives inside an existing Card so it draws no page chrome of its own.
 */
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, Loader2, Plus, Printer, Trash2, X } from 'lucide-react';
import { api, type LabelCassette, type LabelDevice, type LabelJob } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { Button } from '../Button';

/** Fast enough to feel live while somebody is adopting a device, slow enough
 *  that leaving the tab open is not a poll every second forever. */
const REFRESH_MS = 5000;

function relative(iso: string | null, t: (k: string, o?: Record<string, unknown>) => string): string {
  if (!iso) return t('labelDevices.never');
  // ⚠️ The server stores UTC without an offset. Left bare, the browser reads it
  // as local time and a device seen a second ago reads as hours in the future.
  const stamp = iso.endsWith('Z') || iso.includes('+') ? iso : `${iso}Z`;
  const seconds = Math.round((Date.now() - new Date(stamp).getTime()) / 1000);
  if (seconds < 60) return t('labelDevices.justNow');
  if (seconds < 3600) return t('labelDevices.minutesAgo', { count: Math.round(seconds / 60) });
  if (seconds < 86400) return t('labelDevices.hoursAgo', { count: Math.round(seconds / 3600) });
  return t('labelDevices.daysAgo', { count: Math.round(seconds / 86400) });
}

export function LabelDevicesSettings() {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const qc = useQueryClient();

  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings });
  const [enabled, setEnabled] = useState(false);
  // Seeded once. Without the guard a refetch would flip the switch back under
  // somebody who has just clicked it.
  const [seeded, setSeeded] = useState(false);
  useEffect(() => {
    if (!settings || seeded) return;
    setEnabled(Boolean(settings.device_labels_enabled));
    setSeeded(true);
  }, [settings, seeded]);

  const toggle = useMutation({
    mutationFn: (next: boolean) => api.updateSettings({ device_labels_enabled: next }),
    onSuccess: (_data, next) => {
      qc.invalidateQueries({ queryKey: ['settings'] });
      qc.invalidateQueries({ queryKey: ['label-devices'] });
      setEnabled(next);
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const { data: devices, isLoading } = useQuery({
    queryKey: ['label-devices'],
    queryFn: api.getLabelDevices,
    // ⚠️ Not asked at all while the subsystem is off. Gating only the interval
    // still fires the first request — a farm with no bridge should not be
    // asking a question whose answer is always the same empty list.
    enabled,
    refetchInterval: enabled ? REFRESH_MS : false,
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ['label-devices'] });

  const update = useMutation({
    mutationFn: ({ id, body }: { id: number; body: { enabled?: boolean; name?: string | null } }) =>
      api.updateLabelDevice(id, body),
    onSuccess: () => invalidate(),
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const remove = useMutation({
    mutationFn: (id: number) => api.deleteLabelDevice(id),
    onSuccess: () => {
      invalidate();
      showToast(t('labelDevices.forgotten'), 'success');
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const rows = devices ?? [];
  const waiting = rows.filter((d) => !d.enabled);
  const adopted = rows.filter((d) => d.enabled);

  return (
    <div className="space-y-4">
      <label className="flex items-start gap-3 cursor-pointer">
        <input
          type="checkbox"
          checked={enabled}
          disabled={toggle.isPending}
          onChange={(e) => toggle.mutate(e.target.checked)}
          className="w-4 h-4 mt-0.5 text-bambu-green rounded border-bambu-dark-tertiary bg-bambu-dark focus:ring-bambu-green"
        />
        <span>
          <span className="text-white">{t('labelDevices.enableSubsystem')}</span>
          <p className="text-xs text-bambu-gray">{t('labelDevices.enableSubsystemHint')}</p>
        </span>
      </label>

      {!enabled && <p className="text-xs text-bambu-gray italic">{t('labelDevices.offHint')}</p>}

      {enabled && isLoading && <p className="text-sm text-bambu-gray">{t('common.loading')}</p>}

      {enabled && rows.length === 0 && !isLoading && (
        <p className="text-sm text-bambu-gray italic">{t('labelDevices.none')}</p>
      )}

      {enabled && waiting.length > 0 && (
        <div className="space-y-2">
          <h4 className="text-sm font-medium text-white">{t('labelDevices.waitingHeading')}</h4>
          <p className="text-xs text-bambu-gray">{t('labelDevices.waitingHint')}</p>
          {waiting.map((device) => (
            <DeviceRow
              key={device.id}
              device={device}
              onEnable={() => update.mutate({ id: device.id, body: { enabled: true } })}
              onDisable={() => update.mutate({ id: device.id, body: { enabled: false } })}
              onForget={() => remove.mutate(device.id)}
              busy={update.isPending || remove.isPending}
            />
          ))}
        </div>
      )}

      {enabled && adopted.length > 0 && (
        <div className="space-y-2">
          <h4 className="text-sm font-medium text-white">{t('labelDevices.enabledHeading')}</h4>
          {adopted.map((device) => (
            <DeviceRow
              key={device.id}
              device={device}
              onEnable={() => update.mutate({ id: device.id, body: { enabled: true } })}
              onDisable={() => update.mutate({ id: device.id, body: { enabled: false } })}
              onForget={() => remove.mutate(device.id)}
              busy={update.isPending || remove.isPending}
            />
          ))}
        </div>
      )}

      {enabled && <CassetteCatalogue devices={rows} />}
      {enabled && <RecentJobs />}
    </div>
  );
}

function DeviceRow({
  device,
  onEnable,
  onDisable,
  onForget,
  busy,
}: {
  device: LabelDevice;
  onEnable: () => void;
  onDisable: () => void;
  onForget: () => void;
  busy: boolean;
}) {
  const { t } = useTranslation();

  const size =
    device.cassette_width_mm && device.cassette_height_mm
      ? `${device.cassette_width_mm} × ${device.cassette_height_mm} mm`
      : device.cassette_barcode
        ? t('labelDevices.cassetteUnknown')
        : t('labelDevices.cassetteNone');

  return (
    <div className="p-3 bg-bambu-dark rounded-lg space-y-2">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Printer className="w-4 h-4 text-bambu-gray shrink-0" />
            <span className="text-white truncate">
              {device.name || device.model || t('labelDevices.unnamed')}
            </span>
            {device.enabled && device.queued > 0 && (
              <span className="px-1.5 py-0.5 text-xs bg-bambu-green/20 text-bambu-green rounded">
                {t('labelDevices.queued', { count: device.queued })}
              </span>
            )}
          </div>
          {/* The id is how somebody matches this row to the machine in front of
              them — the bridge shows the same string in its own window. */}
          <code className="block text-xs text-bambu-gray break-all mt-1">{device.installation_id}</code>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          {device.enabled ? (
            <Button variant="secondary" size="sm" disabled={busy} onClick={onDisable}>
              <X className="w-4 h-4" />
              {t('labelDevices.disable')}
            </Button>
          ) : (
            <Button size="sm" disabled={busy} onClick={onEnable}>
              <Check className="w-4 h-4" />
              {t('labelDevices.enable')}
            </Button>
          )}
          <Button variant="secondary" size="sm" disabled={busy} onClick={onForget}>
            <Trash2 className="w-4 h-4 text-red-600 dark:text-red-400" />
          </Button>
        </div>
      </div>

      <ul className="text-xs text-bambu-gray space-y-0.5">
        <li>
          {/* ⚠️ Two different failures with two different fixes: the bridge
              being gone, and the bridge being here with the cable out. */}
          {device.printer_reachable
            ? t('labelDevices.printerReachable')
            : t('labelDevices.printerUnreachable')}
          {' · '}
          {t('labelDevices.lastSeen', { when: relative(device.last_seen_at, t) })}
        </li>
        <li>
          {t('labelDevices.cassette')}: {size}
          {device.cassette_barcode && ` (${device.cassette_barcode})`}
        </li>
        <li>
          {device.paper_state === 0
            ? t('labelDevices.noPaper')
            : device.paper_state === null
              ? t('labelDevices.paperUnknown')
              : t('labelDevices.paperLoaded')}
          {device.power_level !== null && ` · ${t('labelDevices.charge', { level: device.power_level })}`}
          {device.app_version && ` · ${t('labelDevices.bridgeVersion', { version: device.app_version })}`}
        </li>
      </ul>
    </div>
  );
}

/**
 * ⚠️ **Taught, never fetched.** A self-hosted install does not send consumable
 * identifiers to a vendor's cloud to find out how big a sticker is. The person
 * holding the cassette can read the box; every machine with that stock then
 * resolves it on its next poll.
 */
function CassetteCatalogue({ devices }: { devices: LabelDevice[] }) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const qc = useQueryClient();
  const [barcode, setBarcode] = useState('');
  const [width, setWidth] = useState('');
  const [height, setHeight] = useState('');

  const { data: cassettes } = useQuery({ queryKey: ['label-cassettes'], queryFn: api.getLabelCassettes });

  const teach = useMutation({
    mutationFn: () =>
      api.putLabelCassette(barcode.trim(), { width_mm: Number(width), height_mm: Number(height) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['label-cassettes'] });
      qc.invalidateQueries({ queryKey: ['label-devices'] });
      setBarcode('');
      setWidth('');
      setHeight('');
      showToast(t('labelDevices.cassetteTaught'), 'success');
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const forget = useMutation({
    mutationFn: (code: string) => api.forgetLabelCassette(code),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['label-cassettes'] });
      qc.invalidateQueries({ queryKey: ['label-devices'] });
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  // A barcode a device has reported but nobody has taught is the one worth
  // offering — it is exactly what is blocking that printer from being used.
  const known = new Set((cassettes ?? []).map((c: LabelCassette) => c.barcode));
  const unknown = devices
    .map((d) => d.cassette_barcode)
    .filter((code): code is string => !!code && !known.has(code));

  const valid = barcode.trim() !== '' && Number(width) > 0 && Number(height) > 0;

  return (
    <div className="space-y-2 pt-2 border-t border-bambu-dark-tertiary">
      <h4 className="text-sm font-medium text-white">{t('labelDevices.cassettesHeading')}</h4>
      <p className="text-xs text-bambu-gray">{t('labelDevices.cassettesHint')}</p>

      {unknown.length > 0 && (
        <p className="text-xs text-amber-600 dark:text-amber-400">
          {t('labelDevices.cassetteUnknownLoaded', { codes: [...new Set(unknown)].join(', ') })}
        </p>
      )}

      {(cassettes ?? []).map((cassette: LabelCassette) => (
        <div key={cassette.id} className="flex items-center justify-between text-xs bg-bambu-dark rounded px-2 py-1.5">
          <span className="text-bambu-gray">
            <code>{cassette.barcode}</code> — {cassette.width_mm} × {cassette.height_mm} mm
            {cassette.name && ` · ${cassette.name}`}
          </span>
          <button
            type="button"
            className="text-red-600 dark:text-red-400"
            onClick={() => forget.mutate(cassette.barcode)}
          >
            <Trash2 className="w-3.5 h-3.5" />
          </button>
        </div>
      ))}

      <div className="flex flex-wrap items-center gap-2">
        <input
          value={barcode}
          onChange={(e) => setBarcode(e.target.value)}
          placeholder={t('labelDevices.barcodePlaceholder')}
          className="flex-1 min-w-40 px-2 py-1 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded"
        />
        <input
          value={width}
          onChange={(e) => setWidth(e.target.value)}
          inputMode="decimal"
          placeholder={t('labelDevices.widthPlaceholder')}
          className="w-20 px-2 py-1 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded"
        />
        <input
          value={height}
          onChange={(e) => setHeight(e.target.value)}
          inputMode="decimal"
          placeholder={t('labelDevices.heightPlaceholder')}
          className="w-20 px-2 py-1 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded"
        />
        <Button size="sm" disabled={!valid || teach.isPending} onClick={() => teach.mutate()}>
          {teach.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
          {t('labelDevices.teach')}
        </Button>
      </div>
    </div>
  );
}

function RecentJobs() {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const qc = useQueryClient();

  const { data: jobs } = useQuery({
    queryKey: ['label-jobs'],
    queryFn: () => api.getLabelJobs(),
    refetchInterval: REFRESH_MS,
  });

  const cancel = useMutation({
    mutationFn: (id: number) => api.cancelLabelJob(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['label-jobs'] }),
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const rows = (jobs ?? []).slice(0, 10);
  if (rows.length === 0) return null;

  return (
    <div className="space-y-2 pt-2 border-t border-bambu-dark-tertiary">
      <h4 className="text-sm font-medium text-white">{t('labelDevices.jobsHeading')}</h4>
      {rows.map((job: LabelJob) => (
        <div key={job.id} className="flex items-center justify-between text-xs bg-bambu-dark rounded px-2 py-1.5">
          <span className="text-bambu-gray truncate">
            #{job.id} · {t(`labelDevices.status.${job.status}`, job.status)}
            {job.spool_id !== null && ` · ${t('labelDevices.forSpool', { id: job.spool_id })}`}
            {/* The device's own words, not a paraphrase — it is the only
                diagnosis there is. */}
            {job.error && ` — ${job.error}`}
          </span>
          {job.status === 'queued' && (
            <button type="button" className="text-bambu-gray" onClick={() => cancel.mutate(job.id)}>
              <X className="w-3.5 h-3.5" />
            </button>
          )}
        </div>
      ))}
    </div>
  );
}
