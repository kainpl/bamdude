import { useEffect, useRef, useState, type ReactNode, type RefObject } from 'react';
import { useNavigate } from 'react-router';
import { useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api, ApiError } from '../../../api/client';
import { useAuth } from '../../../contexts/AuthContext';
import { useToast } from '../../../contexts/ToastContext';
import {
  invalidateAfterDelete,
  invalidateOrderViews,
  invalidateProductCatalog,
  refreshPartImageRefs,
} from '../../../utils/queryInvalidation';
import { useFocusWhenRowLeaves } from '../../../hooks/useFocusWhenRowLeaves';
import { AddToOrderDialog } from '../../projects/add-to-order/AddToOrderDialog';
import { copyName } from './copyName';
import { ProductConfirm, type ProductConfirmKind } from './ProductConfirm';
import type { ProductAction, ProductRef } from './productRef';

export type { ProductAction, ProductRef } from './productRef';

/** What a page hands its doors (a menu, the header, the one-off banner). */
export interface ProductActionsHost<P extends ProductRef = ProductRef> {
  /** The actions this product offers to this user, in the menu's order (F02). */
  available: (p: P) => ProductAction[];
  run: (action: ProductAction, p: P) => void;
  /** The request running for this product, if any — its door says so and sends nothing more. */
  pending: (p: P) => ProductAction | null;
  /** The confirmations and «To order…» — mounted here and nowhere else. */
  host: ReactNode;
}

type Active<P> = { kind: 'confirm'; which: ProductConfirmKind; ref: P } | { kind: 'toOrder'; ref: P };

/**
 * The ONE host of a product's actions (WS-13 E8 F01, after E6-B01): the catalog's menu
 * (card and row), the product page's header buttons and the one-off banner all run
 * through it — the same rights, the same refusals, the same invalidations wherever an
 * action starts. Each action keeps the invalidation it always had (R06): hide / show →
 * the order views; duplicate → the catalog; add to catalog → the product and the catalog;
 * delete → by `context` — from the catalog the lists AND the deleted product's own entry
 * (nobody observes it); from the detail the lists only, and the page finishes the job
 * (`onDeleted`: forget its entry on unmount and leave), so the deleted id is never read
 * again.
 *
 * ⚠️ **It outlives its doors (F08).** A confirmation holds a snapshot of the product it
 * was opened for; a re-read that drops the row changes nothing about the open dialog.
 * When it closes, the focus goes back to the element that opened it while that is still
 * on the page, else to the page's heading (`fallbackFocusRef`) — never BODY (F09).
 *
 * One request per product at a time: a door shows `pending` and a second click sends
 * nothing (the ref is what makes it hold within one tick).
 */
