import { useEffect, useRef, useState } from 'react';
import { NumberInput } from './NumberInput';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Battery, Droplets, RotateCcw, Save, Settings2, Thermometer } from 'lucide-react';
import { api } from '../api/client';
import type { AppSettingsUpdate } from '../api/client';
import { Button } from './Button';
import { Modal } from './Modal';
import { Select } from './Select';
import { ConfirmModal } from './ConfirmModal';
import { useToast } from '../contexts/ToastContext';
import { useAuth } from '../contexts/AuthContext';
import { invalidateSensorViews } from '../utils/sensorQueryInvalidation';
import {
  LOCATION_SENSOR_ALERT_COLORS,
  loadLocationSensorAlertAboveColor,
  loadLocationSensorAlertBelowColor,
  loadLocationSensorAlertOptimalColor,
  loadLocationSensorColorizeValues,
  loadLocationSensorDefaults,
  saveLocationSensorAlertAboveColor,
  saveLocationSensorAlertBelowColor,
  saveLocationSensorAlertOptimalColor,
  saveLocationSensorColorizeValues,
  saveLocationSensorShowOnCardDefaults,
  serializeLocationSensorAlertDefaults,
  type LocationSensorAlertColor,
  type LocationSensorCategory,
  type LocationSensorCategoryDefaults,
  type LocationSensorDefaults,
} from '../utils/locationSensorDefaults';

interface Props {
  onClose: () => void;
}

const MIN_POLL_INTERVAL = 60;
const DEFAULT_POLL_INTERVAL = 120;

const CATEGORY_ICONS: Record<LocationSensorCategory, typeof Thermometer> = {
  temperature: Thermometer,
  humidity: Droplets,
  battery: Battery,
};

const CATEGORY_UNITS: Record<LocationSensorCategory, string> = {
  temperature: '°C',
  humidity: '%',
  battery: '%',
};

// Keep in step with categoryFor in LocationHASensorModal — "moisture" is
// binary wet/dry, not a humidity percentage, and is not a location category.
function categoryFor(deviceClass: string | null): LocationSensorCategory | null {
  if (deviceClass === 'temperature') return 'temperature';
  if (deviceClass === 'humidity') return 'humidity';
  if (deviceClass === 'battery') return 'battery';
  return null;
}

function CategorySection({
  category,
  state,
  onChange,
}: {
  category: LocationSensorCategory;
  state: LocationSensorCategoryDefaults;
  onChange: (patch: Partial<LocationSensorCategoryDefaults>) => void;
}) {
  const { t } = useTranslation();
  const Icon = CATEGORY_ICONS[category];
  const unit = CATEGORY_UNITS[category];
  const hasAlertCondition = state.alertAbove !== '' || state.alertBelow !== '';

  return (
    <div className="p-3 border border-bambu-dark-tertiary rounded-lg space-y-3">
      <div className="flex items-center gap-1.5 text-sm text-white font-medium">
        <Icon className="w-4 h-4" />
        {t(`inventory.${category}`)}
      </div>

      {category === 'battery' ? (
        <div>
          <span className="block text-xs text-bambu-gray mb-1">
            {t('haSensors.alertBelow')} {unit}
          </span>
          <input
            type="number"
            step="any"
            value={state.alertBelow}
            onChange={(e) => onChange({ alertBelow: e.target.value })}
            className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white"
          />
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-3">
          <div>
            <span className="block text-xs text-bambu-gray mb-1">
              {t('haSensors.alertAbove')} {unit}
            </span>
            <input
              type="number"
              step="any"
              value={state.alertAbove}
              onChange={(e) => onChange({ alertAbove: e.target.value })}
              className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white"
            />
          </div>
          <div>
            <span className="block text-xs text-bambu-gray mb-1">
              {t('haSensors.alertBelow')} {unit}
            </span>
            <input
              type="number"
              step="any"
              value={state.alertBelow}
              onChange={(e) => onChange({ alertBelow: e.target.value })}
              className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white"
            />
          </div>
        </div>
      )}

      <label className="flex items-center gap-3 cursor-pointer">
        <input
          type="checkbox"
          checked={state.showOnCard}
          onChange={(e) => onChange({ showOnCard: e.target.checked })}
          className="w-4 h-4"
        />
        <span className="text-sm text-white">{t('locationHaSensors.showOnCard')}</span>
      </label>

      <label className="flex items-center gap-3 cursor-pointer">
        <input
          type="checkbox"
          checked={state.notifyOnAlert}
          onChange={(e) => onChange({ notifyOnAlert: e.target.checked })}
          disabled={!hasAlertCondition}
          className="w-4 h-4"
        />
        <span className={`text-sm ${hasAlertCondition ? 'text-white' : 'text-bambu-gray'}`}>
          {t('haSensors.notifyOnAlert')}
        </span>
      </label>
    </div>
  );
}

