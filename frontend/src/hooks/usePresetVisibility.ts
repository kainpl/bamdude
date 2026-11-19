import { useCallback, useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type UnifiedPreset, type UnifiedPresetsResponse } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { usePersistedState, listViewParser } from './usePersistedState';
import { isCustomPreset, TIER_ORDER, type Slot } from '../utils/presetPickerUtils';
import { buildPrinterParents } from '../utils/printerPresetParents';
import { printerPresetModel } from '../utils/slicerPrinterMatch';
import { modelCompatibility } from '../utils/modelCompatibility';

const parseFlag = listViewParser<'on' | 'off'>(['on', 'off']);

/** Shared display preferences of slicing and assembly; never a dispatch/admission filter. */
export function usePresetVisibility(presets: UnifiedPresetsResponse | undefined) {
  const { hasPermission, loading } = useAuth();
  const canReadFarm = hasPermission('printers:read');
  const [farm, setFarm] = usePersistedState('bamdude:presets:only-farm', 'on', parseFlag);
  const [mine, setMine] = usePersistedState('bamdude:presets:only-mine', 'on', parseFlag);
  const printers = useQuery({ queryKey: ['printers'], queryFn: api.getPrinters, enabled: canReadFarm && farm === 'on' });
  const parents = useMemo(() => Object.fromEntries(TIER_ORDER.map((tier) => [tier, buildPrinterParents(presets, tier)])), [presets]);
  const keep = useCallback((preset: UnifiedPreset, slot: Slot) => {
    if (slot !== 'printer') return mine !== 'on' || preset.source === 'orca_cloud' || isCustomPreset(preset);
    if (farm !== 'on' || !canReadFarm || !printers.data || printers.isError) return true;
    const identity = printerPresetModel(preset.name, parents[preset.source]);
    // Model cards are inheritance parents, not executable machine presets.
    if (!identity || identity.nozzle === null) return false;
    // A queue pause, disconnection or maintenance does not remove a model from the farm.
    return printers.data.some((printer) => !printer.archived
      && modelCompatibility(`Bambu Lab ${identity.model}`, printer.model) === 'exact');
  }, [mine, farm, canReadFarm, printers.data, printers.isError, parents]);
  const visible = useMemo(() => {
    if (!presets) return undefined;
    const next = { ...presets };
    for (const tier of TIER_ORDER) next[tier] = {
      printer: presets[tier].printer.filter((p) => keep(p, 'printer')),
      process: presets[tier].process.filter((p) => keep(p, 'process')),
      filament: presets[tier].filament.filter((p) => keep(p, 'filament')),
    };
    return next;
  }, [presets, keep]);
  return { keep, visible, canReadFarm, onlyFarm: farm === 'on', onlyMine: mine === 'on',
    setOnlyFarm: (on: boolean) => setFarm(on ? 'on' : 'off'),
    setOnlyMine: (on: boolean) => setMine(on ? 'on' : 'off'),
    ready: !loading && (!canReadFarm || farm !== 'on' || printers.isFetched),
    unavailable: canReadFarm && farm === 'on' && printers.isError };
}
