import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import { byLocationName } from '../../utils/locationOrder';
import { parseIdList, toggleId } from './staggerGroupIds';

interface Props {
  byTags: boolean;
  tagIds: string;          // JSON array on the wire
  byLocation: boolean;
  locationIds: string;     // JSON array on the wire
  onChange: (key: 'stagger_split_by_tags' | 'stagger_group_tag_ids' | 'stagger_split_by_location' | 'stagger_group_location_ids', value: boolean | string) => void;
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

/**
 * Which tags and which locations are staggered-start groups.
 *
 * A picker, not a pattern (spec decision 4): the operator ticks the entities
 * that are groups, and what is ticked is exactly what the scheduler uses.
 */
export function StaggerGroupPickers({ byTags, tagIds, byLocation, locationIds, onChange }: Props) {
  const { t } = useTranslation();
  const tags = useQuery({ queryKey: ['printer-tags'], queryFn: api.getPrinterTags, enabled: byTags });
  const locations = useQuery({ queryKey: ['printer-locations'], queryFn: api.getPrinterLocations, enabled: byLocation });
  const pickedTags = parseIdList(tagIds);
  const pickedLocations = parseIdList(locationIds);

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
                <input type="checkbox" checked={pickedTags.includes(tag.id)}
                  onChange={() => onChange('stagger_group_tag_ids', toggleId(pickedTags, tag.id))} />
                {tag.name}
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
                <input type="checkbox" checked={pickedLocations.includes(loc.id)}
                  onChange={() => onChange('stagger_group_location_ids', toggleId(pickedLocations, loc.id))} />
                {loc.name}
              </label>
            ))}
          </div>
          {pickedLocations.length === 0 && <p className="text-xs text-bambu-gray mt-1">{t('settings.staggerGroupsNone')}</p>}
        </fieldset>
      )}
      {(byTags || byLocation) && <p className="text-xs text-bambu-gray">{t('settings.staggerWildcardHint')}</p>}
    </div>
  );
}
