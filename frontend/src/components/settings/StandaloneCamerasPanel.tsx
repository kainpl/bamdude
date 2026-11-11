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

import { api, type Camera, type CameraCreate, type CameraUpdate, type CameraSecretActions } from '../../api/client';
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
  const [edits, setEdits] = useState<Record<number, CameraCreate>>({});
  const [actions, setActions] = useState<Record<number, CameraSecretActions>>({});
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
    mutationFn: ({ id, data }: { id: number; data: CameraUpdate }) => api.updateCamera(id, data),
    onSuccess: (_data, { id }) => {
      setEdits(previous => { const next = { ...previous }; delete next[id]; return next; });
      setActions(previous => { const next = { ...previous }; delete next[id]; return next; });
      invalidate();
    },
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

  const blankEdit = (camera: Camera): CameraCreate => ({ ...camera, url: '', snapshot_url: null });
  const editCamera = (camera: Camera, next: Partial<CameraCreate>) => {
    setEdits(previous => ({ ...previous, [camera.id]: { ...(previous[camera.id] ?? blankEdit(camera)), ...next } }));
    for (const key of ['url', 'snapshot_url'] as const) {
      if (key in next) setActions(previous => ({ ...previous, [camera.id]: {
        ...previous[camera.id], [key]: next[key] ? 'replace' : 'keep',
      } }));
    }
  };
  const payloadFor = (camera: Camera): CameraUpdate => {
    const value = edits[camera.id] ?? blankEdit(camera);
    const { url, snapshot_url, ...metadata } = value;
    const payload: CameraUpdate = { ...metadata, secret_actions: {} };
    for (const key of ['url', 'snapshot_url'] as const) {
      const input = key === 'url' ? url : snapshot_url;
      const action = actions[camera.id]?.[key];
      if (action === 'clear') payload.secret_actions![key] = 'clear';
      else if (input) { payload[key] = input; payload.secret_actions![key] = 'replace'; }
      else if (camera[key === 'url' ? 'url_configured' : 'snapshot_url_configured']) payload.secret_actions![key] = 'keep';
    }
    // Read-only projection metadata never becomes part of the wire edit.
    return Object.fromEntries(Object.entries(payload).filter(([key]) =>
      ['name', 'camera_type', 'rotation', 'enabled', 'location_id', 'url', 'snapshot_url', 'secret_actions'].includes(key)));
  };
  const clearSource = (camera: Camera, key: 'url' | 'snapshot_url') => {
    editCamera(camera, { [key]: key === 'url' ? '' : null, ...(key === 'url' ? { enabled: false } : {}) });
    setActions(previous => ({ ...previous, [camera.id]: { ...previous[camera.id], [key]: 'clear' } }));
  };
  const test = async (key: number | 'draft', url: string, cameraType: string, source: 'stream' | 'snapshot' = 'stream') => {
    setTesting(key);
    try {
      let result;
      if (key === 'draft') result = await api.testCameraSource({ url, camera_type: cameraType });
      else {
        const camera = cameras?.find(item => item.id === key);
        if (!camera) return;
        const payload = payloadFor(camera);
        result = await api.testSavedCamera(key, { camera_type: payload.camera_type, url: payload.url,
          snapshot_url: payload.snapshot_url, enabled: payload.enabled, secret_actions: payload.secret_actions, source });
      }
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
    stored?: Camera,
  ) => (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        <div className="min-w-[8rem] flex-1">
          <label className={label} htmlFor={`${idPrefix}-name`}>{t('settings.otherCameras.name')}</label>
          <input
            disabled={busy}
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
            disabled={busy}
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
            disabled={busy}
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
            type="password"
            autoComplete="new-password"
            placeholder={
              stored?.url_configured ? t('settings.cameraCredentialStored') : value.camera_type === 'usb'
                ? t('settings.cameraPlaceholderUsb')
                : t('settings.cameraPlaceholderUrl')
            }
            disabled={busy}
            value={value.url}
            onChange={(e) => onChange({ url: e.target.value })}
            className={`${field} flex-1`}
          />
          <Button size="sm" variant="secondary" onClick={onTest} disabled={busy || actions[stored?.id ?? 0]?.url === 'clear' || !(value.url || stored?.url_configured)}>
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : t('settings.test')}
          </Button>
        </div>
      </div>
      {stored?.source_host && <p className="text-xs text-bambu-gray">{stored.source_host}</p>}
      {stored?.url_configured && <Button size="sm" variant="secondary" disabled={busy}
        aria-label={`${t('settings.credentials.clear')}: ${t('settings.otherCameras.url')}`}
        onClick={() => clearSource(stored, 'url')}>{t('settings.credentials.clear')}</Button>}
      {TAKES_SNAPSHOT_URL.includes(value.camera_type) && (
        <div>
          <label className={label} htmlFor={`${idPrefix}-snapshot`}>{t('settings.cameraSnapshotUrl')}</label>
          <input
            id={`${idPrefix}-snapshot`}
            type="password"
            autoComplete="new-password"
            disabled={busy}
            placeholder={stored?.snapshot_url_configured ? t('settings.cameraCredentialStored') : t('settings.cameraSnapshotUrlPlaceholder')}
            value={value.snapshot_url ?? ''}
            onChange={(e) => onChange({ snapshot_url: e.target.value || null })}
            className={`${field} w-full`}
          />
          {stored?.snapshot_url_configured && <Button size="sm" variant="secondary" disabled={busy}
            aria-label={`${t('settings.credentials.clear')}: ${t('settings.cameraSnapshotUrl')}`}
            onClick={() => clearSource(stored, 'snapshot_url')}>{t('settings.credentials.clear')}</Button>}
          {stored && <Button size="sm" variant="secondary"
            disabled={busy || actions[stored.id]?.snapshot_url === 'clear' || !(value.snapshot_url || stored.snapshot_url_configured)}
            onClick={() => test(stored.id, value.snapshot_url ?? '', 'snapshot', 'snapshot')}>{t('settings.test')}</Button>}
          <p className="text-xs text-bambu-gray opacity-75 mt-1">{t('settings.otherCameras.snapshotUrlHelp')}</p>
        </div>
      )}
      <div>
        <label className={label}>{t('settings.otherCameras.location')}</label>
        <fieldset disabled={busy}>
          <PrinterLocationSelect
            value={value.location_id}
            onChange={(location_id) => onChange({ location_id })}
            allowCreate
          />
        </fieldset>
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
                    checked={edits[camera.id]?.enabled ?? camera.enabled}
                    disabled={update.isPending}
                    onChange={(e) => editCamera(camera, { enabled: e.target.checked })}
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
              edits[camera.id] ?? blankEdit(camera),
              (next) => editCamera(camera, next),
              () => test(camera.id, '', edits[camera.id]?.camera_type ?? camera.camera_type),
              testing === camera.id || update.isPending,
              camera,
            )}
            {edits[camera.id] && <div className="flex gap-2">
              <Button size="sm" disabled={update.isPending} onClick={() => update.mutate({ id: camera.id, data: payloadFor(camera) })}>{t('common.save')}</Button>
              <Button size="sm" variant="secondary" disabled={update.isPending} onClick={() => {
                setEdits(previous => { const next = { ...previous }; delete next[camera.id]; return next; });
                setActions(previous => { const next = { ...previous }; delete next[camera.id]; return next; });
              }}>{t('common.cancel')}</Button>
            </div>}
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
