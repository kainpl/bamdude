import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ShieldAlert } from 'lucide-react';
import { api } from '../api/client';
import { useAuth } from '../contexts/AuthContext';

/**
 * N62: moving a printer into or out of a location a limited group is given changes which
 * printers that group reaches. The server lets only an administrator do it and names the
 * groups when it refuses; an administrator sees them here before saving.
 */
export function LocationAccessNote({ from, to }: { from: number | null; to: number | null }) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const moved = from !== to;
  const { data: groups } = useQuery({ queryKey: ['groups'], queryFn: api.getGroups, enabled: isAdmin && moved });
  const { data: locationData } = useQuery({
    queryKey: ['printer-locations'],
    queryFn: api.getPrinterLocations,
    enabled: isAdmin && moved,
  });

  const affected = useMemo(() => {
    if (!moved || !groups || !locationData) return [];
    const parent = new Map(locationData.locations.map((loc) => [loc.id, loc.parent_id]));
    const chain = new Set<number>();
    for (const start of [from, to]) {
      let loc = start;
      while (loc != null && !chain.has(loc)) {
        chain.add(loc);
        loc = parent.get(loc) ?? null;
      }
    }
    return groups
      .filter((g) => g.restrict_printers && g.location_ids.some((id) => chain.has(id)))
      .map((g) => g.name)
      .sort();
  }, [moved, groups, locationData, from, to]);

  if (!isAdmin || affected.length === 0) return null;
  return (
    <p className="text-xs text-amber-700 dark:text-amber-400 mt-1 flex items-start gap-1" role="status">
      <ShieldAlert className="w-3.5 h-3.5 mt-0.5 shrink-0" />
      {t('printers.accessChange', { groups: affected.join(', ') })}
    </p>
  );
}
