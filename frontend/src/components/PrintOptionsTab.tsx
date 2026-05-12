import { useTranslation } from 'react-i18next';

import type {
  PrinterSettingsGetResponse,
  PrinterSettingsPostBody,
} from '../api/client';

interface Props {
  data: PrinterSettingsGetResponse;
  onSubmit: (body: PrinterSettingsPostBody) => Promise<void>;
}

type BoolKey = 'auto_recovery' | 'sound' | 'filament_tangle' | 'nozzle_blob' | 'plate_type' | 'plate_align';
type IntKey = 'save_remote_to_storage' | 'purify_air' | 'open_door';
type XCamModule =
  | 'first_layer_inspector' | 'spaghetti_detector' | 'purgechutepileup_detector'
  | 'nozzleclumping_detector' | 'airprinting_detector' | 'fod_check'
  | 'displacement_detection' | 'ai_monitoring';

export function PrintOptionsTab({ data, onSubmit }: Props) {
  const { t } = useTranslation();
  const s = data.print_options;
  const sup = data.supports;

  const sensitivityOptions = [
    { v: 'low', label: t('printerSettings.sensitivity.low') },
    { v: 'medium', label: t('printerSettings.sensitivity.medium') },
    { v: 'high', label: t('printerSettings.sensitivity.high') },
  ] as const;

  const toggleBool = (key: BoolKey, next: boolean) =>
    onSubmit({ action: 'print_option_bool', key, enabled: next });

  const toggleInt = (key: IntKey, value: number) =>
    onSubmit({ action: 'print_option_int', key, value });

  const toggleXcam = (
    module: XCamModule,
    enabled: boolean,
    sensitivity: 'low' | 'medium' | 'high' | null,
  ) => onSubmit({ action: 'xcam_control', module, enabled, sensitivity });

  return (
    <div className="space-y-5">
      {(sup.spaghetti_detector || sup.pileup_detector || sup.nozzleclumping_detector || sup.airprinting_detector || sup.first_layer_inspector || sup.ai_monitoring) && (
        <Group title={t('printerSettings.aiMonitoringGroup')}>
          {sup.first_layer_inspector && (
            <XCamRow
              title={t('printerSettings.firstLayer')}
              state={s.first_layer_inspector}
              onChange={(en, sens) => toggleXcam('first_layer_inspector', en, sens)}
              sensitivityOptions={sensitivityOptions}
            />
          )}
          {sup.spaghetti_detector && (
            <XCamRow
              title={t('printerSettings.spaghetti')}
              state={s.spaghetti_detector}
              onChange={(en, sens) => toggleXcam('spaghetti_detector', en, sens)}
              sensitivityOptions={sensitivityOptions}
            />
          )}
          {sup.pileup_detector && (
            <XCamRow
              title={t('printerSettings.pileup')}
              state={s.pileup_detector}
              onChange={(en, sens) => toggleXcam('purgechutepileup_detector', en, sens)}
              sensitivityOptions={sensitivityOptions}
            />
          )}
          {sup.nozzleclumping_detector && (
            <XCamRow
              title={t('printerSettings.clumping')}
              state={s.nozzleclumping_detector}
              onChange={(en, sens) => toggleXcam('nozzleclumping_detector', en, sens)}
              sensitivityOptions={sensitivityOptions}
            />
          )}
          {sup.airprinting_detector && (
            <XCamRow
              title={t('printerSettings.airprint')}
              state={s.airprinting_detector}
              onChange={(en, sens) => toggleXcam('airprinting_detector', en, sens)}
              sensitivityOptions={sensitivityOptions}
            />
          )}
        </Group>
      )}

      {(sup.filament_tangle || sup.nozzle_blob || sup.fod_check || sup.displacement_detection) && (
        <Group title={t('printerSettings.sensorsGroup')}>
          {sup.filament_tangle && (
            <SimpleRow
              title={t('printerSettings.filamentTangle')}
              checked={!!s.filament_tangle}
              onChange={(v) => toggleBool('filament_tangle', v)}
            />
          )}
          {sup.nozzle_blob && (
            <SimpleRow
              title={t('printerSettings.nozzleBlob')}
              checked={!!s.nozzle_blob}
              onChange={(v) => toggleBool('nozzle_blob', v)}
            />
          )}
          {sup.fod_check && (
            <XCamRow
              title={t('printerSettings.fodCheck')}
              state={{ enabled: s.fod_check, sensitivity: null }}
              onChange={(en) => toggleXcam('fod_check', en, null)}
              sensitivityOptions={[]}
            />
          )}
          {sup.displacement_detection && (
            <XCamRow
              title={t('printerSettings.displacement')}
              state={{ enabled: s.displacement_detection, sensitivity: null }}
              onChange={(en) => toggleXcam('displacement_detection', en, null)}
              sensitivityOptions={[]}
            />
          )}
        </Group>
      )}

      {(sup.open_door_check || sup.purify_air) && (
        <Group title={t('printerSettings.doorAirGroup')}>
          {sup.open_door_check && (
            <SegmentedRow
              title={t('printerSettings.openDoorCheck')}
              value={s.open_door ?? 0}
              options={[
                { v: 0, label: t('printerSettings.openDoorMode.off') },
                { v: 1, label: t('printerSettings.openDoorMode.pause') },
                { v: 2, label: t('printerSettings.openDoorMode.halt') },
              ]}
              onChange={(v) => toggleInt('open_door', v)}
            />
          )}
          {sup.purify_air && (
            <SegmentedRow
              title={t('printerSettings.purifyAirEnd')}
              value={s.purify_air ?? 0}
              options={[
                { v: 0, label: t('printerSettings.purifyAirMode.off') },
                { v: 1, label: t('printerSettings.purifyAirMode.inside') },
                { v: 2, label: t('printerSettings.purifyAirMode.outside') },
              ]}
              onChange={(v) => toggleInt('purify_air', v)}
            />
          )}
        </Group>
      )}

      <Group title={t('printerSettings.behaviourGroup')}>
        {sup.auto_recovery && (
          <SimpleRow
            title={t('printerSettings.autoRecovery')}
            checked={!!s.auto_recovery}
            onChange={(v) => toggleBool('auto_recovery', v)}
          />
        )}
        {sup.sound && (
          <SimpleRow
            title={t('printerSettings.sound')}
            checked={!!s.sound}
            onChange={(v) => toggleBool('sound', v)}
          />
        )}
        {sup.save_remote_to_storage && (
          <SimpleRow
            title={t('printerSettings.saveRemoteToStorage')}
            checked={(s.save_remote_to_storage ?? 0) > 0}
            onChange={(v) => toggleInt('save_remote_to_storage', v ? 1 : 0)}
          />
        )}
        {sup.snapshot && (
          <SimpleRow
            title={t('printerSettings.snapshot')}
            checked={!!s.snapshot}
            onChange={(v) => onSubmit({ action: 'camera_snapshot', enabled: v })}
          />
        )}
      </Group>

      {(sup.plate_type || sup.plate_align) && (
        <Group title={t('printerSettings.buildPlateGroup')}>
          {sup.plate_type && (
            <SimpleRow
              title={t('printerSettings.plateType')}
              checked={!!s.plate_type}
              onChange={(v) => toggleBool('plate_type', v)}
            />
          )}
          {sup.plate_align && (
            <SimpleRow
              title={t('printerSettings.plateAlign')}
              checked={!!s.plate_align}
              onChange={(v) => toggleBool('plate_align', v)}
            />
          )}
        </Group>
      )}
    </div>
  );
}

