/**
 * The form of "Assemble project", as plain data: defaults, validation and the request it becomes.
 * No React here — the dialog holds one `AssembleForm` in state and asks these functions.
 */
import type {
  AssemblyLayout,
  AssemblyOrientation,
  AssemblyRecordResponse,
  AssemblyRequest,
  AssemblyRotationStep,
  BedType,
  PresetRef,
  RelayoutRequest,
} from '../../api/client';
import type { SlicerKind } from '../../hooks/useSlicerHealth';
import { bedPlateForSlicer, isBedType, rememberedBedPlate } from '../slicing-setup/bedPlates';

export const MAX_COPIES = 2000;
export const MAX_TARGETS = 8;
export const MAX_SPACING = 50;

export const ORIENTATIONS: AssemblyOrientation[] = ['as_is', 'x+90', 'x-90', 'y+90', 'y-90', 'x180'];
export const ROTATION_STEPS: AssemblyRotationStep[] = [0, 180, 90, 45];

export interface PartRow {
  /** Absent on a re-layout: the parts are the stored ones, in their stored order. */
  fileId?: number;
  name: string;
  quantity: number;
  fill: boolean;
  orientation: AssemblyOrientation;
}

export interface TargetRow {
  /** Stable React key; never sent. */
  key: string;
  printer: PresetRef | null;
  process: PresetRef | null;
  filament: PresetRef | null;
  nozzle: number;
  bedType: BedType;
}

export interface AssembleForm {
  name: string;
  parts: PartRow[];
  targets: TargetRow[];
  /** Text, as typed: an input that parses on every key stroke cannot hold "2." */
  spacing: string;
  rotationStep: AssemblyRotationStep;
  centre: boolean;
  splitPlates: boolean;
  trashPrevious: boolean;
}

export type FormIssue = 'name' | 'targets' | 'quantity' | 'copies' | 'spacing';

let nextKey = 0;

export function emptyTarget(): TargetRow {
  nextKey += 1;
  return { key: `target-${nextKey}`, printer: null, process: null, filament: null, nozzle: 1, bedType: rememberedBedPlate() };
}

function stem(filename: string): string {
  const dot = filename.lastIndexOf('.');
  return dot > 0 ? filename.slice(0, dot) : filename;
}

/** The single file's name without its extension; for several, the first one's plus how many more. */
export function defaultName(files: { filename: string }[]): string {
  if (files.length === 0) return '';
  const first = stem(files[0].filename);
  return files.length === 1 ? first : `${first} +${files.length - 1}`;
}

export function initialForm(files: { id: number; filename: string }[]): AssembleForm {
  return {
    name: defaultName(files),
    parts: files.map((file) => ({
      fileId: file.id,
      name: file.filename,
      quantity: 1,
      fill: false,
      orientation: 'as_is',
    })),
    targets: [emptyTarget()],
    spacing: '2',
    rotationStep: 90,
    centre: true,
    splitPlates: false,
    trashPrevious: true,
  };
}

/** A re-layout opens with the values the file was made with. */
export function formFromRecord(record: AssemblyRecordResponse): AssembleForm {
  return {
    name: record.base_name,
    parts: record.parts.map((part) => ({
      name: part.name,
      quantity: part.quantity,
      fill: part.fill,
      // the models inside the file are already turned: a new choice turns them further
      orientation: 'as_is',
    })),
    targets: [
      {
        ...emptyTarget(),
        printer: record.printer.printer,
        process: record.printer.process,
        filament: record.printer.filament,
        nozzle: record.printer.nozzle,
        ...(isBedType(record.printer.bed_type) ? { bedType: record.printer.bed_type } : {}),
      },
    ],
    spacing: String(record.layout.spacing),
    rotationStep: record.layout.rotation_step,
    centre: record.layout.centre,
    splitPlates: record.layout.split_plates,
    trashPrevious: true,
  };
}

export function parseSpacing(text: string): number | null {
  const value = Number(text.trim().replace(',', '.'));
  return text.trim() !== '' && Number.isFinite(value) && value >= 0 && value <= MAX_SPACING ? value : null;
}

/** Copies the request asks for by number; a "fill plate" part asks for none. */
export function totalCopies(form: AssembleForm): number {
  return form.parts.reduce((sum, part) => sum + (part.fill ? 0 : part.quantity), 0);
}

export function validate(form: AssembleForm): FormIssue[] {
  const issues: FormIssue[] = [];
  if (form.name.trim() === '') issues.push('name');
  if (
    form.targets.length === 0 ||
    form.targets.length > MAX_TARGETS ||
    form.targets.some((target) => !target.printer || !target.process || !target.filament)
  ) {
    issues.push('targets');
  }
  if (
    form.parts.some(
      (part) => !part.fill && (!Number.isInteger(part.quantity) || part.quantity < 1 || part.quantity > MAX_COPIES),
    )
  ) {
    issues.push('quantity');
  } else if (totalCopies(form) > MAX_COPIES) {
    issues.push('copies');
  }
  if (parseSpacing(form.spacing) === null) issues.push('spacing');
  return issues;
}

function layoutOf(form: AssembleForm): AssemblyLayout {
  return {
    spacing: parseSpacing(form.spacing) ?? 2,
    rotation_step: form.rotationStep,
    centre: form.centre,
    split_plates: form.splitPlates,
  };
}

/** Call only for a form `validate` found nothing in. */
export function toRequest(form: AssembleForm, slicer: SlicerKind | null = null): AssemblyRequest {
  return {
    parts: form.parts.map((part) => ({
      ...(part.fileId !== undefined ? { file_id: part.fileId } : {}),
      quantity: part.fill ? 1 : part.quantity,
      fill: part.fill,
      orientation: part.orientation,
    })),
    targets: form.targets.map((target) => ({
      printer_preset: target.printer as PresetRef,
      process_preset: target.process as PresetRef,
      filament_preset: target.filament as PresetRef,
      nozzle: target.nozzle,
      bed_type: bedPlateForSlicer(target.bedType, slicer),
    })),
    layout: layoutOf(form),
    name: form.name.trim(),
    ...(slicer ? { slicer } : {}),
  };
}

export function toRelayoutRequest(form: AssembleForm, slicer: SlicerKind | null = null): RelayoutRequest {
  return { ...toRequest(form, slicer), trash_previous: form.trashPrevious };
}
