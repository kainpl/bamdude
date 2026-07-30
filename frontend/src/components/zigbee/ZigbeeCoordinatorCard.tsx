/**
 * Configure the Zigbee radio, connect it, pair devices, and see what is paired.
 *
 * Its own file rather than another 200 lines inside SettingsPage.tsx, which is
 * already past 7000.
 *
 * Two rules this component exists to honour:
 *
 * - `reason` is rendered verbatim. It is the whole explanation of why the radio
 *   is not up, and the messages are written to be read by an operator ("port
 *   busy - Zigbee2MQTT or Home Assistant is the most likely owner"). Mapping it
 *   to a friendlier string would throw away the only thing that says what to do.
 * - Connect saves the settings *then* restarts. Restarting against unsaved
 *   settings would reconnect to the old path and look like the new one failed.
 */

import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Loader2, Plug, RefreshCw, Trash2, Usb, Wifi } from 'lucide-react';

import { api } from '../../api/client';
import type { ZigbeeDevice } from '../../api/client';
import { Card, CardContent, CardHeader } from '../Card';
import { Button } from '../Button';
import { ConfirmModal } from '../ConfirmModal';
import { useToast } from '../../contexts/ToastContext';
import { usePairingProgress } from './usePairingProgress';

type Transport = 'ethernet' | 'usb';

// Long enough to walk to the plug and hold its button, short enough that an
// unattended window closes on its own. The backend caps the value at 254: 255
// means "permanently open" in the Zigbee spec.
const PAIRING_WINDOW_SECONDS = 60;

const STATE_STYLES: Record<string, string> = {
  up: 'bg-bambu-green/20 text-bambu-green',
  starting: 'bg-yellow-100 dark:bg-yellow-500/20 text-yellow-700 dark:text-yellow-400',
  error: 'bg-red-100 dark:bg-red-500/20 text-red-700 dark:text-red-400',
  disabled: 'bg-bambu-dark text-bambu-gray',
};

