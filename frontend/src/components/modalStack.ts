/**
 * The modal stack — the ONE place Escape is handled for modals.
 *
 * Every <Modal> registers here on mount and unregisters on unmount. One
 * `keydown` listener on `window` (attached with the first entry, detached
 * with the last) closes the TOPMOST entry only, so nested modals close one
 * per keypress, innermost first. Before this, 66 files each had their own
 * `window` listener and a ConfirmModal over PrintModal closed both at once.
 *
 * ⚠️ `window`, not `document`: an event dispatched on `window` never reaches a
 * `document` listener, while everything that reaches `document` bubbles on to
 * `window`. Tests dispatch on both.
 *
 * ⚠️ Order is MOUNT ORDER with one correction. React runs a child's effects
 * before its parent's, so a parent and child modal mounted in ONE commit
 * register child-first — and within one commit React also places a portal's
 * children in post-order, so they land in `body` child-first: neither
 * registration order nor DOM order can be trusted there. Every entry carries
 * the keys of the shells enclosing it (`ModalAncestryContext`); an entry that
 * turns out to be an ANCESTOR of one already registered is inserted just
 * below that descendant. Everything else is appended, so an app-level alert
 * raised over "modal → nested confirm" lands on top of both — which is what
 * AlertModal's old `z-[120]` existed for. (Ordering by nesting depth instead
 * would bury that alert under the confirm.) z-index is 50 + stack position,
 * so paint order follows the stack, not the DOM.
 *
 * Entries are read through a ref at keypress time, so a modal whose
 * `onClose` or `closeDisabled` changed after mount never has a stale
 * closure called.
 *
 * The stack also owns every `inert` attribute of the modal layers — it is the
 * same fact, "which layer is live", written into the DOM. While the stack is
 * non-empty `#root` (the element main.tsx renders into) is inert, and so is
 * every registered overlay except the topmost; that is the whole focus trap.
 * Tab cannot reach the page because nothing under #root can take focus, with
 * no key handler of our own; the toast viewport and popovers portalled into
 * body are deliberately live. Every modal is portalled into body for the same
 * reason (see contexts/ToastContext.tsx for the toast side).
 *
 * The overlay ref is REQUIRED of every caller, so a new modal cannot silently
 * opt out of the trap and stay live under the one above it; the ref itself may
 * of course still hold `null` before its first mount, hence `current?.`.
 *
 * ⚠️ The attributes are written imperatively, in `register`/`unregister`,
 * BEFORE `notify()` — never as a React prop. `useDialogFocus` returns focus
 * to the opener in the same effect-cleanup pass in which the modal
 * unregisters, and `focus()` on an element inside an inert subtree silently
 * does nothing; a prop would land one commit too late. That is also why
 * `Modal.tsx` calls `useModalStackEntry` before `useDialogFocus`: React runs
 * a component's cleanups in declaration order.
 */
import {
  createContext,
  useContext,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useSyncExternalStore,
  type RefObject,
} from 'react';

export interface ModalStackEntry {
  onClose: () => void;
  closeDisabled: boolean;
}

interface Registered {
  key: string;
  ancestors: readonly string[];
  entry: RefObject<ModalStackEntry>;
  /** The outermost portalled element; inert unless topmost. Read at apply time, never cached. */
  overlay: RefObject<HTMLElement | null>;
}

/** Keys of the <Modal>s enclosing the current subtree, outermost first. The shell provides its `childAncestry`. */
export const ModalAncestryContext = createContext<readonly string[]>([]);

let stack: Registered[] = [];
const subscribers = new Set<() => void>();

function notify(): void {
  for (const fn of subscribers) fn();
}

/** The rule: the page (#root) is inert while any modal is open; of the registered overlays only the topmost is live. */
function applyInert(): void {
  document.getElementById('root')?.toggleAttribute('inert', stack.length > 0);
  stack.forEach((r, i) => r.overlay.current?.toggleAttribute('inert', i !== stack.length - 1));
}

