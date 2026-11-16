// The Slice dialog's two farm filters (owner's decision 7; upstream 7f00816f7
// and 8a95eb9f7 as the behaviour reference): "only printers that are online"
// narrows the printer presets, "only filaments that are loaded" the filament
// presets. Both are answered from ONE read, `GET /slicer/loaded-filaments`.
//
// What the two words mean is the server's: online = active, not archived,
// connected; loaded = the spool a slot holds and its family — never the profile
// a slot was advertised under. This module only compares that answer with the
// preset listing, by identity:
//
//   * a printer preset by its MODEL (and nozzle, when it states one), read from
//     the preset it was saved from when its own name says nothing
//     (`printerPresetModel`), and compared through the model normalisation the
//     rest of the app uses (`modelCompatibility`) — a printer also counts for
//     the file models its mirrored config accepts;
//   * a filament preset by the id its tier lists it under, against the ids the
//     server names for the loaded family — never by display name. A source
//     that states only a material is matched by material type.
//
// Display only: nothing here picks, replaces or removes a chosen preset.

import type { FamilyPresetRefs, LoadedFilament, LoadedFilamentsResponse, OnlinePrinter, UnifiedPreset } from '../api/client';
import { filamentTypesCompatible } from './amsHelpers';
import { modelCompatibility } from './modelCompatibility';
import type { PrinterParents } from './printerPresetParents';
import { printerPresetModel } from './slicerPrinterMatch';

type PresetModel = { model: string; nozzle: string | null };

/** Is this online printer one the preset's model can be sliced for? */
function printerIsOfModel(printer: OnlinePrinter, model: string): boolean {
  if (!printer.model) return false;
  // `model` is the fragment a printer preset's name carries after "Bambu Lab "
  // ("X1 Carbon"); the long marketing name is the spelling the normaliser knows.
  const verdict = modelCompatibility(`Bambu Lab ${model}`, printer.model, { [printer.model]: printer.accepts });
  return verdict === 'exact' || verdict === 'compatible';
}

function printerHasNozzle(printer: OnlinePrinter, nozzle: string | null): boolean {
  // A preset that states no nozzle, or a printer that reported none yet, is not
  // ruled out on a size nobody stated.
  if (nozzle === null || printer.nozzle_diameters.length === 0) return true;
  const size = Number.parseFloat(nozzle);
  if (Number.isNaN(size)) return true;
  return printer.nozzle_diameters.some((d) => Math.abs(d - size) < 1e-6);
}

/** Does a printer preset belong to a printer that is online now? */
export function printerPresetIsOnline(
  preset: Pick<UnifiedPreset, 'name'>,
  farm: LoadedFilamentsResponse,
  printerParents: PrinterParents | null | undefined,
): boolean {
  const identity: PresetModel | null = printerPresetModel(preset.name, printerParents);
  if (!identity) return false;
  return farm.printers.some((p) => printerIsOfModel(p, identity.model) && printerHasNozzle(p, identity.nozzle));
}

/**
 * The online printers the loaded-filament filter counts: every one, or — when a
 * printer preset is chosen and its model can be read — those of that model
 * only (`byModel`). An empty list `byModel` means "no printer of the chosen
 * model is online", which is another thing than "nothing is loaded".
 */
export function printersCountedFor(
  farm: LoadedFilamentsResponse,
  selectedPrinterName: string | null | undefined,
  printerParents: PrinterParents | null | undefined,
): { printers: OnlinePrinter[]; byModel: boolean } {
  const identity = selectedPrinterName ? printerPresetModel(selectedPrinterName, printerParents) : null;
  return identity
    ? { printers: farm.printers.filter((p) => printerIsOfModel(p, identity.model)), byModel: true }
    : { printers: farm.printers, byModel: false };
}

/** The filaments the filter counts: those on the printers `printersCountedFor` names. */
export function loadedFilamentsFor(
  farm: LoadedFilamentsResponse,
  selectedPrinterName: string | null | undefined,
  printerParents: PrinterParents | null | undefined,
): LoadedFilament[] {
  return printersCountedFor(farm, selectedPrinterName, printerParents).printers.flatMap((p) => p.filaments);
}

// A stock preset's id may carry a version suffix ("GFSA00_07") where the
// server names the base form too.
function idsOf(preset: Pick<UnifiedPreset, 'id'>): string[] {
  const base = preset.id.split('_')[0];
  return base === preset.id ? [preset.id] : [preset.id, base];
}

function isOfFamily(preset: Pick<UnifiedPreset, 'id' | 'source'>, refs: FamilyPresetRefs | undefined): boolean {
  if (!refs) return false;
  const listed = refs[preset.source] ?? [];
  // The bundle lists a preset under its name, which is its id there: exact.
  if (preset.source === 'standard' || preset.source === 'local') return listed.includes(preset.id);
  return idsOf(preset).some((id) => listed.includes(id));
}

/** Is a filament preset one of the loaded filaments? */
export function filamentPresetIsLoaded(
  preset: Pick<UnifiedPreset, 'id' | 'source' | 'filament_type'>,
  loaded: LoadedFilament[],
  families: Record<string, FamilyPresetRefs>,
): boolean {
  return loaded.some((filament) => {
    if (filament.family_id && isOfFamily(preset, families[filament.family_id])) return true;
    // A spool without a family keeps ITS type: any preset of that material.
    return (
      filament.by_material
      && Boolean(preset.filament_type)
      && Boolean(filament.material)
      && filamentTypesCompatible(preset.filament_type ?? undefined, filament.material)
    );
  });
}
