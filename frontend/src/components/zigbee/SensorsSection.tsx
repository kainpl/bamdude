import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Thermometer } from 'lucide-react';

import { api } from '../../api/client';
import type { ZigbeeDevice, ZigbeeSensor } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { Button } from '../Button';
import { Card, CardContent, CardHeader } from '../Card';
import { ConfirmModal } from '../ConfirmModal';
import { SensorCard } from './SensorCard';
import { SensorFormModal } from './SensorFormModal';
import { DeviceReportingModal } from './DeviceReportingModal';
import { SensorHistoryModal } from './SensorHistoryModal';
import { SensorThresholdsModal } from './SensorThresholdsModal';

interface Props {
  /** Set when the operator pressed "add" on a row in the coordinator card. */
  adoptDevice: ZigbeeDevice | null;
  onAdoptHandled: () => void;
}

export function SensorsSection({ adoptDevice, onAdoptHandled }: Props) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const queryClient = useQueryClient();

  const [editing, setEditing] = useState<ZigbeeSensor | null>(null);
  const [adopting, setAdopting] = useState(false);
  const [unbinding, setUnbinding] = useState<ZigbeeSensor | null>(null);
  const [configuring, setConfiguring] = useState<ZigbeeSensor | null>(null);
  const [charting, setCharting] = useState<ZigbeeSensor | null>(null);
  const [thresholding, setThresholding] = useState<ZigbeeSensor | null>(null);

  const mayRead = hasPermission('smart_sensors:read');
  const { data: status } = useQuery({ queryKey: ['zigbee-status'], queryFn: api.getZigbeeStatus });
  const { data } = useQuery({
    queryKey: ['zigbee-sensors'],
    queryFn: api.getZigbeeSensors,
    // The same cadence as plug status, and no faster than the sensors report.
    refetchInterval: 30000,
    enabled: mayRead,
  });

  const unbind = useMutation({
    mutationFn: (id: number) => api.deleteZigbeeSensor(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['zigbee-sensors'] });
      // The device becomes free to adopt again, which the paired list shows.
      queryClient.invalidateQueries({ queryKey: ['zigbee-devices'] });
      setUnbinding(null);
    },
  });

  if (!mayRead) return null;

  const sensors: ZigbeeSensor[] = data?.sensors ?? [];
  const radioUp = status?.state === 'up';

  return (
    <Card className="mb-6">
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <h3 className="text-base font-semibold text-white flex items-center gap-2">
            <Thermometer className="w-4 h-4 text-bambu-green" />
            {t('settings.zigbee.sensors.title')}
          </h3>
          {hasPermission('smart_sensors:create') && (
            <Button size="sm" onClick={() => setAdopting(true)} disabled={!radioUp}>
              {t('settings.zigbee.sensors.add')}
            </Button>
          )}
        </div>
      </CardHeader>

      <CardContent>
        {/* One banner for the whole section: the cause is one for all of them,
            and repeating it on five cards is noise. The cards still render --
            their names and places do not come from the radio. */}
        {!radioUp && (
          <p className="text-sm text-amber-600 dark:text-amber-400 mb-3">{t('settings.zigbee.sensors.radioDown')}</p>
        )}

        {sensors.length === 0 ? (
          <div className="text-sm text-bambu-gray">
            <p className="text-white">{t('settings.zigbee.sensors.empty')}</p>
            <p className="text-xs">{t('settings.zigbee.sensors.emptyHint')}</p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {sensors.map((sensor) => (
              <SensorCard
                key={sensor.id}
                sensor={sensor}
                onEdit={setEditing}
                onUnbind={setUnbinding}
                onConfigure={setConfiguring}
                onChart={setCharting}
                onThresholds={setThresholding}
                canEdit={hasPermission('smart_sensors:update')}
                canDelete={hasPermission('smart_sensors:delete')}
                // The permission the settings endpoint actually checks -- both
                // classes ride the plug one. Gating on smart_sensors:update
                // would offer an action that returns 403.
                canConfigure={hasPermission('smart_plugs:update')}
              />
            ))}
          </div>
        )}
        {(adopting || adoptDevice) && (
          <SensorFormModal
            sensor={null}
            initialDevice={adoptDevice}
            onClose={() => {
              setAdopting(false);
              onAdoptHandled();
            }}
          />
        )}
        {editing && <SensorFormModal sensor={editing} initialDevice={null} onClose={() => setEditing(null)} />}
        {charting && <SensorHistoryModal isOpen onClose={() => setCharting(null)} sensor={charting} />}
        {thresholding && (
          <SensorThresholdsModal isOpen onClose={() => setThresholding(null)} sensor={thresholding} />
        )}
        {configuring && (
          <DeviceReportingModal
            ieee={configuring.ieee}
            deviceName={configuring.name}
            onClose={() => setConfiguring(null)}
          />
        )}
        {unbinding && (
          /* The confirmation is the only place a person learns that unbinding
             is not the same as taking the device off the network. */
          <ConfirmModal
            title={t('settings.zigbee.sensors.unbindTitle', { name: unbinding.name })}
            message={t('settings.zigbee.sensors.unbindBody')}
            confirmText={t('settings.zigbee.sensors.unbindConfirm')}
            variant="danger"
            onConfirm={() => unbind.mutate(unbinding.id)}
            onCancel={() => setUnbinding(null)}
          />
        )}
      </CardContent>
    </Card>
  );
}