function onKeyDown(e: KeyboardEvent): void {
  if (e.key !== 'Escape') return;
  const top = stack[stack.length - 1];
  if (!top) return;
  const entry = top.entry.current;
  if (entry.closeDisabled) return;
  e.preventDefault();
  e.stopPropagation();
  entry.onClose();
}

export function register(
  key: string,
  ancestors: readonly string[],
  entry: RefObject<ModalStackEntry>,
  overlay: RefObject<HTMLElement | null>,
): void {
  // Mount order — unless a descendant is already here (a same-commit mount
  // registers child-first): then this one goes just below it.
  const descendant = stack.findIndex((r) => r.ancestors.includes(key));
  const index = descendant === -1 ? stack.length : descendant;
  const wasEmpty = stack.length === 0;
  stack = [...stack.slice(0, index), { key, ancestors, entry, overlay }, ...stack.slice(index)];
  if (wasEmpty) window.addEventListener('keydown', onKeyDown);
  applyInert();
  notify();
}

export function unregister(key: string): void {
  const leaving = stack.find((r) => r.key === key);
  stack = stack.filter((r) => r.key !== key);
  if (stack.length === 0) window.removeEventListener('keydown', onKeyDown);
  // On a real unmount this is dormant — React nulls host refs in the
  // mutation phase, before passive cleanups run — and the node leaves the
  // DOM with its attribute. It matters when the effect re-runs for a
  // still-mounted modal (a caller passing a different ref object than before
  // — the old ref still points at a live node). Keep it here, in the passive
  // path: a layout effect would run before the focus return and break the
  // cleanup order useDialogFocus relies on.
  leaving?.overlay.current?.removeAttribute('inert');
  applyInert();
  notify();
}

/** 0 = bottom; −1 when the key is not registered. */
export function positionOf(key: string): number {
  return stack.findIndex((r) => r.key === key);
}

export function isAnyModalOpen(): boolean {
  return stack.length > 0;
}

function subscribe(fn: () => void): () => void {
  subscribers.add(fn);
  return () => {
    subscribers.delete(fn);
  };
}

/** Re-renders when the first modal opens or the last one closes. */
export function useIsAnyModalOpen(): boolean {
  return useSyncExternalStore(subscribe, isAnyModalOpen, () => false);
}

export interface ModalStackPlacement {
  /** This modal's index in the stack, bottom = 0. Before the registering effect has run it is the nesting depth — a fair guess for one frame. */
  position: number;
  /** What this modal's children must receive as `ModalAncestryContext`. Stable across renders. */
  childAncestry: readonly string[];
}

/**
 * Attach the calling modal to the stack for its lifetime and report where it
 * sits. The key is the component's `useId`; the ancestry is whatever
 * `ModalAncestryContext` says encloses it.
 */
export function useModalStackEntry(
  entry: ModalStackEntry,
  overlay: RefObject<HTMLElement | null>,
): ModalStackPlacement {
  const key = useId();
  const ancestors = useContext(ModalAncestryContext);
  const latest = useRef<ModalStackEntry>(entry);
  useLayoutEffect(() => {
    latest.current = entry;
  });
  useEffect(() => {
    register(key, ancestors, latest, overlay);
    return () => unregister(key);
  }, [key, ancestors, overlay]);
  const registered = useSyncExternalStore(subscribe, () => positionOf(key), () => -1);
  const position = registered === -1 ? ancestors.length : registered;
  const childAncestry = useMemo(() => [...ancestors, key], [ancestors, key]);
  return { position, childAncestry };
}

export function _resetForTests(): void {
  for (const r of stack) r.overlay.current?.removeAttribute('inert');
  stack = [];
  window.removeEventListener('keydown', onKeyDown);
  applyInert();
  notify();
}

export function _stackDepthForTests(): number {
  return stack.length;
}
