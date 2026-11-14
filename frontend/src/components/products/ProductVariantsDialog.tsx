import { useEffect, useId, useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Loader2, Plus, X } from 'lucide-react';
import { api, ApiError } from '../../api/client';
import type { Product, VariantsApply } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { invalidateProductVariants } from '../../utils/queryInvalidation';
import { Button } from '../Button';
import { Select } from '../Select';
import { ActionConfirm } from '../workshop/ActionConfirm';
import { WorkshopDialog } from '../workshop/WorkshopDialog';
import { variantsMutationKey } from './partMutations';
import {
  addGroup,
  addOption,
  draftErrors,
  draftFromProduct,
  draftToApply,
  restoreGroup,
  restoreOption,
  sameDraft,
  type DraftGroup,
  type DraftOption,
  type VariantsDraft,
} from './variantsDraft';

const FIELD_CLASS =
  'w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none aria-[invalid=true]:border-red-500';
const NAME_MAX = 128;

/** What the last refusal is about (R03): a row of the draft, a row the draft let go, or nothing named. */
type Refusal =
  | { kind: 'field'; message: string; group: string; option?: string }
  | { kind: 'restore'; message: string; target: 'group' | 'option'; id: number; name: string }
  | { kind: 'changed'; message: string }
  | { kind: 'plain'; message: string };

/**
 * A product's variant groups as ONE draft, saved by one `PUT /products/{id}/variants`
 * against the revision it was opened on (WS-13 E10 D01–D06, F16, K8).
 *
 * ⚠️ **One session, one base (J).** The product handed over at the opening is the base:
 * its revision is sent, its usage counts shut what the server would refuse to delete,
 * its names name a row the server would not let go. A background refresh of the page
 * changes none of it; only a confirmed «Reload» after `variants_changed` starts a new
 * session — and replaces the draft only once the read succeeded.
 *
 * Renames keep ids (configuration keys are built from ids); a new row carries a temp id.
 * An existing group stored without a standard, or empty, stays so unless the operator
 * chooses otherwise — the draft never picks a first option by itself (A06, E9-V01).
 */