function Group({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="border-t border-bambu-dark-tertiary pt-3 first:border-t-0 first:pt-0">
      <div className="text-xs uppercase tracking-wider text-bambu-gray mb-2">{title}</div>
      <div className="space-y-2">{children}</div>
    </div>
  );
}

function SimpleRow({ title, checked, onChange }: { title: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="flex items-center gap-3 cursor-pointer">
      <input
        type="checkbox"
        className="h-4 w-4 accent-bambu-green"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        aria-label={title}
      />
      <span className="text-white">{title}</span>
    </label>
  );
}

function XCamRow({
  title,
  state,
  onChange,
  sensitivityOptions,
}: {
  title: string;
  state: { enabled: boolean | null; sensitivity: string | null };
  onChange: (enabled: boolean, sensitivity: 'low' | 'medium' | 'high' | null) => void;
  sensitivityOptions: readonly { v: string; label: string }[];
}) {
  const enabled = !!state.enabled;
  const sens = (state.sensitivity as 'low' | 'medium' | 'high' | null) ?? 'medium';
  return (
    <label className="flex items-center gap-3 cursor-pointer">
      <input
        type="checkbox"
        className="h-4 w-4 accent-bambu-green"
        checked={enabled}
        onChange={(e) => onChange(e.target.checked, sensitivityOptions.length ? sens : null)}
        aria-label={title}
      />
      <span className="text-white flex-1">{title}</span>
      {sensitivityOptions.length > 0 && (
        <select
          className="bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1 text-white text-sm"
          value={sens}
          disabled={!enabled}
          onChange={(e) => onChange(enabled, e.target.value as 'low' | 'medium' | 'high')}
        >
          {sensitivityOptions.map((o) => (
            <option key={o.v} value={o.v}>{o.label}</option>
          ))}
        </select>
      )}
    </label>
  );
}

function SegmentedRow({
  title,
  value,
  options,
  onChange,
}: {
  title: string;
  value: number;
  options: { v: number; label: string }[];
  onChange: (v: number) => void;
}) {
  return (
    <div>
      <div className="text-white text-sm mb-1">{title}</div>
      <div className="inline-flex gap-1 rounded-lg p-1 bg-bambu-dark">
        {options.map((o) => (
          <button
            key={o.v}
            type="button"
            className={`px-3 py-1 text-sm rounded-md transition-colors ${
              value === o.v ? 'bg-bambu-green text-white' : 'text-bambu-gray hover:text-white'
            }`}
            onClick={() => onChange(o.v)}
          >
            {o.label}
          </button>
        ))}
      </div>
    </div>
  );
}
