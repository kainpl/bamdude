import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import { api } from '../../api/client';
import type { ZigbeeSensor } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { sensorsForPrinter } from '../../utils/sensorReadings';
import { SensorChip } from './SensorChip';
import { SensorHistoryModal } from './SensorHistoryModal';

interface Props {
  printerId: number;
}

/**
 * What this printer's own sensors say.
 *
 * The counterpart of `LocationConditions`, and deliberately the same shape: one
 * fetch of the same key that TanStack collapses across every card on the page,
 * the same chip, the same chart on click. The only difference is which question
 * decides membership — the sensor's binding, which the operator sets once in
 * the sensor's settings.
 *
 * Renders nothing whenever there is nothing honest to render: no permission, no
 * bound sensors, a failed query. A printer card must never break over a
 * thermometer.
 */
export function PrinterConditions({ printerId }: Props) {
  const { hasPermission } = useAuth();
  const [charting, setCharting] = useState<ZigbeeSensor | null>(null);

  const mayRead = hasPermission('smart_sensors:read');

  const { data } = useQuery({
    queryKey: ['zigbee-sensors'],
    queryFn: api.getZigbeeSensors,
    refetchInterval: 30000,
    enabled: mayRead,
  });

  const sensors = useMemo(() => sensorsForPrinter(data?.sensors ?? [], printerId), [data, printerId]);

  if (!mayRead || sensors.length === 0) return null;

  return (
    <>
      <div className="flex items-center gap-2 flex-wrap pt-2 mt-2 border-t border-bambu-dark-tertiary">
        {sensors.map((sensor) => (
          <SensorChip key={sensor.id} sensor={sensor} onOpen={() => setCharting(sensor)} />
        ))}
      </div>

      {charting && <SensorHistoryModal isOpen onClose={() => setCharting(null)} sensor={charting} />}
    </>
  );
}
