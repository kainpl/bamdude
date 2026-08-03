import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Thermometer } from 'lucide-react';

import { api } from '../../api/client';
import type { ZigbeeDevice, ZigbeeSensor } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { Button } from '../Button';
import { Card, CardContent, CardHeader } from '../Card';
import { SensorCard } from './SensorCard';

interface Props {
  /** Set when the operator pressed "add" on a row in the coordinator card. */
  adoptDevice: ZigbeeDevice | null;
  onAdoptHandled: () => void;
}

export function SensorsSection({ adoptDevice, onAdoptHandled }: Props) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();

  const mayRead = hasPermission('smart_sensors:read');
  const { data: status } = useQuery({ queryKey: ['zigbee-status'], queryFn: api.getZigbeeStatus });
  const { data } = useQuery({
    queryKey: ['zigbee-sensors'],
    queryFn: api.getZigbeeSensors,
    // The same cadence as plug status, and no faster than the sensors report.
    refetchInterval: 30000,
    enabled: mayRead,
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
            <Button size="sm" onClick={onAdoptHandled} disabled={!radioUp}>
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
                onEdit={() => {}}
                onUnbind={() => {}}
                canEdit={hasPermission('smart_sensors:update')}
                canDelete={hasPermission('smart_sensors:delete')}
              />
            ))}
          </div>
        )}
        {/* adoptDevice becomes the modal's initial device in the next task. */}
        {adoptDevice ? null : null}
      </CardContent>
    </Card>
  );
}
