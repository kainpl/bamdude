import { useEffect, useId, useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Loader2, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { api } from '../../api/client';
import type {
  PartImageChoice,
  PartImageRef,
  Product,
  ProductPart,
  ProductPartCreate,
  ProductPartKind,
  ProductPartUpdate,
} from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { useToast } from '../../contexts/ToastContext';
import { useInnerEscape } from '../../hooks/useInnerEscape';
import { useUiPreferences } from '../../hooks/useUiPreferences';
import { getCurrencySymbol } from '../../utils/currency';
import { invalidatePartImageViews } from '../../utils/queryInvalidation';
import { Button } from '../Button';
import { Select } from '../Select';
import { WorkshopDialog } from '../workshop/WorkshopDialog';
import { WorkshopField, WorkshopFormGrid } from '../workshop/WorkshopFormGrid';
import { PartImageSection } from './PartImageSection';
import { bindingByGroup } from './partBinding';
import {
  draftFromChoice,
  needsPhotoFile,
  pendingImageRequest,
  sendImageRequest,
  type PartImageDraft,
  type PartImageRequest,
} from './partImageDraft';
import { compositionMutationKey, invalidateComposition } from './partMutations';

const FIELD_CLASS =
  'w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none';

/** The columns' lengths (WS-13 E10 A03): a purchased part's key carries the 10-character
 *  `purchased:` prefix, so its name stops at 502; an alias and the address at 512. */
const NAME_MAX: Record<ProductPartKind, number> = { printed: 512, purchased: 502 };
const ALIAS_MAX = 512;
const URL_MAX = 512;

/** An alias as the server stores it (`product_composition.normalise_alias`). */
function normaliseAlias(raw: string): string {
  return raw.trim().toLowerCase();
}

function sameList(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((value, index) => value === b[index]);
}

/** A binding is a set: the same options in another order are no change. */
function sameSet(a: readonly number[], b: readonly number[]): boolean {
  const left = new Set(a);
  const right = new Set(b);
  return left.size === right.size && [...left].every((id) => right.has(id));
}

/** The saved picture as the server says it now (plan E5 r2, D15) — not the editing base. */
interface ImageFacts {
  image: PartImageRef | null;
  choice: PartImageChoice | null;
}

function factsOf(part: ProductPart | null | undefined): ImageFacts {
  return { image: part?.image ?? null, choice: part?.image_choice ?? null };
}

/** A printed part's aliases without its own key — what the token field shows and sends. */
function ownAliases(part: ProductPart | null): string[] {
  return part ? part.aliases.filter((alias) => alias !== part.name_key) : [];
}

interface ProductPartDialogProps {
  product: Product;
  /** The part to edit; none for a new one. */
  part?: ProductPart | null;
  onClose: () => void;
}

/**
 * A product's part — new or edited — and its picture, in one dialog (WS-13 E10 C01–C07, F14;
 * spec part-thumbnails §12.4). Nothing is written before «Save part»: the aliases travel with the
 * part as its whole list, a new part carries its variant binding (A05), and the picture section
 * holds its choice and its file as a draft — «Cancel» sends nothing at all.
 *
 * ⚠️ **«Save part» is two requests, in order (plan E5, D7).** The part first — created, or patched
 * by what differs — and then, only if the draft moved, the picture. After each success the dialog
 * takes the server's answer as its base: what was sent is saved, so nothing of it is sent again. A
 * picture refused after the part was saved keeps the dialog open on the saved part — a new one is an
 * edit now — with the draft and its file kept: the retry sends the picture alone, never a second
 * create, and «Cancel» closes without the picture, undoing nothing (the error says so).
 *
 * ⚠️ **The saved picture is live, the base is not (plan E5 r2, D15–D16).** The effective picture, the
 * saved choice and its pin's validity come from `product.parts` by the base's id — refreshed by the
 * server event and every write — and from this dialog's own answers, whichever came later; the
 * caller's `part` is a snapshot taken at the opening. A picture draft the operator has not touched IS
 * «keep what is saved» and follows the saved choice; a touched one never moves, and «Save part»
 * compares it with the LIVE saved choice — an untouched draft never reverts another client's pin.
 *
 * ⚠️ **One session, one base (J).** Only this dialog's own writes move the base; a background refresh
 * of the product changes the variant list (the dictionary) but never what was ticked. A bound option
 * the product lost is named so and blocks the save until it is unticked.
 *
 * The binding is a row per variant group — «any» (nothing ticked) or the ticked options of that
 * group (spec product-part-variant-combinations п. 22): «and» between groups, «or» within one. It
 * is a set, sent whole as `variant_option_ids` — on an edit only when the set differs from the base.
 *
 * The kind is chosen at creation only (K2): the server has no `kind` on a PATCH.
 * «Not counted» belongs to a printed part with nothing per unit (K4); a count typed on a
 * marked part takes the mark off in the same request.
 */
export function ProductPartDialog({ product, part, onClose }: ProductPartDialogProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { hasPermission } = useAuth();
  const { data: settings } = useUiPreferences();

  const groups = product.variant_groups ?? [];
  const optionLabel = new Map(groups.flatMap((g) => g.options.map((o) => [o.id, `${g.name}: ${o.name}`] as const)));

  // The session's base — the part as it was at the opening, then as this dialog's writes answered.
  const [base, setBase] = useState<ProductPart | null>(part ?? null);
  // The bound options' names as the opening saw them: one the product loses later is still named.
  const [baseOptionLabels] = useState(
    () => new Map((part?.variant_option_ids ?? []).map((id) => [id, optionLabel.get(id)] as const)),
  );
  const isEdit = base != null;
  const ownKey = base?.kind === 'printed' ? base.name_key : null;
  const baseAliases = ownAliases(base);

  const ids = {
    kind: useId(),
    name: useId(),
    qty: useId(),
    extra: useId(),
    variant: useId(),
    aliases: useId(),
    aliasNote: useId(),
    ignore: useId(),
    price: useId(),
    url: useId(),
    remarks: useId(),
    photo: useId(),
  };
  // The cursor starts in the name (C01). The Modal focuses its panel in its own (child)
  // effect; this one runs after it.
  const nameId = ids.name;
  useEffect(() => {
    document.getElementById(nameId)?.focus();
  }, [nameId]);

  const [kind, setKind] = useState<ProductPartKind>(base?.kind ?? 'printed');
  const [name, setName] = useState(base?.name ?? '');
  const [qty, setQty] = useState(String(base?.qty_per_unit ?? 1));
  const [extra, setExtra] = useState(String(base?.extra_percent ?? 0));
  const [ignored, setIgnored] = useState(base?.ignored ?? false);
  const [bound, setBound] = useState<number[]>(base?.variant_option_ids ?? []);
  const [price, setPrice] = useState(base?.unit_price != null ? String(base.unit_price) : '');
  const [url, setUrl] = useState(base?.sourcing_url ?? '');
  const [remarks, setRemarks] = useState(base?.remarks ?? '');
  const [aliases, setAliases] = useState<string[]>(baseAliases);
  const [aliasDraft, setAliasDraft] = useState('');
  const [aliasNote, setAliasNote] = useState<string | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const [imageDraft, setImageDraft] = useState<PartImageDraft>(() => draftFromChoice(part?.image_choice));
  // The saved picture, live (D15): from the product by the base's id, and from this dialog's answers.
  const [facts, setFacts] = useState<ImageFacts>(() => factsOf(part));
  // The operator changed the picture in this dialog: no refresh moves the draft any more (D16).
  const touched = useRef(false);
  const livePart = base ? product.parts.find((p) => p.id === base.id) : undefined;
  // At the opening the caller's part IS the state; the product counts from its first CHANGE on — the
  // part as last seen is kept, so an unchanged one (structural sharing keeps its reference) and
  // StrictMode's second run apply nothing.
  const seenPart = useRef(livePart);
  useEffect(() => {
    if (!livePart || livePart === seenPart.current) return;
    seenPart.current = livePart;
    setFacts(factsOf(livePart));
    if (!touched.current) setImageDraft(draftFromChoice(livePart.image_choice));
  }, [livePart]);
  // A write of this dialog landed: a picture refused later says the part IS saved (D7).
  const [savedHere, setSavedHere] = useState(false);
  // Escape in the alias field empties it and stops there; on an empty field it is the
  // dialog's again — the next Escape closes it (C06).
  const aliasField = useRef<HTMLInputElement>(null);
  useInnerEscape(aliasField, aliasDraft !== '' || aliasNote !== null, () => {
    setAliasDraft('');
    setAliasNote(null);
  });

  const printed = kind === 'printed';
  const qtyNumber = qty.trim() === '' ? Number.NaN : Number(qty);
  const extraNumber = extra.trim() === '' ? Number.NaN : Number(extra);
  const extraValid = Number.isFinite(extraNumber) && extraNumber >= 0 && extraNumber <= 1000;
  const qtyValid = Number.isInteger(qtyNumber) && qtyNumber >= 0;
  // «Not counted» only at zero (C04): a count > 0 takes the mark off.
  const canIgnore = printed && qtyValid && qtyNumber === 0;
  const ignoredNow = canIgnore && ignored;
  const goneOptions = bound.filter((id) => !optionLabel.has(id));
  const variantGone = goneOptions.length > 0;
  // What is sent: the ticked options the product has, in its groups' order.
  const boundIds = [...bindingByGroup(groups, bound).values()].flat();

  function tick(optionId: number, on: boolean) {
    setBound((prev) => (on ? (prev.includes(optionId) ? prev : [...prev, optionId]) : prev.filter((id) => id !== optionId)));
  }

  /** «Any» in a group: none of its options is a condition any more. */
  function clearGroup(optionIds: number[]) {
    setBound((prev) => prev.filter((id) => !optionIds.includes(id)));
  }

  /** The alias field's text as tokens — a pasted comma list is a token per name (a comma is
   *  the field's separator); `false` (with the note) when one of them is already in the list. */
  function takeDraft(raw: string): string[] | false {
    const keys = raw.split(',').map(normaliseAlias).filter((key) => key !== '');
    const fresh = keys.filter((key, index) => keys.indexOf(key) === index);
    if (fresh.some((key) => key === ownKey || aliases.includes(key))) {
      setAliasNote(t('products.partDialog.aliasDuplicate'));
      return false;
    }
    const next = [...aliases, ...fresh];
    setAliases(next);
    setAliasDraft('');
    setAliasNote(null);
    return next;
  }

  /** The server's answer is the base now, and the fields with it: a retry measures from here (D7). */
  function adopt(saved: ProductPart) {
    setBase(saved);
    setName(saved.name);
    setQty(String(saved.qty_per_unit));
    setIgnored(saved.ignored);
    setBound(saved.variant_option_ids);
    setPrice(saved.unit_price != null ? String(saved.unit_price) : '');
    setUrl(saved.sourcing_url ?? '');
    setRemarks(saved.remarks ?? '');
    setAliases(ownAliases(saved));
    // the answer is the newest word on the picture too (D15); an untouched draft follows it (D16)
    setFacts(factsOf(saved));
    if (!touched.current) setImageDraft(draftFromChoice(saved.image_choice));
  }

  const fields = useMutation({
    mutationKey: compositionMutationKey(product.id),
    mutationFn: ({ partId, body }: { partId: number | null; body: ProductPartCreate | ProductPartUpdate }) =>
      partId != null
        ? api.updateProductPart(product.id, partId, body as ProductPartUpdate)
        : api.createProductPart(product.id, body as ProductPartCreate),
  });
  const image = useMutation({
    mutationKey: compositionMutationKey(product.id),
    mutationFn: ({ partId, request }: { partId: number; request: PartImageRequest }) => sendImageRequest(partId, request),
  });

  // ⚠️ Synchronous: a press and an Escape in the same frame see `isPending` still false —
  // the ref makes «one press, one request chain» and «no closing under a request» hold; a
  // refusal re-arms it.
  const sent = useRef(false);
  const formId = useId();
  const submitId = `${formId}-submit`;
  // After a refusal the fields are live again: the focus goes back to the button that sent it.
  const refused = fields.isError || image.isError;
  useEffect(() => {
    if (refused) document.getElementById(submitId)?.focus();
  }, [refused, fields.error, image.error, submitId]);

  function close() {
    if (sent.current) return;
    onClose();
  }

  const pending = fields.isPending || image.isPending;

  function refuse(message: string, fieldId: string) {
    setLocalError(message);
    document.getElementById(fieldId)?.focus();
  }

  /** What «Save part» sends for the part itself; `null` when an edit changed nothing (C07). */
  function fieldsBody(tokens: string[], optionIds: number[], unitPrice: number | null) {
    if (!base) {
      return printed
        ? {
            kind,
            name: name.trim(),
            qty_per_unit: qtyNumber,
            ...(extraNumber > 0 ? { extra_percent: extraNumber } : {}),
            ignored: ignoredNow,
            aliases: tokens,
            variant_option_ids: optionIds,
          }
        : {
            kind,
            name: name.trim(),
            qty_per_unit: qtyNumber,
            variant_option_ids: optionIds,
            unit_price: unitPrice,
            sourcing_url: url.trim() || null,
            remarks: remarks.trim() || null,
          };
    }
    // An edit sends what differs from the session's base (C07); the aliases only if the list moved,
    // the binding only if the SET moved.
    const data: ProductPartUpdate = {};
    if (name.trim() !== base.name) data.name = name.trim();
    if (qtyNumber !== base.qty_per_unit) data.qty_per_unit = qtyNumber;
    if (!sameSet(optionIds, base.variant_option_ids)) data.variant_option_ids = optionIds;
    if (printed) {
      if (extraNumber !== (base.extra_percent ?? 0)) data.extra_percent = extraNumber;
      if (ignoredNow !== base.ignored) data.ignored = ignoredNow;
      if (!sameList(tokens, baseAliases)) data.aliases = tokens;
    } else {
      if (unitPrice !== base.unit_price) data.unit_price = unitPrice;
      if ((url.trim() || null) !== base.sourcing_url) data.sourcing_url = url.trim() || null;
      if ((remarks.trim() || null) !== base.remarks) data.remarks = remarks.trim() || null;
    }
    return Object.keys(data).length === 0 ? null : data;
  }

  async function submit() {
    if (sent.current || pending || variantGone) return;
    setLocalError(null);
    if (name.trim() === '') return refuse(t('products.partDialog.nameRequired'), ids.name);
    if (!qtyValid) return refuse(t('products.partDialog.qtyInvalid'), ids.qty);
    if (printed && !extraValid) return refuse(t('products.partDialog.extraInvalid'), ids.extra);
    let unitPrice: number | null = null;
    if (!printed && price.trim() !== '') {
      unitPrice = Number(price);
      if (!Number.isFinite(unitPrice) || unitPrice < 0) return refuse(t('products.partDialog.priceInvalid'), ids.price);
    }
    // Text left in the alias field becomes a token, not lost (C06); a duplicate stops here.
    let tokens = aliases;
    if (printed && aliasDraft.trim() !== '') {
      const taken = takeDraft(aliasDraft);
      if (taken === false) {
        document.getElementById(ids.aliases)?.focus();
        return;
      }
      tokens = taken;
    }
    if (needsPhotoFile(imageDraft, facts.choice)) return refuse(t('products.partImage.photoRequired'), ids.photo);
    const body = fieldsBody(tokens, boundIds, unitPrice);
    const request = pendingImageRequest(imageDraft, facts.choice); // the LIVE saved choice (D16)
    if (body === null && request === null) {
      // a partial success earlier in this dialog IS a saved part: say so on the way out (final review 4)
      if (savedHere) showToast(t('products.partDialog.saved'));
      onClose();
      return;
    }

    sent.current = true;
    try {
      let saved = base;
      if (body !== null) {
        saved = await fields.mutateAsync({ partId: base?.id ?? null, body });
        adopt(saved);
        setSavedHere(true);
        invalidateComposition(queryClient, product.id);
        // what the part matches follows its saved name and aliases (D8): the gallery is asked again
        queryClient.invalidateQueries({ queryKey: ['part-image-candidates', saved.id] });
      }
      if (request !== null && saved) {
        adopt(await image.mutateAsync({ partId: saved.id, request }));
        invalidatePartImageViews(queryClient);
      }
    } catch {
      // The refusal is in the dialog; the draft and its file stay for the retry (D7). A refused pin may be
      // a gallery that moved: the saved state and the candidates are asked again (final review 2).
      sent.current = false;
      if (base) {
        queryClient.invalidateQueries({ queryKey: ['product', product.id] });
        queryClient.invalidateQueries({ queryKey: ['part-image-candidates', base.id] });
      }
      return;
    }
    showToast(t('products.partDialog.saved'));
    onClose();
  }

  // the server's own sentence without its closing full stop: the dialog's sentence puts one after it
  const pictureRefusal = image.isError ? (image.error as Error).message.replace(/\.\s*$/, '') : null;
  const error =
    localError ??
    (fields.isError ? (fields.error as Error).message : undefined) ??
    (pictureRefusal === null
      ? undefined
      : savedHere
        ? t('products.partImage.partSavedPictureFailed', { message: pictureRefusal })
        : t('products.partImage.failed', { message: pictureRefusal }));
  const kindLabel = (k: ProductPartKind) => t(k === 'printed' ? 'products.partDialog.printed' : 'products.partDialog.purchased');
  const currency = getCurrencySymbol(settings?.currency || 'USD');

  return (
    <WorkshopDialog
      onClose={close}
      title={isEdit ? t('products.partDialog.editTitle') : t('products.partDialog.createTitle')}
      subtitle={`${product.code} · ${product.name}`}
      size="lg"
      pending={pending}
      error={error}
      footer={
        <>
          <Button type="button" variant="secondary" onClick={close} disabled={pending}>
            {t('common.cancel')}
          </Button>
          <Button id={submitId} type="submit" form={formId} disabled={pending || variantGone}>
            {pending && <Loader2 className="w-4 h-4 animate-spin" />}
            {pending ? t('products.partDialog.saving') : t('products.partDialog.save')}
          </Button>
        </>
      }
    >
      <form
        id={formId}
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <WorkshopFormGrid>
          <WorkshopField
            label={t('products.partDialog.kind')}
            htmlFor={ids.kind}
            hint={isEdit ? t('products.partDialog.kindFixed') : undefined}
          >
            {isEdit ? (
              <input
                id={ids.kind}
                type="text"
                readOnly
                value={kindLabel(kind)}
                aria-describedby={`${ids.kind}-hint`}
                // Read-only: no field ground, so it does not look like a box to type into.
                className="w-full cursor-default rounded-lg border border-transparent bg-transparent px-0 py-2 text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green"
              />
            ) : (
              <Select
                id={ids.kind}
                value={kind}
                onChange={(e) => setKind(e.target.value as ProductPartKind)}
                disabled={pending}
                className="w-full"
              >
                <option value="printed">{kindLabel('printed')}</option>
                <option value="purchased">{kindLabel('purchased')}</option>
              </Select>
            )}
          </WorkshopField>

          <WorkshopField label={t('products.partDialog.name')} htmlFor={ids.name}>
            <input
              id={ids.name}
              type="text"
              value={name}
              onChange={(e) => {
                setName(e.target.value);
                setLocalError(null);
              }}
              maxLength={NAME_MAX[kind]}
              className={FIELD_CLASS}
              disabled={pending}
            />
          </WorkshopField>

          <WorkshopField
            label={t('products.partDialog.perUnit')}
            htmlFor={ids.qty}
            hint={t('products.partDialog.perUnitHint')}
          >
            <input
              id={ids.qty}
              type="number"
              min={0}
              step={1}
              value={qty}
              onChange={(e) => {
                setQty(e.target.value);
                setLocalError(null);
              }}
              aria-describedby={`${ids.qty}-hint`}
              className={`${FIELD_CLASS} tabular-nums`}
              disabled={pending}
            />
          </WorkshopField>

          {printed && (
            <WorkshopField label={t('products.partDialog.extraPercent')} htmlFor={ids.extra}
              hint={t('products.partDialog.extraHint')}>
              <input id={ids.extra} type="number" min={0} max={1000} step="any" value={extra}
                onChange={(e) => { setExtra(e.target.value); setLocalError(null); }}
                aria-describedby={`${ids.extra}-hint`} className={`${FIELD_CLASS} tabular-nums`} disabled={pending} />
            </WorkshopField>
          )}

          {(groups.length > 0 || variantGone) && (
            <fieldset
              aria-describedby={`${ids.variant}-hint`}
              className="col-span-full m-0 flex min-w-0 flex-col gap-1.5 border-0 p-0"
            >
              <legend className="mb-1 p-0 text-sm text-bambu-gray-light">{t('products.partDialog.variant')}</legend>
              {groups.map((group) => {
                const optionIds = group.options.map((o) => o.id);
                const any = !optionIds.some((id) => bound.includes(id));
                return (
                  <fieldset
                    key={group.id}
                    className="m-0 flex min-w-0 flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark px-3 py-2"
                  >
                    <legend className="float-left mr-2 p-0 text-sm font-semibold text-white">{group.name}</legend>
                    <label className="inline-flex items-center gap-1.5 text-sm text-bambu-gray-light">
                      <input
                        type="checkbox"
                        checked={any}
                        onChange={() => clearGroup(optionIds)}
                        disabled={pending}
                        className="accent-bambu-green"
                      />
                      {t('products.partDialog.variantAny')}
                    </label>
                    {group.options.map((option) => (
                      <label key={option.id} className="inline-flex min-w-0 items-center gap-1.5 text-sm text-white">
                        <input
                          type="checkbox"
                          checked={bound.includes(option.id)}
                          onChange={(e) => tick(option.id, e.target.checked)}
                          disabled={pending}
                          className="accent-bambu-green"
                        />
                        <span className="wrap-anywhere">{option.name}</span>
                      </label>
                    ))}
                  </fieldset>
                );
              })}
              {variantGone && (
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1 px-3">
                  {goneOptions.map((id) => (
                    <label key={id} className="inline-flex min-w-0 items-center gap-1.5 text-sm text-amber-700 dark:text-amber-400">
                      <input
                        type="checkbox"
                        checked
                        onChange={() => tick(id, false)}
                        disabled={pending}
                        aria-invalid
                        className="accent-bambu-green"
                      />
                      <span className="wrap-anywhere">
                        {t('products.partDialog.variantGone', { name: baseOptionLabels.get(id) ?? `#${id}` })}
                      </span>
                    </label>
                  ))}
                </div>
              )}
              <p id={`${ids.variant}-hint`} className="text-xs leading-[18px] text-bambu-gray">
                {variantGone ? t('products.partDialog.variantGoneHint') : t('products.partDialog.variantHint')}
              </p>
            </fieldset>
          )}

          {printed ? (
            <>
              <WorkshopField
                label={t('products.partDialog.aliases')}
                htmlFor={ids.aliases}
                hint={t('products.partDialog.aliasesHint')}
                full
              >
                <ul data-testid="part-alias-tokens" className="flex flex-wrap gap-1.5">
                  {ownKey && (
                    <li className="inline-flex items-center gap-1 rounded bg-bambu-dark-tertiary px-2 py-0.5 text-xs text-bambu-gray-light">
                      <span className="font-mono">{ownKey}</span>
                      <span className="sr-only"> — {t('products.partDialog.ownKey')}</span>
                    </li>
                  )}
                  {aliases.map((alias) => (
                    <li
                      key={alias}
                      className="inline-flex items-center gap-1 rounded bg-bambu-dark-tertiary px-2 py-0.5 text-xs text-bambu-gray-light"
                    >
                      <span className="font-mono">{alias}</span>
                      <button
                        type="button"
                        onClick={() => setAliases(aliases.filter((a) => a !== alias))}
                        disabled={pending}
                        aria-label={t('products.partDialog.aliasRemove', { name: alias })}
                        className="rounded text-bambu-gray hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green"
                      >
                        <X className="h-3 w-3" />
                      </button>
                    </li>
                  ))}
                </ul>
                <input
                  ref={aliasField}
                  id={ids.aliases}
                  type="text"
                  value={aliasDraft}
                  maxLength={ALIAS_MAX}
                  placeholder={t('products.partDialog.aliasPlaceholder')}
                  onChange={(e) => {
                    setAliasDraft(e.target.value);
                    setAliasNote(null);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ',') {
                      // A token, never the form's submit (C06).
                      e.preventDefault();
                      takeDraft(aliasDraft);
                    }
                  }}
                  aria-describedby={`${ids.aliases}-hint${aliasNote ? ` ${ids.aliasNote}` : ''}`}
                  aria-invalid={aliasNote !== null || undefined}
                  className={FIELD_CLASS}
                  disabled={pending}
                />
                {aliasNote && (
                  <p id={ids.aliasNote} role="status" className="text-xs text-amber-700 dark:text-amber-400">
                    {aliasNote}
                  </p>
                )}
              </WorkshopField>

              <div className="col-span-full flex min-w-0 flex-col gap-1">
                <label htmlFor={ids.ignore} className="inline-flex items-center gap-2 text-sm text-bambu-gray-light">
                  <input
                    id={ids.ignore}
                    type="checkbox"
                    checked={ignoredNow}
                    onChange={(e) => setIgnored(e.target.checked)}
                    disabled={pending || !canIgnore}
                    aria-describedby={`${ids.ignore}-hint`}
                    className="accent-bambu-green"
                  />
                  {t('products.partDialog.ignore')}
                </label>
                <p id={`${ids.ignore}-hint`} className="text-xs leading-[18px] text-bambu-gray">
                  {canIgnore ? t('products.partDialog.ignoreHint') : t('products.partDialog.ignoreNeedsZero')}
                </p>
              </div>
            </>
          ) : (
            <>
              <WorkshopField label={t('products.partDialog.unitPrice', { currency })} htmlFor={ids.price}>
                <input
                  id={ids.price}
                  type="number"
                  min={0}
                  step="0.01"
                  value={price}
                  onChange={(e) => {
                    setPrice(e.target.value);
                    setLocalError(null);
                  }}
                  className={`${FIELD_CLASS} tabular-nums`}
                  disabled={pending}
                />
              </WorkshopField>
              <WorkshopField label={t('products.partDialog.sourcingUrl')} htmlFor={ids.url}>
                <input
                  id={ids.url}
                  type="url"
                  value={url}
                  maxLength={URL_MAX}
                  onChange={(e) => setUrl(e.target.value)}
                  className={FIELD_CLASS}
                  disabled={pending}
                />
              </WorkshopField>
              <WorkshopField label={t('products.partDialog.remarks')} htmlFor={ids.remarks} full>
                <input
                  id={ids.remarks}
                  type="text"
                  value={remarks}
                  onChange={(e) => setRemarks(e.target.value)}
                  className={FIELD_CLASS}
                  disabled={pending}
                />
              </WorkshopField>
            </>
          )}
        </WorkshopFormGrid>
        <PartImageSection
          productId={product.id}
          partId={base?.id ?? null}
          image={facts.image}
          choice={facts.choice}
          kind={kind}
          name={name.trim() || base?.name || ''}
          draft={imageDraft}
          onChange={(next) => {
            touched.current = true;
            setImageDraft(next);
            setLocalError(null);
            image.reset(); // a new choice is not the one refused (browser check, plan E5 Task 41)
          }}
          disabled={pending}
          photoInputId={ids.photo}
          canRerender={hasPermission('products:update')}
        />
      </form>
    </WorkshopDialog>
  );
}