export function ProductVariantsDialog({ product, onClose }: { product: Product; onClose: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const uid = useId();
  const idOf = (key: string, part: string) => `${uid}-${key}-${part}`;

  const [base, setBase] = useState<Product>(product);
  const [draft, setDraft] = useState<VariantsDraft>(() => draftFromProduct(product));
  const [refusal, setRefusal] = useState<Refusal | null>(null);
  const [reloading, setReloading] = useState(false);
  const errors = draftErrors(draft);

  /** Every edit of the draft ends the last refusal: it was about the draft as it was. */
  const update = (next: (d: VariantsDraft) => VariantsDraft) => {
    setDraft(next);
    setRefusal(null);
  };
  const setGroup = (key: string, patch: Partial<DraftGroup>) =>
    update((d) => ({ groups: d.groups.map((g) => (g.key === key ? { ...g, ...patch } : g)) }));
  const setOptionName = (group: string, option: string, name: string) =>
    update((d) => ({
      groups: d.groups.map((g) =>
        g.key === group ? { ...g, options: g.options.map((o) => (o.key === option ? { ...o, name } : o)) } : g,
      ),
    }));

  const storedGroup = (id?: number) => (id == null ? undefined : base.variant_groups?.find((g) => g.id === id));
  const storedOption = (id?: number) =>
    id == null ? undefined : base.variant_groups?.flatMap((g) => g.options).find((o) => o.id === id);

  /** Why the server would refuse letting this option go, or null (D03). */
  const optionRefusal = (group: DraftGroup, option: DraftOption): string | null => {
    if (option.key === group.standard) return t('products.variants.isStandard');
    const stored = storedOption(option.id);
    if (!stored) return null;
    // A count the server masks (WS-13 E13 O12) is not known here — the server keeps its rule.
    if ((stored.lines_count ?? 0) > 0) return t('products.variants.chosenInLines', { count: stored.lines_count ?? 0 });
    if ((stored.stock_count ?? 0) > 0) return t('products.variants.heldByStock', { count: stored.stock_count ?? 0 });
    if (stored.parts_count > 0) return t('products.variants.hasParts', { count: stored.parts_count });
    return null;
  };
  const groupRefusal = (group: DraftGroup): string | null => {
    const stored = storedGroup(group.id);
    if (!stored) return null;
    if ((stored.lines_count ?? 0) > 0) return t('products.variants.chosenInLines', { count: stored.lines_count ?? 0 });
    if ((stored.stock_count ?? 0) > 0) return t('products.variants.heldByStock', { count: stored.stock_count ?? 0 });
    if (stored.parts_count > 0) return t('products.variants.hasParts', { count: stored.parts_count });
    return null;
  };

  /** A refusal read by its references, never by its sentence (R03). */
  function classify(e: Error): Refusal {
    const message = e.message;
    if (!(e instanceof ApiError)) return { kind: 'plain', message };
    if (e.code === 'variants_changed') return { kind: 'changed', message };
    const same = (id: number | undefined, tempId: string | undefined, ref: number | string) =>
      typeof ref === 'number' ? id === ref : tempId === ref;
    const option = e.refs?.option;
    if (option !== undefined) {
      for (const g of draft.groups) {
        const o = g.options.find((x) => same(x.id, x.tempId, option));
        if (o) return { kind: 'field', message, group: g.key, option: o.key };
      }
      const stored = typeof option === 'number' ? storedOption(option) : undefined;
      if (stored) return { kind: 'restore', message, target: 'option', id: stored.id, name: stored.name };
    }
    const group = e.refs?.group;
    if (group !== undefined) {
      const g = draft.groups.find((x) => same(x.id, x.tempId, group));
      if (g) return { kind: 'field', message, group: g.key };
      const stored = typeof group === 'number' ? storedGroup(group) : undefined;
      if (stored) return { kind: 'restore', message, target: 'group', id: stored.id, name: stored.name };
    }
    return { kind: 'plain', message };
  }

  // ⚠️ Synchronous: one press, one request; nothing closes the dialog under it.
  const sent = useRef(false);
  const mutation = useMutation({
    mutationKey: variantsMutationKey(product.id),
    mutationFn: (body: VariantsApply) => api.applyProductVariants(product.id, body),
    onSuccess: () => {
      // Order lines and stock positions name the options they chose — one helper decides the keys.
      invalidateProductVariants(queryClient, product.id);
      showToast(t('products.variantsDialog.saved'));
      onClose();
    },
    onError: (e: Error) => {
      sent.current = false;
      setRefusal(classify(e));
    },
  });

  const formId = useId();
  const submitId = `${formId}-submit`;
  const restoreId = `${formId}-restore`;

  // The focus after a refusal (D05 over J): the field it names, the «Bring back» action,
  // else the primary button.
  useEffect(() => {
    if (!refusal) return;
    const target =
      refusal.kind === 'field'
        ? refusal.option
          ? idOf(refusal.option, 'name')
          : idOf(refusal.group, 'name')
        : refusal.kind === 'restore'
          ? restoreId
          : submitId;
    document.getElementById(target)?.focus();
    // `idOf` is derived from `uid`, which never changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refusal, restoreId, submitId]);

  // The cursor starts in the first field (J): the first group's name, else «Add group».
  useEffect(() => {
    const first = draft.groups[0];
    document.getElementById(first ? idOf(first.key, 'name') : `${formId}-add-group`)?.focus();
    // Once, at the opening.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function close() {
    if (sent.current) return;
    onClose();
  }

  const pending = mutation.isPending;

  function submit() {
    if (sent.current || pending || errors.length > 0) return;
    if (sameDraft(draft, draftFromProduct(base))) {
      onClose();
      return;
    }
    setRefusal(null);
    sent.current = true;
    mutation.mutate(draftToApply(draft, base.variants_revision));
  }

  function bringBack(r: Extract<Refusal, { kind: 'restore' }>) {
    update((d) => (r.target === 'option' ? restoreOption(d, base, r.id) : restoreGroup(d, base, r.id)));
  }

  const slot =
    refusal == null ? undefined : refusal.kind === 'restore' ? (
      <span className="flex flex-wrap items-center gap-3">
        <span>
          {t(refusal.target === 'group' ? 'products.variantsDialog.refusedGroup' : 'products.variantsDialog.refusedOption', {
            name: refusal.name,
            reason: refusal.message,
          })}
        </span>
        <Button id={restoreId} variant="secondary" size="sm" onClick={() => bringBack(refusal)}>
          {t('products.variantsDialog.restore', { name: refusal.name })}
        </Button>
      </span>
    ) : refusal.kind === 'changed' ? (
      <span className="flex flex-wrap items-center gap-3">
        <span>{refusal.message}</span>
        <Button variant="secondary" size="sm" onClick={() => setReloading(true)}>
          {t('products.variantsDialog.reload')}
        </Button>
      </span>
    ) : (
      refusal.message
    );

  return (
    <>
      <WorkshopDialog
        size="lg"
        onClose={close}
        title={t('products.variantsDialog.title')}
        subtitle={`${product.code} · ${product.name}`}
        pending={pending}
        error={slot}
        footer={
          <>
            <Button type="button" variant="secondary" onClick={close} disabled={pending}>
              {t('common.cancel')}
            </Button>
            <Button id={submitId} type="submit" form={formId} disabled={pending || errors.length > 0}>
              {pending && <Loader2 className="w-4 h-4 animate-spin" />}
              {pending ? t('products.variantsDialog.saving') : t('products.variantsDialog.save')}
            </Button>
          </>
        }
      >
        <form
          id={formId}
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
          className="space-y-3"
        >
          <p className="text-xs text-bambu-gray">{t('products.variantsDialog.hint')}</p>
          {draft.groups.length === 0 && (
            <p className="text-sm text-bambu-gray">{t('products.variantsDialog.empty')}</p>
          )}
          {draft.groups.map((group) => {
            const groupErrors = errors.filter((e) => e.group === group.key && e.option === undefined);
            const nameBad =
              groupErrors.some((e) => e.code === 'groupNameEmpty' || e.code === 'groupNameTaken') ||
              (refusal?.kind === 'field' && refusal.group === group.key && refusal.option === undefined);
            const standardBad = groupErrors.some((e) => e.code === 'newGroupNoStandard' || e.code === 'standardMissing');
            const blocked = groupRefusal(group);
            const errorId = idOf(group.key, 'error');
            return (
              <div
                key={group.key}
                data-testid={`variant-group-${group.key}`}
                className="space-y-2 rounded-xl border border-bambu-dark-tertiary p-3"
              >
                <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] items-end gap-3 max-[761px]:grid-cols-1">
                  <div className="flex min-w-0 flex-col gap-1">
                    <label htmlFor={idOf(group.key, 'name')} className="text-sm text-bambu-gray-light">
                      {t('products.variantsDialog.group')}
                    </label>
                    <input
                      id={idOf(group.key, 'name')}
                      type="text"
                      value={group.name}
                      maxLength={NAME_MAX}
                      onChange={(e) => setGroup(group.key, { name: e.target.value })}
                      aria-invalid={nameBad || undefined}
                      aria-describedby={groupErrors.length > 0 ? errorId : undefined}
                      className={FIELD_CLASS}
                      disabled={pending}
                    />
                  </div>
                  <div className="flex min-w-0 flex-col gap-1">
                    <label htmlFor={idOf(group.key, 'standard')} className="text-sm text-bambu-gray-light">
                      {t('products.variantsDialog.standard')}
                    </label>
                    <Select
                      id={idOf(group.key, 'standard')}
                      value={group.standard ?? ''}
                      onChange={(e) => setGroup(group.key, { standard: e.target.value || null })}
                      aria-invalid={standardBad || undefined}
                      disabled={pending}
                      className="w-full"
                    >
                      {/* Only where it was stored so (A06): a group with a standard, and a new
                          one, never get «No standard». */}
                      {group.stored && !group.stored.hadStandard && (
                        <option value="">{t('products.variantsDialog.noStandard')}</option>
                      )}
                      {(!group.stored || group.stored.hadStandard) && group.standard == null && <option value="" />}
                      {group.options.map((o) => (
                        <option key={o.key} value={o.key}>
                          {o.name}
                        </option>
                      ))}
                    </Select>
                  </div>
                  <Button
                    type="button"
                    variant="ghost"
                    onClick={() => update((d) => ({ groups: d.groups.filter((g) => g.key !== group.key) }))}
                    disabled={pending || blocked !== null}
                    title={blocked ?? undefined}
                    aria-label={t('products.variants.deleteGroup', { name: group.name })}
                  >
                    {t('products.variantsDialog.deleteGroupLabel')}
                  </Button>
                </div>
                {blocked && <small className="block text-xs text-bambu-gray">{blocked}</small>}
                {/* «Every value needs its own part» (spec product-part-variant-combinations п. 10):
                    off by default — an optional add-on keeps its «no» orderable. */}
                <div className="flex min-w-0 flex-col gap-0.5">
                  <label
                    htmlFor={idOf(group.key, 'requires')}
                    className="inline-flex items-center gap-2 text-sm text-bambu-gray-light"
                  >
                    <input
                      id={idOf(group.key, 'requires')}
                      type="checkbox"
                      checked={group.requiresPart}
                      onChange={(e) => setGroup(group.key, { requiresPart: e.target.checked })}
                      aria-describedby={idOf(group.key, 'requires-hint')}
                      disabled={pending}
                      className="accent-bambu-green"
                    />
                    {t('products.variantsDialog.requiresPart')}
                  </label>
                  <small id={idOf(group.key, 'requires-hint')} className="block text-xs text-bambu-gray">
                    {t('products.variantsDialog.requiresPartHint')}
                  </small>
                </div>
                {groupErrors.length > 0 && (
                  <p id={errorId} className="text-xs text-red-600 dark:text-red-400">
                    {groupErrors.map((e) => t(`products.variantsDialog.errors.${e.code}`)).join(' ')}
                  </p>
                )}

                {group.options.length === 0 ? (
                  <p className="text-sm text-bambu-gray">
                    {t('products.variantsDialog.noOptions')}{' '}
                    <small className="text-xs">{t('products.variantsDialog.noOptionsHint')}</small>
                  </p>
                ) : (
                  <ul className="space-y-1.5">
                    {group.options.map((option, index) => {
                      const optionErrors = errors.filter((e) => e.group === group.key && e.option === option.key);
                      const marked =
                        optionErrors.length > 0 ||
                        (refusal?.kind === 'field' && refusal.option === option.key);
                      const reason = optionRefusal(group, option);
                      // Why «×» is shut, on screen and naming the button — a title on a disabled
                      // button reaches neither a keyboard nor a finger. The standard needs no
                      // line: its select says which option it is.
                      const usageReason = reason !== null && option.key !== group.standard ? reason : null;
                      const optionErrorId = idOf(option.key, 'error');
                      const reasonId = idOf(option.key, 'reason');
                      return (
                        <li key={option.key} className="space-y-1">
                          <div className="flex items-center gap-2">
                            <input
                              id={idOf(option.key, 'name')}
                              type="text"
                              value={option.name}
                              maxLength={NAME_MAX}
                              onChange={(e) => setOptionName(group.key, option.key, e.target.value)}
                              aria-label={t('products.variantsDialog.option', { index: index + 1, group: group.name })}
                              aria-invalid={marked || undefined}
                              aria-describedby={optionErrors.length > 0 ? optionErrorId : undefined}
                              className={`${FIELD_CLASS} min-w-0 flex-1`}
                              disabled={pending}
                            />
                            <button
                              type="button"
                              onClick={() =>
                                update((d) => ({
                                  groups: d.groups.map((g) =>
                                    g.key === group.key ? { ...g, options: g.options.filter((o) => o.key !== option.key) } : g,
                                  ),
                                }))
                              }
                              disabled={pending || reason !== null}
                              title={reason ?? undefined}
                              aria-describedby={usageReason ? reasonId : undefined}
                              aria-label={t('products.variants.deleteOption', { name: option.name })}
                              className="shrink-0 rounded-lg p-1.5 text-bambu-gray transition-colors hover:bg-bambu-dark hover:text-red-400 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-bambu-gray"
                            >
                              <X className="h-4 w-4" />
                            </button>
                          </div>
                          {/* Under the row, as the group's reason is under its name: beside the
                              field it squeezed the option's name on a phone. */}
                          {usageReason && (
                            <small id={reasonId} className="block text-xs text-bambu-gray">
                              {usageReason}
                            </small>
                          )}
                          {optionErrors.length > 0 && (
                            <p id={optionErrorId} className="text-xs text-red-600 dark:text-red-400">
                              {optionErrors.map((e) => t(`products.variantsDialog.errors.${e.code}`)).join(' ')}
                            </p>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                )}
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() =>
                    update((d) =>
                      addOption(d, group.key, t('products.variantsDialog.newOption', { index: group.options.length + 1 })),
                    )
                  }
                  disabled={pending}
                >
                  <Plus className="h-4 w-4" />
                  {t('products.variantsDialog.addOption')}
                </Button>
              </div>
            );
          })}
          <Button
            id={`${formId}-add-group`}
            type="button"
            variant="secondary"
            onClick={() =>
              update((d) =>
                addGroup(d, {
                  name: t('products.variantsDialog.newGroup'),
                  options: [1, 2].map((index) => t('products.variantsDialog.newOption', { index })),
                }),
              )
            }
            disabled={pending}
          >
            <Plus className="h-4 w-4" />
            {t('products.variantsDialog.addGroup')}
          </Button>
        </form>
      </WorkshopDialog>

      {reloading && (
        <ActionConfirm
          title={t('products.variantsDialog.reloadTitle')}
          body={t('products.variantsDialog.reloadBody')}
          primaryLabel={t('products.variantsDialog.reload')}
          send={async () => {
            // The draft is replaced only once the read succeeded — a failed read keeps it.
            const fresh = await api.getProduct(product.id);
            setBase(fresh);
            setDraft(draftFromProduct(fresh));
            setRefusal(null);
            void queryClient.invalidateQueries({ queryKey: ['product', product.id] });
          }}
          onClose={() => setReloading(false)}
        />
      )}
    </>
  );
}