export function useProductActions<P extends ProductRef>({
  context,
  onEdit,
  onDeleted,
  fallbackFocusRef,
}: {
  context: 'catalog' | 'detail';
  onEdit: (p: P) => void;
  /** `detail` only: forget the product's entry on unmount and go to the catalog. */
  onDeleted?: (p: P) => void;
  fallbackFocusRef: RefObject<HTMLElement | null>;
}): ProductActionsHost<P> {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [active, setActive] = useState<Active<P> | null>(null);
  const [running, setRunning] = useState<Record<number, ProductAction>>({});
  const runningRef = useRef(new Map<number, ProductAction>());
  const opened = useRef(false);
  const opener = useRef<HTMLElement | null>(null);
  // A delete takes the row away: the heading gets the focus whatever the Modal gave back (F09).
  const toHeading = useRef(false);
  // The one watch for a row an action may take away (E8-V02; WS-13 E9 B11 shares it).
  const keepFocusWhenRowLeaves = useFocusWhenRowLeaves(fallbackFocusRef);

  const open = (next: Active<P>) => {
    if (opener.current == null) {
      const focused = document.activeElement;
      opener.current = focused instanceof HTMLElement && focused !== document.body ? focused : null;
    }
    setActive(next);
  };

  // The dialog has closed: focus goes back to its opener while it is on the page, else to
  // the page's heading — late enough for the Modal's own return to have run (E6-B07).
  useEffect(() => {
    if (active) {
      opened.current = true;
      return;
    }
    if (!opened.current) return;
    opened.current = false;
    const back = opener.current;
    opener.current = null;
    const timer = window.setTimeout(() => {
      if (toHeading.current) {
        toHeading.current = false;
        fallbackFocusRef.current?.focus();
        return;
      }
      const now = document.activeElement;
      if (now != null && now !== document.body) return;
      const usable = back != null && back.isConnected && !back.closest('[inert]') && !(back as HTMLButtonElement).disabled;
      (usable ? back : fallbackFocusRef.current)?.focus();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [active, fallbackFocusRef]);


  /** Runs one request for a product; a second one while it runs is not sent. */
  const once = (p: P, action: ProductAction, request: () => Promise<unknown>) => {
    if (runningRef.current.has(p.id)) return;
    runningRef.current.set(p.id, action);
    setRunning((now) => ({ ...now, [p.id]: action }));
    request().finally(() => {
      runningRef.current.delete(p.id);
      setRunning((now) => {
        const next = { ...now };
        delete next[p.id];
        return next;
      });
    });
  };

  const failed = (e: unknown) => showToast((e as Error).message, 'error');

  const available: ProductActionsHost<P>['available'] = (p) => {
    const catalog = p.origin === 'catalog';
    const update = hasPermission('products:update');
    const out: ProductAction[] = [];
    if (update) out.push('edit');
    if (hasPermission('orders:update') && catalog && p.is_active) out.push('toOrder');
    if (hasPermission('products:create')) out.push('duplicate');
    if (hasPermission('products:read')) out.push('export');
    if (update && catalog) out.push(p.is_active ? 'hide' : 'show');
    if (update && !catalog) out.push('promote');
    if (hasPermission('products:delete')) out.push('delete');
    return out;
  };

  const run: ProductActionsHost<P>['run'] = (action, p) => {
    switch (action) {
      case 'edit':
        onEdit(p);
        return;
      case 'toOrder':
        open({ kind: 'toOrder', ref: p });
        return;
      case 'promote':
      case 'delete':
        open({ kind: 'confirm', which: action, ref: p });
        return;
      case 'duplicate':
        // F03: the copy's name is ours, localized and cut to fit — the server's own
        // English fallback is never relied on.
        once(p, action, () =>
          api.duplicateProduct(p.id, copyName(p.name, ` ${t('products.actions.copySuffix')}`)).then((saved) => {
            // No order view moves: the copy is a brand-new product no order line names yet.
            invalidateProductCatalog(queryClient);
            showToast(t('products.toast.duplicated'));
            navigate(`/products/${saved.id}`);
          }, failed),
        );
        return;
      case 'export':
        // Not a mutation: nothing on the page changes, and a failed download says so
        // where the operator clicked.
        once(p, action, () =>
          api.downloadProductExport(p.id).catch((e: unknown) =>
            showToast(
              e instanceof ApiError ? t('products.toast.exportFailed', { status: e.status }) : (e as Error).message,
              'error',
            ),
          ),
        );
        return;
      case 'hide':
      case 'show': {
        // The menu has just given focus back to its trigger.
        const trigger = document.activeElement;
        // `is_active` is refused as an explicit null (422), so the boolean is always sent.
        once(p, action, () =>
          api.updateProduct(p.id, { is_active: action === 'show' }).then((saved) => {
            // The order views, which include the product keys (Ruling 29): a product that
            // leaves the catalog is still on the lines of every order that ordered it.
            invalidateOrderViews(queryClient);
            showToast(saved.is_active ? t('products.toast.shown') : t('products.toast.hidden'));
            keepFocusWhenRowLeaves(trigger);
          }, failed),
        );
        return;
      }
    }
  };

  const send = (which: ProductConfirmKind, p: P) => async () => {
    if (which === 'promote') {
      // `origin` alone: promoting a one-off never changes whether it is listed (F06).
      await api.updateProduct(p.id, { origin: 'catalog' });
      queryClient.invalidateQueries({ queryKey: ['product', p.id] });
      invalidateProductCatalog(queryClient);
      showToast(t('products.toast.promoted'));
      return;
    }
    await api.deleteProduct(p.id);
    // The row goes with the product: focus returns to the heading, not to a trigger about to vanish (F09).
    toHeading.current = true;
    if (context === 'catalog') {
      invalidateAfterDelete(queryClient, 'product', p.id);
    } else {
      invalidateAfterDelete(queryClient, 'product');
      onDeleted?.(p);
    }
    // Its parts' ids are free again (spec procurement-stock-images A16): their cached pictures go too.
    refreshPartImageRefs(queryClient, [p.id]);
    showToast(t('products.toast.deleted'));
  };

  const close = () => setActive(null);
  let host: ReactNode = null;
  if (active?.kind === 'confirm') {
    host = (
      <ProductConfirm
        key={`${active.which}-${active.ref.id}`}
        kind={active.which}
        product={active.ref}
        send={send(active.which, active.ref)}
        onClose={close}
      />
    );
  } else if (active?.kind === 'toOrder') {
    // The row or the page already knows the product's groups: the dialog asks its combination
    // at once instead of waiting for a catalog page to list it (review M-h).
    const groups = active.ref.variant_groups;
    host = (
      <AddToOrderDialog
        preselectProduct={{
          id: active.ref.id,
          code: active.ref.code,
          name: active.ref.name,
          hasGroups: groups ? groups.length > 0 : undefined,
        }}
        onClose={close}
      />
    );
  }

  return {
    available,
    run,
    pending: (p) => running[p.id] ?? null,
    host,
  };
}
