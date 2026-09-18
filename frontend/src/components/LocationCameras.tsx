import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Video } from 'lucide-react';

import { api } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { openCameraWindow, standaloneSource } from '../utils/cameraSource';

interface Props {
  /** The group's location. Null for the "no location" group, which shows nothing. */
  locationId: number | null;
  /** Open the floating window instead of a browser one — the farm's camera_view_mode. */
  onOpenEmbedded?: (cameraId: number, cameraName: string) => void;
}

/**
 * The cameras watching the place this group of printers stands in.
 *
 * The counterpart of `LocationConditions` next to it, and deliberately the same
 * shape: it fetches for itself (TanStack collapses every header on the page
 * into one request), renders nothing for the "no location" group, nothing when
 * the place has no camera, and nothing without the permission. A group header
 * must never break — a camera that will not open is diagnosed in Settings, not
 * by a red line across the farm's main screen.
 *
 * ⚠️ Only cameras that are switched ON get a button. A disabled camera is one
 * the operator retired, and its routes refuse anyway.
 */
export function LocationCameras({ locationId, onOpenEmbedded }: Props) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();

  const enabled = hasPermission('camera:view') && locationId != null;

  const { data: cameras } = useQuery({
    queryKey: ['cameras'],
    queryFn: api.getCameras,
    enabled,
  });

  const here = (cameras ?? []).filter((camera) => camera.enabled && camera.location_id === locationId);
  if (!enabled || here.length === 0) return null;

  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      {here.map((camera) => (
        <button
          key={camera.id}
          type="button"
          onClick={() => {
            if (onOpenEmbedded) onOpenEmbedded(camera.id, camera.name);
            else openCameraWindow(standaloneSource(camera.id));
          }}
          title={t('printers.openCameraWindow')}
          className="inline-flex items-center gap-1 rounded-md border border-bambu-dark-tertiary bg-bambu-dark px-2 py-0.5 text-xs text-bambu-gray-light transition-colors hover:bg-bambu-dark-tertiary hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green"
        >
          <Video className="h-3.5 w-3.5" aria-hidden="true" />
          <span className="max-w-[10rem] truncate">{camera.name}</span>
        </button>
      ))}
    </span>
  );
}
