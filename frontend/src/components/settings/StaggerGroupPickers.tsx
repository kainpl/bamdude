import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import { byLocationName } from '../../utils/locationOrder';
import { parseIdList, parseLimitMap, setLimit, toggleId } from './staggerGroupIds';

interface Props {
  byTags: boolean;
  tagIds: string;          // JSON array on the wire
  tagLimits: string;       // JSON object id → cap on the wire
  byLocation: boolean;
  locationIds: string;     // JSON array on the wire
  locationLimits: string;  // JSON object id → cap on the wire
  /** The global `stagger_concurrent` — what an empty limit means, shown as the placeholder. */
  globalCap: number;
  onChange: (
    key:
      | 'stagger_split_by_tags' | 'stagger_group_tag_ids' | 'stagger_tag_limits'
      | 'stagger_split_by_location' | 'stagger_group_location_ids' | 'stagger_location_limits',
    value: boolean | string,
  ) => void;
}

interface ToggleRowProps {
  checked: boolean;
  onToggle: (v: boolean) => void;
  label: string;
  description: string;
}

/**
 * Declared at module scope, not inside the component: a component defined in a
 * render body is a new type on every render, so React unmounts and remounts it
 * — which loses focus mid-click on the checkbox it wraps.
 */
function ToggleRow({ checked, onToggle, label, description }: ToggleRowProps) {
  return (
    <div className="flex items-center justify-between">
      <div>
        <label className="block text-sm text-white">{label}</label>
        <p className="text-xs text-bambu-gray mt-0.5">{description}</p>
      </div>
      <label className="relative inline-flex items-center cursor-pointer">
        <input type="checkbox" checked={checked} onChange={(e) => onToggle(e.target.checked)} className="sr-only peer" aria-label={label} />
        <div className="w-11 h-6 bg-bambu-dark-tertiary peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-bambu-green"></div>
      </label>
    </div>
  );
}

interface LimitFieldProps {
  name: string;
  value: number | undefined;
  globalCap: number;
  onChange: (value: number | null) => void;
}

/** The per-group cap beside a picked row. Empty = the global cap, which the placeholder shows. */
function LimitField({ name, value, globalCap, onChange }: LimitFieldProps) {
  const { t } = useTranslation();
  return (
    <input
      type="number"
      min={1}
      inputMode="numeric"
      aria-label={t('settings.staggerGroupLimitFor', { name })}
      placeholder={String(globalCap)}
      value={value ?? ''}
      onChange={(e) => onChange(e.target.value === '' ? null : Math.max(1, parseInt(e.target.value, 10) || 1))}
      className="w-14 ml-1 px-1.5 py-0.5 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-xs text-center focus:border-bambu-green focus:outline-none"
    />
  );
}

/**
 * Which tags and which locations are staggered-start groups.
 *
 * A picker, not a pattern (spec decision 4): the operator ticks the entities
 * that are groups, and what is ticked is exactly what the scheduler uses.
 */