export function ZigbeeCoordinatorCard() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings });
  const { data: status } = useQuery({ queryKey: ['zigbee-status'], queryFn: api.getZigbeeStatus });
  const { data: deviceList } = useQuery({ queryKey: ['zigbee-devices'], queryFn: api.getZigbeeDevices });
  const { data: plugs } = useQuery({ queryKey: ['smart-plugs'], queryFn: api.getSmartPlugs });

  const [enabled, setEnabled] = useState(false);
  const [transport, setTransport] = useState<Transport>('ethernet');
  const [path, setPath] = useState('');
  const [removing, setRemoving] = useState<ZigbeeDevice | null>(null);

  // Seed the editable copy once the saved settings arrive. Without the guard a
  // refetch would overwrite what the operator is in the middle of typing.
  const [seeded, setSeeded] = useState(false);
  useEffect(() => {
    if (!settings || seeded) return;
    setEnabled(Boolean(settings.zigbee_enabled));
    setTransport((settings.zigbee_transport as Transport) || 'ethernet');
    setPath(settings.zigbee_path || '');
    setSeeded(true);
  }, [settings, seeded]);

  // Only asked for on USB: enumerating serial ports is pointless for a radio
  // reached over the network, and the endpoint walks the host's serial drivers.
  const { data: portList, refetch: refetchPorts } = useQuery({
    queryKey: ['zigbee-ports'],
    queryFn: api.getZigbeePorts,
    enabled: transport === 'usb',
  });

  const connect = useMutation({
    mutationFn: async () => {
      await api.updateSettings({
        zigbee_enabled: enabled,
        zigbee_transport: transport,
        zigbee_path: path,
      });
      return api.restartZigbeeCoordinator();
    },
    onSuccess: (next) => {
      queryClient.invalidateQueries({ queryKey: ['settings'] });
      queryClient.invalidateQueries({ queryKey: ['zigbee-status'] });
      queryClient.invalidateQueries({ queryKey: ['zigbee-devices'] });
      if (next.state === 'error' && next.reason) showToast(next.reason, 'error');
    },
    onError: (err: Error) => showToast(err.message, 'error'),
  });

  const pairing = usePairingProgress();
  const permit = useMutation({
    mutationFn: () => api.permitZigbeeJoin(PAIRING_WINDOW_SECONDS),
    // The countdown starts only once the window is actually open. Starting it
    // optimistically would show a timer for a window that was refused.
    onSuccess: (granted) => pairing.start(granted.seconds),
    onError: (err: Error) => showToast(err.message, 'error'),
  });

  const remove = useMutation({
    mutationFn: (ieee: string) => api.removeZigbeeDevice(ieee),
    onSuccess: () => {
      setRemoving(null);
      queryClient.invalidateQueries({ queryKey: ['zigbee-devices'] });
    },
    onError: (err: Error) => showToast(err.message, 'error'),
  });

  const state = status?.state ?? 'disabled';
  const devices = deviceList?.devices ?? [];
  const ports = portList?.ports ?? [];

  const boundName = (ieee: string) =>
    plugs?.find((p) => p.zigbee_ieee && p.zigbee_ieee.toLowerCase() === ieee.toLowerCase())?.name;

  return (
    <Card className="mb-6">
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <h3 className="text-base font-semibold text-white flex items-center gap-2">
            <Plug className="w-4 h-4 text-bambu-green" />
            {t('settings.zigbee.title')}
          </h3>
          <span className={`px-2 py-0.5 rounded text-xs font-medium ${STATE_STYLES[state]}`}>
            {t(`settings.zigbee.state.${state}`)}
          </span>
        </div>
        <p className="text-sm text-bambu-gray mt-1">{t('settings.zigbee.description')}</p>
      </CardHeader>

      <CardContent className="space-y-4">
        {/* Verbatim, never mapped: this is the only thing that says what to do. */}
        {status?.reason ? (
          <p className="text-sm text-red-600 dark:text-red-400 bg-red-100 dark:bg-red-500/10 rounded-lg p-2">
            {status.reason}
          </p>
        ) : null}

        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            className="w-4 h-4 accent-bambu-green"
          />
          <span className="text-sm text-white">{t('settings.zigbee.enabled')}</span>
        </label>

        <div>
          <label className="block text-sm text-bambu-gray mb-1">{t('settings.zigbee.transport')}</label>
          <div className="flex gap-2">
            {(['ethernet', 'usb'] as Transport[]).map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => setTransport(option)}
                className={`flex-1 flex items-center justify-center gap-2 px-3 py-2 rounded-lg font-medium transition-colors ${
                  transport === option
                    ? 'bg-bambu-green text-white'
                    : 'bg-bambu-dark text-bambu-gray hover:text-white border border-bambu-dark-tertiary'
                }`}
              >
                {option === 'ethernet' ? <Wifi className="w-4 h-4" /> : <Usb className="w-4 h-4" />}
                {t(`settings.zigbee.transport_${option}`)}
              </button>
            ))}
          </div>
        </div>

        <div>
          <label className="block text-sm text-bambu-gray mb-1">{t('settings.zigbee.path')}</label>
          {transport === 'usb' ? (
            <div className="flex gap-2">
              <select
                value={path}
                onChange={(e) => setPath(e.target.value)}
                className="flex-1 px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
              >
                <option value="">{t('settings.zigbee.pickPort')}</option>
                {ports.map((port) => (
                  <option key={port.device} value={port.device}>
                    {port.description ? `${port.device} — ${port.description}` : port.device}
                  </option>
                ))}
              </select>
              <Button variant="secondary" onClick={() => refetchPorts()} title={t('settings.zigbee.refreshPorts')}>
                <RefreshCw className="w-4 h-4" />
              </Button>
            </div>
          ) : (
            <input
              type="text"
              value={path}
              onChange={(e) => setPath(e.target.value)}
              placeholder="192.168.1.50:6638"
              className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
            />
          )}
          <p className="text-xs text-bambu-gray mt-1">
            {transport === 'usb'
              ? // A machine with no serial ports is normal, not a failure.
                ports.length === 0
                ? t('settings.zigbee.noSerialPorts')
                : t('settings.zigbee.pathUsbHint')
              : t('settings.zigbee.pathEthernetHint')}
          </p>
        </div>

        <div className="flex gap-2">
          <Button onClick={() => connect.mutate()} disabled={connect.isPending}>
            {connect.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
            {state === 'up' ? t('settings.zigbee.reconnect') : t('settings.zigbee.connect')}
          </Button>
          {/* Refused against a dead radio: `permit` returns cleanly and then does
              nothing for the whole window, so the operator would watch a
              countdown that was never going to work. */}
          <Button variant="secondary" onClick={() => permit.mutate()} disabled={state !== 'up' || permit.isPending}>
            {permit.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plug className="w-4 h-4" />}
            {t('settings.zigbee.pairDevice')}
          </Button>
        </div>

        {pairing.phase === 'pairing' ? (
          <div className="bg-bambu-dark rounded-lg p-3 space-y-1">
            <p className="text-sm text-white">
              {t('settings.zigbee.pairingCountdown', { seconds: pairing.secondsLeft })}
            </p>
            {pairing.events.map((event, index) => (
              <p
                key={`${event.kind}-${event.ieee}-${index}`}
                className={`text-xs ${
                  event.kind === 'rejected'
                    ? 'text-red-600 dark:text-red-400'
                    : event.kind === 'paired'
                      ? 'text-bambu-green'
                      : 'text-bambu-gray'
                }`}
              >
                {event.kind === 'joining'
                  ? t('settings.zigbee.pairingJoining')
                  : // Rejection is spelled out rather than hidden: the backend
                    // removed the device from the network, and an operator who
                    // is not told that will wonder where their sensor went.
                    t(`settings.zigbee.pairing${event.kind === 'paired' ? 'Paired' : 'Rejected'}`, {
                      name: event.model || event.ieee,
                    })}
              </p>
            ))}
          </div>
        ) : null}

        {/* Never the network key: it is deliberately absent from the response and
            must stay absent — losing it means re-pairing every device by hand. */}
        {state === 'up' && status?.coordinator ? (
          <p className="text-xs text-bambu-gray">
            {[status.coordinator.manufacturer, status.coordinator.model].filter(Boolean).join(' ')}
            {' · '}
            {status.coordinator.ieee}
            {status.coordinator.version ? ` · v${status.coordinator.version}` : ''}
            {status.network?.channel != null
              ? ` · ${t('settings.zigbee.channel')} ${status.network.channel}`
              : ''}
            {status.network?.pan_id != null ? ` · PAN ${status.network.pan_id}` : ''}
          </p>
        ) : null}

        <div className="border-t border-bambu-dark-tertiary pt-4">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm text-white">{t('settings.zigbee.pairedDevices')}</span>
          </div>

          {devices.length === 0 ? (
            <p className="text-xs text-bambu-gray">{t('settings.zigbee.noPairedDevices')}</p>
          ) : (
            <ul className="space-y-2">
              {devices.map((device) => {
                const bound = boundName(device.ieee);
                return (
                  <li
                    key={device.ieee}
                    className="flex items-center justify-between gap-2 bg-bambu-dark rounded-lg px-3 py-2"
                  >
                    <div className="min-w-0">
                      <p className="text-sm text-white truncate">
                        {device.model || device.ieee}
                        {device.manufacturer ? <span className="text-bambu-gray"> · {device.manufacturer}</span> : null}
                      </p>
                      <p className="text-xs text-bambu-gray truncate">
                        {device.ieee}
                        {' · '}
                        {device.has_metering || device.has_electrical_measurement
                          ? t('settings.zigbee.capabilityEnergy')
                          : t('settings.zigbee.capabilitySwitchOnly')}
                      </p>
                      {/* Which devices are still free is otherwise invisible
                          without cross-checking the plug list by hand. */}
                      {bound ? (
                        <p className="text-xs text-bambu-green truncate">
                          {t('settings.zigbee.boundTo', { name: bound })}
                        </p>
                      ) : null}
                    </div>
                    <button
                      type="button"
                      onClick={() => setRemoving(device)}
                      title={t('settings.zigbee.removeDevice')}
                      aria-label={t('settings.zigbee.removeDevice')}
                      className="p-1.5 rounded text-bambu-gray hover:text-red-500 hover:bg-bambu-dark-tertiary transition-colors shrink-0"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </CardContent>

      {removing ? (
        <ConfirmModal
          title={t('settings.zigbee.removeDevice')}
          // Says "from the network", because that is what the backend does — the
          // device has to be physically re-paired afterwards.
          message={t('settings.zigbee.removeDeviceConfirm', { name: removing.model || removing.ieee })}
          confirmText={t('settings.zigbee.removeDevice')}
          onConfirm={() => remove.mutate(removing.ieee)}
          onCancel={() => setRemoving(null)}
          variant="danger"
        />
      ) : null}
    </Card>
  );
}
