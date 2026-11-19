// The choice state of a preset field (spec slicing-setup r5, R6): who chose
// the value, and whether it left the listing. A choice made by a person, a
// pipeline or a re-layout record is never replaced in silence; an automatic
// one follows the printer, the filters and the listing.
//
// `applyChoice` is pure; `useChoiceFields` keeps one field per key, so a list
// of fields (Slice's filament slots, assembly's rows) needs no hook per field.
import { useCallback, useRef, useState } from 'react';
import type { PresetRef } from '../../api/client';
import type { Slot } from '../../utils/presetPickerUtils';

export type ChoiceOrigin = 'auto' | 'person' | 'pipeline' | 'record';

export interface ChoiceField {
  ref: PresetRef | null;
  origin: ChoiceOrigin;
  gone: boolean;
}

export type ChoiceEvent =
  | { kind: 'seed'; ref: PresetRef | null; origin: 'record' | 'auto'; listed: boolean }
  | { kind: 'refresh'; listed: (r: PresetRef) => boolean; usable: (r: PresetRef) => boolean; repick: () => PresetRef | null }
  | { kind: 'printerChanged'; usable: (r: PresetRef) => boolean; repick: () => PresetRef | null }
  | { kind: 'nozzleChanged'; usable: (r: PresetRef) => boolean; repick: () => PresetRef | null }
  | { kind: 'filterChanged' | 'loadedAnswered' | 'familyAnswered'; repick: () => PresetRef | null; belongs?: (r: PresetRef) => boolean }
  | { kind: 'personPicked'; ref: PresetRef | null }
  | { kind: 'pipelineApplied'; ref: PresetRef }
  | { kind: 'reset'; pick: PresetRef | null };

export interface ChoicePolicy {
  slot: Slot;
  /** What a ref that left the listing does, by who chose it. */
  onGone: Record<ChoiceOrigin, 'repick' | 'repickNamed' | 'block'>;
  /** A blocked ref that is listed again is restored (assembly: its row re-checks every time). */
  restoreOnReturn: boolean;
}

export interface ChoiceOutcome {
  field: ChoiceField;
  /** The person's choice left the listing and was replaced: say so (`chosenPresetGone`). */
  namedGone: boolean;
}

export const EMPTY_CHOICE: ChoiceField = { ref: null, origin: 'auto', gone: false };

const blockAll: ChoicePolicy['onGone'] = { auto: 'block', person: 'block', pipeline: 'block', record: 'block' };
const repickNamed: ChoicePolicy['onGone'] = { auto: 'repick', person: 'repickNamed', pipeline: 'repickNamed', record: 'repickNamed' };
const assemblyField: ChoicePolicy['onGone'] = { auto: 'repick', person: 'block', pipeline: 'block', record: 'block' };

export const POLICIES = {
  slicePrinter: { slot: 'printer', onGone: blockAll, restoreOnReturn: false },
  sliceProcess: { slot: 'process', onGone: repickNamed, restoreOnReturn: false },
  sliceFilament: { slot: 'filament', onGone: repickNamed, restoreOnReturn: false },
  calibrationPrinter: { slot: 'printer', onGone: blockAll, restoreOnReturn: false },
  calibrationProcess: { slot: 'process', onGone: repickNamed, restoreOnReturn: false },
  calibrationFilament: { slot: 'filament', onGone: repickNamed, restoreOnReturn: false },
  assemblyPrinter: { slot: 'printer', onGone: blockAll, restoreOnReturn: true },
  assemblyProcess: { slot: 'process', onGone: assemblyField, restoreOnReturn: true },
  assemblyFilament: { slot: 'filament', onGone: assemblyField, restoreOnReturn: true },
} satisfies Record<string, ChoicePolicy>;

const keep = (field: ChoiceField): ChoiceOutcome => ({ field, namedGone: false });
const auto = (ref: PresetRef | null): ChoiceOutcome => keep({ ref, origin: 'auto', gone: false });

export function applyChoice(field: ChoiceField, event: ChoiceEvent, policy: ChoicePolicy): ChoiceOutcome {
  switch (event.kind) {
    case 'seed':
      return keep({ ref: event.ref, origin: event.origin, gone: event.ref !== null && !event.listed });
    case 'personPicked':
      // The placeholder never clears a block: only a concrete pick does.
      if (event.ref === null && field.gone) return keep(field);
      return keep({ ref: event.ref, origin: 'person', gone: false });
    case 'pipelineApplied':
      return keep({ ref: event.ref, origin: 'pipeline', gone: false });
    case 'reset':
      return auto(event.pick);
    case 'refresh': {
      if (field.gone) {
        const back = policy.restoreOnReturn && field.ref !== null && event.listed(field.ref);
        return keep(back ? { ...field, gone: false } : field);
      }
      if (field.ref === null) return field.origin === 'auto' ? auto(event.repick()) : keep(field);
      if (event.listed(field.ref)) {
        if (field.origin === 'auto' && !event.usable(field.ref)) return auto(event.repick());
        return keep(field);
      }
      const mode = policy.onGone[field.origin];
      if (mode === 'block') return keep({ ...field, gone: true });
      return { field: { ref: event.repick(), origin: 'auto', gone: false }, namedGone: mode === 'repickNamed' };
    }
    case 'printerChanged':
    case 'nozzleChanged':
      if (field.gone || field.origin !== 'auto') return keep(field);
      return field.ref === null || !event.usable(field.ref) ? auto(event.repick()) : keep(field);
    case 'filterChanged':
    case 'loadedAnswered':
    case 'familyAnswered': {
      if (field.gone || field.origin !== 'auto') return keep(field);
      if (field.ref === null) return event.kind === 'familyAnswered' ? auto(event.repick()) : keep(field);
      if (event.belongs && event.belongs(field.ref)) return keep(field);
      return auto(event.repick());
    }
  }
}

/** A field that stops the action: required and empty, or gone. */
export function blocksSubmit(field: ChoiceField, required: boolean): boolean {
  return field.gone || (required && field.ref === null);
}

export function useChoiceFields<K extends string>(): {
  get: (key: K) => ChoiceField;
  dispatch: (key: K, event: ChoiceEvent, policy: ChoicePolicy) => ChoiceOutcome;
  forget: (key: K) => void;
} {
  const fields = useRef(new Map<K, ChoiceField>());
  const [, setVersion] = useState(0);
  const get = useCallback((key: K) => fields.current.get(key) ?? EMPTY_CHOICE, []);
  const dispatch = useCallback((key: K, event: ChoiceEvent, policy: ChoicePolicy) => {
    const before = fields.current.get(key) ?? EMPTY_CHOICE;
    const outcome = applyChoice(before, event, policy);
    const after = outcome.field;
    const changed =
      after.origin !== before.origin ||
      after.gone !== before.gone ||
      after.ref?.source !== before.ref?.source ||
      after.ref?.id !== before.ref?.id;
    if (changed) {
      fields.current.set(key, after);
      setVersion((v) => v + 1);
    }
    return outcome;
  }, []);
  const forget = useCallback((key: K) => {
    if (fields.current.delete(key)) setVersion((v) => v + 1);
  }, []);
  return { get, dispatch, forget };
}