export function StaggerGroupPickers({ byTags, tagIds, tagLimits: tagLimitsRaw, byLocation, locationIds, locationLimits: locationLimitsRaw, globalCap, onChange }: Props) {
  const { t } = useTranslation();
  const tags = useQuery({ queryKey: ['printer-tags'], queryFn: api.getPrinterTags, enabled: byTags });
  const locations = useQuery({ queryKey: ['printer-locations'], queryFn: api.getPrinterLocations, enabled: byLocation });
  const pickedTags = parseIdList(tagIds);
  const pickedLocations = parseIdList(locationIds);
  const tagLimits = parseLimitMap(tagLimitsRaw);
  const locationLimits = parseLimitMap(locationLimitsRaw);

  return (
    <div className="space-y-3 pt-3 border-t border-bambu-dark-tertiary">
      <ToggleRow checked={byTags} onToggle={(v) => onChange('stagger_split_by_tags', v)}
        label={t('settings.staggerSplitByTags')} description={t('settings.staggerSplitByTagsDescription')} />
      {byTags && (
        <fieldset className="pl-2">
          <legend className="text-xs text-bambu-gray mb-1">{t('settings.staggerGroupsPick')}</legend>
          {(tags.data?.tags ?? []).length === 0 && <p className="text-xs text-bambu-gray">{t('printers.tags.empty')}</p>}
          <div className="flex flex-wrap gap-2">
            {[...(tags.data?.tags ?? [])].sort(byLocationName((tag) => tag.name)).map((tag) => (
              <label key={tag.id} className="inline-flex items-center gap-1.5 text-sm text-white">
                <input type="checkbox" checked={pickedTags.includes(tag.id)} aria-label={tag.name}
                  onChange={() => {
                    const wasPicked = pickedTags.includes(tag.id);
                    onChange('stagger_group_tag_ids', toggleId(pickedTags, tag.id));
                    // Unpicking clears the override, or a stale limit would silently return with the next pick.
                    if (wasPicked && tag.id in tagLimits) onChange('stagger_tag_limits', setLimit(tagLimitsRaw, tag.id, null));
                  }} />
                {/* The tag's own colour, so the phase reads the same here as it
                    does on a printer card and in the queue banner. A colourless
                    tag keeps an outlined placeholder rather than jumping left. */}
                <span className="w-2 h-2 rounded-full shrink-0"
                  style={{ backgroundColor: tag.color ?? 'transparent', border: tag.color ? 'none' : '1px solid currentColor' }}
                  aria-hidden />
                {tag.name}
                {pickedTags.includes(tag.id) && (
                  <LimitField name={tag.name} value={tagLimits[tag.id]} globalCap={globalCap}
                    onChange={(v) => onChange('stagger_tag_limits', setLimit(tagLimitsRaw, tag.id, v))} />
                )}
              </label>
            ))}
          </div>
          {pickedTags.length === 0 && <p className="text-xs text-bambu-gray mt-1">{t('settings.staggerGroupsNone')}</p>}
        </fieldset>
      )}
      <ToggleRow checked={byLocation} onToggle={(v) => onChange('stagger_split_by_location', v)}
        label={t('settings.staggerSplitByLocation')} description={t('settings.staggerSplitByLocationDescription')} />
      {byLocation && (
        <fieldset className="pl-2">
          <legend className="text-xs text-bambu-gray mb-1">{t('settings.staggerGroupsPick')}</legend>
          {(locations.data?.locations ?? []).length === 0 && <p className="text-xs text-bambu-gray">{t('printers.locations.empty')}</p>}
          <div className="flex flex-col gap-1">
            {[...(locations.data?.locations ?? [])].sort(byLocationName((loc) => loc.path)).map((loc) => (
              <label key={loc.id} className="inline-flex items-center gap-1.5 text-sm text-white" style={{ paddingLeft: (loc.depth - 1) * 16 }} title={loc.path}>
                <input type="checkbox" checked={pickedLocations.includes(loc.id)} aria-label={loc.name}
                  onChange={() => {
                    const wasPicked = pickedLocations.includes(loc.id);
                    onChange('stagger_group_location_ids', toggleId(pickedLocations, loc.id));
                    // Unpicking clears the override, or a stale limit would silently return with the next pick.
                    if (wasPicked && loc.id in locationLimits) onChange('stagger_location_limits', setLimit(locationLimitsRaw, loc.id, null));
                  }} />
                {loc.name}
                {pickedLocations.includes(loc.id) && (
                  <LimitField name={loc.name} value={locationLimits[loc.id]} globalCap={globalCap}
                    onChange={(v) => onChange('stagger_location_limits', setLimit(locationLimitsRaw, loc.id, v))} />
                )}
              </label>
            ))}
          </div>
          {pickedLocations.length === 0 && <p className="text-xs text-bambu-gray mt-1">{t('settings.staggerGroupsNone')}</p>}
        </fieldset>
      )}
      {(byTags || byLocation) && <p className="text-xs text-bambu-gray">{t('settings.staggerWildcardHint')}</p>}
      {(byTags || byLocation) && <p className="text-xs text-bambu-gray">{t('settings.staggerGroupLimitHint')}</p>}
    </div>
  );
}
