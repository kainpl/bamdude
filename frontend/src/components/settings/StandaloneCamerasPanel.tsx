/**
 * Cameras that belong to no printer — a room, a shelf, a dryer.
 *
 * Drop-in panel for the Camera card in Settings → Printing, below the printers'
 * external cameras. The two look alike and are not the same thing: a printer's
 * external camera REPLACES that printer's own camera and feeds its finish
 * photo, plate check and Obico, while a camera here is only ever shown — on the
 * wall, on a button in its location's header, in a window.
 */
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Loader2, Plus, Trash2, Video } from 'lucide-react';

import { api, type Camera, type CameraCreate } from '../../api/client';
import { Button } from '../Button';
import { ConfirmModal } from '../ConfirmModal';
import { PrinterLocationSelect } from '../PrinterLocationSelect';
import { useToast } from '../../contexts/ToastContext';
import { Select } from '../Select';

/** Value plus the label key the printers' external camera select already uses:
 *  the same four sources, so they must read the same in both lists. */
const TYPES: { value: Camera['camera_type']; labelKey: string }[] = [
  { value: 'mjpeg', labelKey: 'settings.cameraTypeMjpeg' },
  { value: 'rtsp', labelKey: 'settings.cameraTypeRtsp' },
  { value: 'snapshot', labelKey: 'settings.cameraTypeSnapshot' },
  { value: 'usb', labelKey: 'settings.cameraTypeUsb' },
];
/** A snapshot source is already a single frame, so an override would be redundant. */
const TAKES_SNAPSHOT_URL: Camera['camera_type'][] = ['mjpeg', 'rtsp', 'usb'];

const BLANK: CameraCreate = {
  name: '',
  camera_type: 'mjpeg',
  url: '',
  snapshot_url: null,
  rotation: 0,
  enabled: true,
  location_id: null,
};

