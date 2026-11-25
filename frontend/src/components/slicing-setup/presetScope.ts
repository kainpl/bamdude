import type { PresetSource, UnifiedPresetsResponse } from '../../api/client';
import type { SlicerKind } from '../../hooks/useSlicerHealth';

export function presetSourceFitsSlicer(source: PresetSource, slicer: SlicerKind): boolean {
  return source === 'local' || source === 'standard'
    || source === (slicer === 'orcaslicer' ? 'orca_cloud' : 'cloud');
}

const emptyTier = { printer: [], process: [], filament: [] };

export function scopePresetCatalogue(data: UnifiedPresetsResponse, slicer: SlicerKind): UnifiedPresetsResponse {
  return slicer === 'orcaslicer'
    ? { ...data, cloud: emptyTier, cloud_status: 'not_authenticated' }
    : { ...data, orca_cloud: emptyTier, orca_cloud_status: 'not_authenticated' };
}
