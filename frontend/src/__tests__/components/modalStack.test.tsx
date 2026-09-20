/**
 * The modal stack owns the ONE Escape listener in the app. Esc reaches the
 * topmost registered modal only — a ConfirmModal over PrintModal closes
 * alone, the form behind it survives.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, fireEvent, act, screen } from '@testing-library/react';
import { useRef, type ReactNode, type RefObject } from 'react';
import {
  register,
  unregister,
  positionOf,
  isAnyModalOpen,
  useIsAnyModalOpen,
  useModalStackEntry,
  ModalAncestryContext,
  _resetForTests,
  _stackDepthForTests,
  type ModalStackEntry,
} from '../../components/modalStack';

function entry(onClose = vi.fn(), closeDisabled = false): RefObject<ModalStackEntry> {
  return { current: { onClose, closeDisabled } };
}

/** A ref the stack can mark; tests that only care about #root pass one anyway. */
function overlayRef(): RefObject<HTMLElement | null> {
  return { current: null };
}

/** The element main.tsx renders into; the stack marks it inert while a modal is open. */
function mountRoot(): HTMLElement {
  const root = document.createElement('div');
  root.id = 'root';
  document.body.appendChild(root);
  return root;
}

function overlay(): RefObject<HTMLElement | null> {
  const el = document.createElement('div');
  document.body.appendChild(el);
  return { current: el };
}