export function StandaloneCamerasPanel() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const [draft, setDraft] = useState<CameraCreate | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<Camera | null>(null);
  const [testing, setTesting] = useState<number | 'draft' | null>(null);

  const { data: cameras } = useQuery({ queryKey: ['cameras'], queryFn: api.getCameras });
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['cameras'] });

  const create = useMutation({
    mutationFn: (data: CameraCreate) => api.createCamera(data),
    onSuccess: () => {
      invalidate();
      setDraft(null);
      showToast(t('settings.otherCameras.added'), 'success');
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const update = useMutation({
    mutationFn: ({ id, data }: { id: number; data: Partial<CameraCreate> }) => api.updateCamera(id, data),
    onSuccess: invalidate,
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const remove = useMutation({
    mutationFn: (id: number) => api.deleteCamera(id),
    onSuccess: () => {
      invalidate();
      setConfirmDelete(null);
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const test = async (key: number | 'draft', url: string, cameraType: string) => {
    setTesting(key);
    try {
      const result = await api.testCameraSource({ url, camera_type: cameraType });
      showToast(
        result.success ? t('settings.otherCameras.testOk') : result.error || t('settings.otherCameras.testFailed'),
        result.success ? 'success' : 'error',
      );
    } catch (error) {
      showToast(error instanceof Error ? error.message : t('settings.otherCameras.testFailed'), 'error');
    } finally {
      setTesting(null);
    }
  };

  const field =
    'px-3 py-2 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded text-white text-sm focus:border-bambu-green focus:outline-none';
  const label = 'block text-xs text-bambu-gray mb-1';

  /** ``idPrefix`` keeps every label bound to its own row's input: the same
   *  fields are drawn once per saved camera plus once for the draft, and a
   *  shared id would point every label at the first row. */
  const rowFields = (
    idPrefix: string,
    value: CameraCreate,
    onChange: (next: Partial<CameraCreate>) => void,
    onTest: () => void,
    busy: boolean,
  ) => (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        <div className="min-w-[8rem] flex-1">
          <label className={label} htmlFor={`${idPrefix}-name`}>{t('settings.otherCameras.name')}</label>
          <input
            id={`${idPrefix}-name`}
            type="text"
            placeholder={t('settings.otherCameras.namePlaceholder')}
            value={value.name}
            onChange={(e) => onChange({ name: e.target.value })}
            className={`${field} w-full`}
          />
        </div>
        <div>
          <label className={label} htmlFor={`${idPrefix}-type`}>{t('settings.otherCameras.type')}</label>
          <Select
            tone="raised"
            id={`${idPrefix}-type`}
            value={value.camera_type}
            onChange={(e) => onChange({ camera_type: e.target.value as Camera['camera_type'] })}
          >
            {TYPES.map((type) => (
              <option key={type.value} value={type.value}>{t(type.labelKey)}</option>
            ))}
          </Select>
        </div>
        <div>
          <label className={label} htmlFor={`${idPrefix}-rotation`}>{t('settings.cameraRotation')}</label>
          <Select
            tone="raised"
            id={`${idPrefix}-rotation`}
            value={value.rotation}
            onChange={(e) => onChange({ rotation: parseInt(e.target.value, 10) })}
          >
            {[0, 90, 180, 270].map((deg) => (
              <option key={deg} value={deg}>{deg}°</option>
            ))}
          </Select>
        </div>
      </div>
      <div>
        <label className={label} htmlFor={`${idPrefix}-url`}>
          {value.camera_type === 'usb' ? t('settings.otherCameras.device') : t('settings.otherCameras.url')}
        </label>
        <div className="flex gap-2">
          <input
            id={`${idPrefix}-url`}
            type="text"
            placeholder={
              value.camera_type === 'usb'
                ? t('settings.cameraPlaceholderUsb')
                : t('settings.cameraPlaceholderUrl')
            }
            value={value.url}
            onChange={(e) => onChange({ url: e.target.value })}
            className={`${field} flex-1`}
          />
          <Button size="sm" variant="secondary" onClick={onTest} disabled={busy || !value.url}>
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : t('settings.test')}
          </Button>
        </div>
      </div>
      {TAKES_SNAPSHOT_URL.includes(value.camera_type) && (
        <div>
          <label className={label} htmlFor={`${idPrefix}-snapshot`}>{t('settings.cameraSnapshotUrl')}</label>
          <input
            id={`${idPrefix}-snapshot`}
            type="text"
            placeholder={t('settings.cameraSnapshotUrlPlaceholder')}
            value={value.snapshot_url ?? ''}
            onChange={(e) => onChange({ snapshot_url: e.target.value || null })}
            className={`${field} w-full`}
          />
          <p className="text-xs text-bambu-gray opacity-75 mt-1">{t('settings.otherCameras.snapshotUrlHelp')}</p>
        </div>
      )}
      <div>
        <label className={label}>{t('settings.otherCameras.location')}</label>
        <PrinterLocationSelect
          value={value.location_id}
          onChange={(location_id) => onChange({ location_id })}
          allowCreate
        />
      </div>
    </div>
  );

  return (
    <div className="border-t border-bambu-dark-tertiary pt-4 mt-4">
      <h3 className="text-sm font-medium text-white mb-2 flex items-center gap-2">
        <Video className="w-4 h-4 text-bambu-green" />
        {t('settings.otherCameras.title')}
      </h3>
      <p className="text-xs text-bambu-gray mb-3">{t('settings.otherCameras.description')}</p>

      <div className="space-y-3">
        {(cameras ?? []).map((camera) => (
          <div key={camera.id} className="p-3 bg-bambu-dark rounded-lg space-y-2">
            <div className="flex items-center justify-between gap-2">
              <span className="text-white font-medium text-sm truncate">{camera.name}</span>
              <div className="flex items-center gap-2">
                <label className="relative inline-flex items-center cursor-pointer" title={t('settings.otherCameras.enabled')}>
                  <input
                    type="checkbox"
                    aria-label={t('settings.otherCameras.enabled')}
                    checked={camera.enabled}
                    onChange={(e) => update.mutate({ id: camera.id, data: { enabled: e.target.checked } })}
                    className="sr-only peer"
                  />
                  <div className="w-9 h-5 bg-bambu-dark-tertiary peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-bambu-green"></div>
                </label>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => setConfirmDelete(camera)}
                  title={t('common.delete')}
                  aria-label={t('settings.otherCameras.deleteAria', { name: camera.name })}
                >
                  <Trash2 className="w-4 h-4" />
                </Button>
              </div>
            </div>
            {rowFields(
              `camera-${camera.id}`,
              camera,
              (next) => update.mutate({ id: camera.id, data: next }),
              () => test(camera.id, camera.url, camera.camera_type),
              testing === camera.id,
            )}
          </div>
        ))}

        {draft ? (
          <div className="p-3 bg-bambu-dark rounded-lg space-y-2 border border-bambu-green/40">
            {rowFields(
              'camera-draft',
              draft,
              (next) => setDraft({ ...draft, ...next }),
              () => test('draft', draft.url, draft.camera_type),
              testing === 'draft',
            )}
            <div className="flex gap-2">
              <Button size="sm" onClick={() => create.mutate(draft)} disabled={create.isPending || !draft.name || !draft.url}>
                {create.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : t('common.save')}
              </Button>
              <Button size="sm" variant="secondary" onClick={() => setDraft(null)}>
                {t('common.cancel')}
              </Button>
            </div>
          </div>
        ) : (
          <Button size="sm" variant="secondary" onClick={() => setDraft({ ...BLANK })}>
            <Plus className="w-4 h-4 mr-1" />
            {t('settings.otherCameras.add')}
          </Button>
        )}
      </div>

      {confirmDelete && (
        <ConfirmModal
          title={t('settings.otherCameras.deleteTitle')}
          message={t('settings.otherCameras.deleteMessage', { name: confirmDelete.name })}
          confirmText={t('common.delete')}
          variant="danger"
          isLoading={remove.isPending}
          onConfirm={() => remove.mutate(confirmDelete.id)}
          onCancel={() => setConfirmDelete(null)}
        />
      )}
    </div>
  );
}