export function LocationSensorOptionsModal({ onClose }: Props) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const { hasPermission } = useAuth();
  const canManageServer = hasPermission('settings:update');
  const canResetBindings = canManageServer && hasPermission('smart_sensors:update');
  const queryClient = useQueryClient();
  // Built-ins first, then seeded from the server once the settings query
  // lands. The alert fields come from `location_sensor_alert_defaults`;
  // show-on-card is still local.
  const [defaults, setDefaults] = useState<LocationSensorDefaults>(() => loadLocationSensorDefaults());
  const [colorizeValues, setColorizeValues] = useState(() => loadLocationSensorColorizeValues());
  const [aboveColor, setAboveColor] = useState<LocationSensorAlertColor>(() => loadLocationSensorAlertAboveColor());
  const [belowColor, setBelowColor] = useState<LocationSensorAlertColor>(() => loadLocationSensorAlertBelowColor());
  const [optimalColor, setOptimalColor] = useState<LocationSensorAlertColor>(() => loadLocationSensorAlertOptimalColor());
  const [showResetConfirm, setShowResetConfirm] = useState(false);

  const { data: appSettings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings });
  const [pollInterval, setPollInterval] = useState(DEFAULT_POLL_INTERVAL);

  // Seed the server-backed fields once, and never over an edit in progress.
  // The dialog renders immediately with placeholder values, so a settings
  // response landing mid-keystroke would otherwise put the old value back in
  // front of what was just typed (a field cleared and retyped came out as
  // "3035"). In practice ['settings'] is warm — SettingsPage, which opens this
  // dialog, already holds it — so this only covers the cold path and a
  // background refetch.
  //
  // "Seeded" and "touched" are tracked apart on purpose. One flag for both
  // means a keystroke that beats the response cancels the seed outright, and
  // Save then writes built-in defaults over the server values for every field
  // the user never saw. Seeding therefore always happens; it just skips the
  // fields already edited, which are named here rather than counted.
  const seeded = useRef(false);
  const touched = useRef(new Set<LocationSensorCategory | 'pollInterval'>());
  useEffect(() => {
    if (!appSettings || seeded.current) return;
    seeded.current = true;
    if (!touched.current.has('pollInterval')) setPollInterval(appSettings.location_sensor_poll_interval);
    const fromServer = loadLocationSensorDefaults(appSettings.location_sensor_alert_defaults);
    setDefaults((prev) => {
      const next = { ...fromServer };
      (Object.keys(next) as LocationSensorCategory[]).forEach((category) => {
        if (touched.current.has(category)) next[category] = prev[category];
      });
      return next;
    });
  }, [appSettings]);

  const updateCategory = (category: LocationSensorCategory, patch: Partial<LocationSensorCategoryDefaults>) => {
    touched.current.add(category);
    setDefaults((prev) => ({ ...prev, [category]: { ...prev[category], ...patch } }));
  };

  const updatePollInterval = (value: number) => {
    touched.current.add('pollInterval');
    setPollInterval(value);
  };

  const persistDefaults = async () => {
    // Server call first, and only for fields that actually changed (these
    // need SETTINGS_UPDATE/admin), before writing anything to localStorage.
    // A failed PATCH must leave the local preferences below untouched, so the
    // error toast that follows is true — nothing was saved, not "half of it
    // was".
    const clampedInterval = Math.max(MIN_POLL_INTERVAL, pollInterval);
    const alertDefaults = serializeLocationSensorAlertDefaults({
      ...defaults,
      battery: { ...defaults.battery, alertAbove: '' },
    });
    const patch: AppSettingsUpdate = {};
    if (appSettings && clampedInterval !== appSettings.location_sensor_poll_interval) {
      patch.location_sensor_poll_interval = clampedInterval;
    }
    if (appSettings && alertDefaults !== appSettings.location_sensor_alert_defaults) {
      patch.location_sensor_alert_defaults = alertDefaults;
    }
    if (canManageServer && Object.keys(patch).length > 0) {
      await api.updateSettings(patch);
      queryClient.invalidateQueries({ queryKey: ['settings'] });
    }

    saveLocationSensorShowOnCardDefaults(defaults);
    saveLocationSensorColorizeValues(colorizeValues);
    saveLocationSensorAlertAboveColor(aboveColor);
    saveLocationSensorAlertBelowColor(belowColor);
    saveLocationSensorAlertOptimalColor(optimalColor);
  };

  const saveMutation = useMutation({
    mutationFn: persistDefaults,
    onSuccess: () => {
      showToast(t('locationHaSensors.options.saved'), 'success');
      onClose();
    },
    onError: (err: Error) => {
      showToast(err.message || t('locationHaSensors.options.saveFailed'), 'error');
    },
  });

  const handleSave = (e: React.FormEvent) => {
    e.preventDefault();
    saveMutation.mutate();
  };

  const resetMutation = useMutation({
    mutationFn: async () => {
      // Fetch current rows before each attempt so a retry writes only rows
      // that still differ. The per-sensor writes are not atomic as a group.
      const [sensors, entities] = await Promise.all([api.getLocationHASensors(), api.getBindableLocationHAEntities()]);
      const friendlyNameByEntityId = new Map(entities.map((entity) => [entity.entity_id, entity.friendly_name]));
      const targets = sensors.filter((sensor) => categoryFor(sensor.device_class) !== null);
      const pending = targets.flatMap((sensor) => {
          const category = categoryFor(sensor.device_class)!;
          const categoryDefaults = defaults[category];
          const friendlyName = friendlyNameByEntityId.get(sensor.entity_id);
          const patch = {
            ...(friendlyName ? { name: friendlyName.slice(0, 100) } : {}),
            alert_above:
              category !== 'battery' && categoryDefaults.alertAbove !== '' ? Number(categoryDefaults.alertAbove) : null,
            alert_below: categoryDefaults.alertBelow !== '' ? Number(categoryDefaults.alertBelow) : null,
            notify_on_alert: categoryDefaults.notifyOnAlert,
            show_on_card: categoryDefaults.showOnCard,
          };
          return Object.entries(patch).every(([key, value]) =>
            sensor[key as keyof typeof sensor] === value) ? [] : [{ sensor, patch }];
        });
      const results = await Promise.allSettled(pending.map(({ sensor, patch }) =>
        api.updateLocationHASensor(sensor.id, patch)));
      const failed = results.flatMap((result, index) => result.status === 'rejected'
        ? [pending[index].sensor.entity_id] : []);
      if (failed.length) throw new Error(t('sensorSettings.partialReset', {
        count: results.length - failed.length, failed: failed.join(', '),
      }));
      try {
        await persistDefaults();
      } catch {
        throw new Error(t('sensorSettings.partialResetDefaults', { count: pending.length }));
      }
      return pending.length;
    },
    onSuccess: (count) => {
      invalidateSensorViews(queryClient);
      showToast(t('locationHaSensors.options.resetDone', { count }), 'success');
      setShowResetConfirm(false);
      onClose();
    },
    onError: (err: Error) => {
      invalidateSensorViews(queryClient);
      showToast(err.message || t('locationHaSensors.options.resetFailed'), 'error');
      setShowResetConfirm(false);
    },
  });

  return (
    <>
    <Modal
      onClose={onClose}
      size="lg"
      icon={
        <div className="p-2 rounded-full bg-bambu-green/20 text-bambu-green">
          <Settings2 className="w-5 h-5" />
        </div>
      }
      title={t('locationHaSensors.options.title')}
    >
      <form onSubmit={handleSave} className="px-6 pb-6 pt-3 space-y-4">
          <p className="text-xs text-bambu-gray">{t('locationHaSensors.options.description')}</p>

          {canManageServer ? <div className="space-y-3">
            <CategorySection
              category="temperature"
              state={defaults.temperature}
              onChange={(patch) => updateCategory('temperature', patch)}
            />
            <CategorySection
              category="humidity"
              state={defaults.humidity}
              onChange={(patch) => updateCategory('humidity', patch)}
            />
            <CategorySection
              category="battery"
              state={defaults.battery}
              onChange={(patch) => updateCategory('battery', patch)}
            />
          </div> : <div className="space-y-2 rounded-lg border border-bambu-dark-tertiary p-3">
            {(['temperature', 'humidity', 'battery'] as const).map(category =>
              <label key={category} className="flex items-center gap-2 text-sm text-white">
                <input type="checkbox" checked={defaults[category].showOnCard}
                  onChange={event => updateCategory(category, { showOnCard: event.target.checked })} />
                {t(`inventory.${category}`)} · {t('locationHaSensors.showOnCard')}
              </label>)}
          </div>}

          <div className="p-3 border border-bambu-dark-tertiary rounded-lg space-y-3">
            <label className="flex items-center gap-3 cursor-pointer">
              <input
                type="checkbox"
                checked={colorizeValues}
                onChange={(e) => setColorizeValues(e.target.checked)}
                className="w-4 h-4"
              />
              <span className="text-sm text-white">{t('locationHaSensors.options.colorizeValues')}</span>
            </label>

            <div className={`grid grid-cols-3 gap-x-3 gap-y-1 items-end ${colorizeValues ? '' : 'opacity-50'}`}>
              <label className="block text-xs text-bambu-gray" htmlFor="location-sensor-below-color">
                {t('locationHaSensors.options.belowColor')}
              </label>
              <label className="block text-xs text-bambu-gray" htmlFor="location-sensor-optimal-color">
                {t('locationHaSensors.options.optimalColor')}
              </label>
              <label className="block text-xs text-bambu-gray" htmlFor="location-sensor-above-color">
                {t('locationHaSensors.options.aboveColor')}
              </label>
              <Select
                id="location-sensor-below-color"
                value={belowColor}
                onChange={(e) => setBelowColor(e.target.value as LocationSensorAlertColor)}
                disabled={!colorizeValues}
                className="w-full"
              >
                {LOCATION_SENSOR_ALERT_COLORS.map((color) => (
                  <option key={color} value={color}>
                    {t(`locationHaSensors.options.colors.${color}`)}
                  </option>
                ))}
              </Select>
              <Select
                id="location-sensor-optimal-color"
                value={optimalColor}
                onChange={(e) => setOptimalColor(e.target.value as LocationSensorAlertColor)}
                disabled={!colorizeValues}
                className="w-full"
              >
                {LOCATION_SENSOR_ALERT_COLORS.map((color) => (
                  <option key={color} value={color}>
                    {t(`locationHaSensors.options.colors.${color}`)}
                  </option>
                ))}
              </Select>
              <Select
                id="location-sensor-above-color"
                value={aboveColor}
                onChange={(e) => setAboveColor(e.target.value as LocationSensorAlertColor)}
                disabled={!colorizeValues}
                className="w-full"
              >
                {LOCATION_SENSOR_ALERT_COLORS.map((color) => (
                  <option key={color} value={color}>
                    {t(`locationHaSensors.options.colors.${color}`)}
                  </option>
                ))}
              </Select>
            </div>
          </div>

          {canManageServer && <><div className="pt-4 mt-4 border-t border-bambu-dark-tertiary">
            <p className="text-xs font-medium text-bambu-gray uppercase tracking-wider mb-3">
              {t('locationHaSensors.options.generalSettings')}
            </p>
          </div>

          <div className="p-3 border border-bambu-dark-tertiary rounded-lg space-y-2">
            <label className="block text-sm text-white" htmlFor="location-sensor-poll-interval">
              {t('locationHaSensors.options.pollInterval')}
            </label>
            <NumberInput
              id="location-sensor-poll-interval"
              min={MIN_POLL_INTERVAL}
              step="1"
              value={pollInterval}
              onChange={updatePollInterval}
              fallback={DEFAULT_POLL_INTERVAL}
              className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white"
            />
            <p className="text-xs text-bambu-gray">{t('locationHaSensors.options.pollIntervalHint')}</p>
          </div></>}

          <div className="flex items-center justify-between gap-2 pt-2">
            {canResetBindings && <Button type="button" variant="secondary" onClick={() => setShowResetConfirm(true)}>
              <RotateCcw className="w-4 h-4" />
              {t('locationHaSensors.options.reset')}
            </Button>}
            <div className="flex items-center gap-2">
              <Button type="button" variant="secondary" onClick={onClose}>
                {t('common.cancel')}
              </Button>
              <Button type="submit" disabled={saveMutation.isPending}>
                <Save className="w-4 h-4" />
                {t('common.save')}
              </Button>
            </div>
          </div>
        </form>
    </Modal>

    {showResetConfirm && (
      <ConfirmModal
        title={t('locationHaSensors.options.resetConfirm.title')}
        message={t('locationHaSensors.options.resetConfirm.message')}
        confirmText={t('locationHaSensors.options.reset')}
        variant="danger"

        isLoading={resetMutation.isPending}
        onConfirm={() => resetMutation.mutate()}
        onCancel={() => setShowResetConfirm(false)}
      />
    )}
    </>
  );
}