describe('modalStack', () => {
  beforeEach(() => _resetForTests());
  afterEach(() => {
    cleanup();
    // A failing assertion skips a test's own `root.remove()`, and the leaked
    // node then fails `a missing #root is not an error` as collateral.
    document.getElementById('root')?.remove();
    _resetForTests();
  });

  it('Escape reaches only the topmost entry, then the next one once the top is gone', () => {
    const outer = vi.fn();
    const inner = vi.fn();
    register('a', [], entry(outer), overlayRef());
    register('b', [], entry(inner), overlayRef());

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(inner).toHaveBeenCalledTimes(1);
    expect(outer).not.toHaveBeenCalled();

    unregister('b');
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(outer).toHaveBeenCalledTimes(1);
    unregister('a');
  });

  it('a closeDisabled top swallows Escape without touching the one below', () => {
    const below = vi.fn();
    const top = vi.fn();
    register('a', [], entry(below), overlayRef());
    register('b', [], entry(top, true), overlayRef());

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(top).not.toHaveBeenCalled();
    expect(below).not.toHaveBeenCalled();
  });

  it('an Escape dispatched on document reaches the stack too', () => {
    const onClose = vi.fn();
    register('a', [], entry(onClose), overlayRef());
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('other keys are ignored', () => {
    const onClose = vi.fn();
    register('a', [], entry(onClose), overlayRef());
    fireEvent.keyDown(window, { key: 'Enter' });
    expect(onClose).not.toHaveBeenCalled();
  });

  it('the window listener is attached with the first entry and detached with the last', () => {
    const add = vi.spyOn(window, 'addEventListener');
    const remove = vi.spyOn(window, 'removeEventListener');
    const keydownCalls = (spy: typeof add) => spy.mock.calls.filter((c) => c[0] === 'keydown');

    register('a', [], entry(), overlayRef());
    register('b', [], entry(), overlayRef());
    expect(keydownCalls(add)).toHaveLength(1);

    unregister('a');
    expect(keydownCalls(remove)).toHaveLength(0);
    unregister('b');
    expect(keydownCalls(remove)).toHaveLength(1);

    add.mockRestore();
    remove.mockRestore();
  });

  it('a parent that registers after its child (same-commit mount) goes just below it', () => {
    // React runs a child's effects before its parent's, so a child modal
    // mounted in the same commit registers first. Ancestry, not order, decides.
    const onParentClose = vi.fn();
    const onChildClose = vi.fn();

    register('child', ['parent'], entry(onChildClose), overlayRef());
    register('parent', [], entry(onParentClose), overlayRef());

    expect(positionOf('parent')).toBe(0);
    expect(positionOf('child')).toBe(1);
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onChildClose).toHaveBeenCalledTimes(1);
    expect(onParentClose).not.toHaveBeenCalled();
  });

  it('a later top-level entry sits above an earlier nested chain', () => {
    // An app-level alert raised while "modal → nested confirm" is open must
    // be on top of both — this is what AlertModal's old z-[120] was for.
    const onA = vi.fn();
    const onC = vi.fn();
    const onB = vi.fn();
    register('a', [], entry(onA), overlayRef());
    register('c', ['a'], entry(onC), overlayRef());
    register('b', [], entry(onB), overlayRef());

    expect([positionOf('a'), positionOf('c'), positionOf('b')]).toEqual([0, 1, 2]);
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onB).toHaveBeenCalledTimes(1);
    expect(onC).not.toHaveBeenCalled();
    expect(onA).not.toHaveBeenCalled();
  });

  it('positionOf is -1 for a key that is not registered', () => {
    expect(positionOf('nope')).toBe(-1);
  });

  it('isAnyModalOpen and useIsAnyModalOpen follow the stack', () => {
    function Probe() {
      const open = useIsAnyModalOpen();
      return <span data-testid="probe">{open ? 'open' : 'closed'}</span>;
    }
    render(<Probe />);
    expect(isAnyModalOpen()).toBe(false);
    expect(screen.getByTestId('probe')).toHaveTextContent('closed');

    act(() => register('a', [], entry(), overlayRef()));
    expect(isAnyModalOpen()).toBe(true);
    expect(screen.getByTestId('probe')).toHaveTextContent('open');

    act(() => unregister('a'));
    expect(screen.getByTestId('probe')).toHaveTextContent('closed');
  });

  /** A stand-in for the shell: registers, shows its position, and passes its ancestry down. */
  function Host({
    label,
    onClose,
    disabled = false,
    children,
  }: {
    label: string;
    onClose: () => void;
    disabled?: boolean;
    children?: ReactNode;
  }) {
    const overlay = useRef<HTMLSpanElement>(null);
    const { position, childAncestry } = useModalStackEntry({ onClose, closeDisabled: disabled }, overlay);
    return (
      <ModalAncestryContext.Provider value={childAncestry}>
        <span ref={overlay} data-testid={label}>
          {position}
        </span>
        {children}
      </ModalAncestryContext.Provider>
    );
  }

  it('useModalStackEntry registers on mount, reads the latest props, unregisters on unmount', () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender, unmount } = render(<Host label="h" onClose={first} disabled={false} />);
    expect(_stackDepthForTests()).toBe(1);
    expect(screen.getByTestId('h')).toHaveTextContent('0');

    rerender(<Host label="h" onClose={first} disabled />);
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(first).not.toHaveBeenCalled();

    rerender(<Host label="h" onClose={second} disabled={false} />);
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);

    unmount();
    expect(_stackDepthForTests()).toBe(0);
  });

  it('a Host nested in a Host, mounted in one render, is above its parent and shows position 1', () => {
    const parent = vi.fn();
    const child = vi.fn();
    render(
      <Host label="p" onClose={parent}>
        <Host label="c" onClose={child} />
      </Host>,
    );
    expect(screen.getByTestId('p')).toHaveTextContent('0');
    expect(screen.getByTestId('c')).toHaveTextContent('1');

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(child).toHaveBeenCalledTimes(1);
    expect(parent).not.toHaveBeenCalled();
  });

  it('a Host mounted later, outside the chain, is on top of it and its position updates live', () => {
    const chainTop = vi.fn();
    const later = vi.fn();
    render(
      <Host label="p" onClose={vi.fn()}>
        <Host label="c" onClose={chainTop} />
      </Host>,
    );
    render(<Host label="later" onClose={later} />);
    expect(screen.getByTestId('later')).toHaveTextContent('2');

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(later).toHaveBeenCalledTimes(1);
    expect(chainTop).not.toHaveBeenCalled();
  });

  describe('inert ownership', () => {
    it('#root is inert exactly while the stack is non-empty', () => {
      const root = mountRoot();
      expect(root.hasAttribute('inert')).toBe(false);

      register('a', [], entry(), overlayRef());
      expect(root.hasAttribute('inert')).toBe(true);
      register('b', [], entry(), overlayRef());
      expect(root.hasAttribute('inert')).toBe(true);

      unregister('a');
      expect(root.hasAttribute('inert')).toBe(true);
      unregister('b');
      expect(root.hasAttribute('inert')).toBe(false);
      root.remove();
    });

    it('a missing #root is not an error', () => {
      expect(document.getElementById('root')).toBeNull();
      expect(() => {
        register('a', [], entry(), overlayRef());
        unregister('a');
      }).not.toThrow();
    });

    it('only the topmost overlay is live, and the next one becomes live synchronously', () => {
      const a = overlay();
      const b = overlay();
      const c = overlay();
      register('a', [], entry(), a);
      register('b', [], entry(), b);
      register('c', [], entry(), c);
      expect(a.current!.hasAttribute('inert')).toBe(true);
      expect(b.current!.hasAttribute('inert')).toBe(true);
      expect(c.current!.hasAttribute('inert')).toBe(false);

      unregister('c');
      // No act(), no await: useDialogFocus returns focus in the SAME cleanup
      // pass, so the modal below must already be live here.
      expect(b.current!.hasAttribute('inert')).toBe(false);
      expect(a.current!.hasAttribute('inert')).toBe(true);

      unregister('b');
      expect(a.current!.hasAttribute('inert')).toBe(false);
      unregister('a');
      for (const r of [a, b, c]) r.current!.remove();
    });

    it('an ancestor registering after its descendant is inert, the descendant stays live', () => {
      const parent = overlay();
      const child = overlay();
      register('child', ['parent'], entry(), child);
      register('parent', [], entry(), parent);
      expect(parent.current!.hasAttribute('inert')).toBe(true);
      expect(child.current!.hasAttribute('inert')).toBe(false);
      unregister('child');
      unregister('parent');
      parent.current!.remove();
      child.current!.remove();
    });

    it('_resetForTests clears inert from #root', () => {
      const root = mountRoot();
      register('a', [], entry(), overlayRef());
      expect(root.hasAttribute('inert')).toBe(true);
      _resetForTests();
      expect(root.hasAttribute('inert')).toBe(false);
      root.remove();
    });

    it('useModalStackEntry passes its overlay to the stack', () => {
      // The inner Shell is a DOM child of the outer one here; real modals are body siblings. jsdom does not inherit inert, so the child-shape is safe for asserting which element got the attribute.
      function Shell({ label, children }: { label: string; children?: ReactNode }) {
        const ref = useRef<HTMLDivElement>(null);
        const { childAncestry } = useModalStackEntry({ onClose: vi.fn(), closeDisabled: false }, ref);
        return (
          <ModalAncestryContext.Provider value={childAncestry}>
            <div ref={ref} data-testid={label}>
              {children}
            </div>
          </ModalAncestryContext.Provider>
        );
      }
      const { rerender } = render(
        <Shell label="outer">
          <Shell label="inner" />
        </Shell>,
      );
      expect(screen.getByTestId('outer')).toHaveAttribute('inert');
      expect(screen.getByTestId('inner')).not.toHaveAttribute('inert');

      rerender(<Shell label="outer" />);
      expect(screen.getByTestId('outer')).not.toHaveAttribute('inert');
    });
  });
});
