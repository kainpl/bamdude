/**
 * The modal shell. A modal closes through its X, its own buttons, or Esc —
 * never through a click outside. The `lightbox` variant is the one exception.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { useState } from 'react';
import { screen, fireEvent, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { Modal, MODAL_SIZE_CLASS, type ModalSize } from '../../components/Modal';
import { CardActionMenu, CardActionMenuItem } from '../../components/CardActionMenu';
import { _resetForTests } from '../../components/modalStack';

describe('Modal', () => {
  afterEach(() => {
    cleanup();
    // A failing assertion skips a test's own `root.remove()`; a leftover #root
    // would make `the opener is read before #root becomes inert` hold a second
    // root nobody marks inert, and pass vacuously.
    document.getElementById('root')?.remove();
    _resetForTests();
  });

  it('renders the title and the body inside a labelled dialog', () => {
    render(
      <Modal onClose={vi.fn()} title="Hello">
        <p>body</p>
      </Modal>,
    );
    const dialog = screen.getByRole('dialog', { name: 'Hello' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(screen.getByText('body')).toBeInTheDocument();
  });

  it('the header X calls onClose', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<Modal onClose={onClose} title="T">x</Modal>);
    await user.click(screen.getByRole('button', { name: 'Close' }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('Escape calls onClose', () => {
    const onClose = vi.fn();
    render(<Modal onClose={onClose} title="T">x</Modal>);
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('a click on the backdrop does NOT call onClose', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<Modal onClose={onClose} title="T">x</Modal>);
    const overlay = screen.getByRole('dialog').parentElement as HTMLElement;
    await user.click(overlay);
    expect(onClose).not.toHaveBeenCalled();
  });

  it('closeDisabled blocks both the X and Escape', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<Modal onClose={onClose} title="T" closeDisabled>x</Modal>);
    const x = screen.getByRole('button', { name: 'Close' });
    expect(x).toBeDisabled();
    await user.click(x);
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).not.toHaveBeenCalled();
  });

  it('hideClose renders no X', () => {
    render(<Modal onClose={vi.fn()} title="T" hideClose>x</Modal>);
    expect(screen.queryByRole('button', { name: 'Close' })).toBeNull();
  });

  it('without a title the X still renders — nothing is Esc-only', () => {
    render(<Modal onClose={vi.fn()}>x</Modal>);
    expect(screen.getByRole('button', { name: 'Close' })).toBeInTheDocument();
  });

  it('a custom header is labelled through labelledBy', () => {
    render(
      <Modal onClose={vi.fn()} header={<h2 id="custom-h">Custom</h2>} labelledBy="custom-h">
        x
      </Modal>,
    );
    expect(screen.getByRole('dialog', { name: 'Custom' })).toBeInTheDocument();
  });

  it('renders the footer slot', () => {
    render(
      <Modal onClose={vi.fn()} title="T" footer={<button>Save</button>}>
        x
      </Modal>,
    );
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument();
  });

  it.each(Object.entries(MODAL_SIZE_CLASS))('size %s puts "%s" on the panel', (size, classes) => {
    render(
      <Modal onClose={vi.fn()} title="T" size={size as ModalSize}>
        x
      </Modal>,
    );
    const panel = screen.getByRole('dialog');
    for (const c of classes.split(' ')) expect(panel).toHaveClass(c);
    if (size === 'full') expect(panel).not.toHaveClass('max-h-[90vh]');
    else expect(panel).toHaveClass('max-h-[90vh]');
    cleanup();
    _resetForTests();
  });

  it('index.css declares --container-8xl, without which max-w-8xl does not exist in Tailwind 4', () => {
    const css = readFileSync(join(process.cwd(), 'src/index.css'), 'utf8');
    expect(css).toMatch(/--container-8xl:\s*88rem/);
  });

  it('lightbox: a click on the ground closes, a click on the content does not, and there is no X', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(
      <Modal variant="lightbox" onClose={onClose} ariaLabel="Preview">
        <img alt="big" src="x.png" />
      </Modal>,
    );
    const ground = screen.getByRole('dialog', { name: 'Preview' });
    expect(screen.queryByRole('button', { name: 'Close' })).toBeNull();

    await user.click(screen.getByRole('img', { name: 'big' }));
    expect(onClose).not.toHaveBeenCalled();

    await user.click(ground);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('focus moves into the dialog on mount and returns to the opener on unmount', () => {
    const opener = document.createElement('button');
    document.body.appendChild(opener);
    opener.focus();
    const { unmount } = render(<Modal onClose={vi.fn()} title="T">x</Modal>);
    expect(document.activeElement).toBe(screen.getByRole('dialog'));
    unmount();
    expect(document.activeElement).toBe(opener);
    opener.remove();
  });

  describe('focus trap through inert', () => {
    function mountRoot(): HTMLElement {
      const root = document.createElement('div');
      root.id = 'root';
      document.body.appendChild(root);
      return root;
    }

    it('#root is inert while the modal is open and live again before focus returns to the opener', () => {
      const root = mountRoot();
      const opener = document.createElement('button');
      root.appendChild(opener);
      opener.focus();
      // jsdom does not refuse focus() inside an inert subtree, so the test
      // pins the ORDER: when the opener is focused back, #root must already
      // be live, or in a real browser the call would be a silent no-op.
      // ⚠️ defineProperty, not `opener.focus = …`: the first userEvent.setup()
      // in this file replaces HTMLElement.prototype.focus with a getter-only
      // accessor (user-event's patchFocus), so a plain assignment throws.
      const rootWasLiveWhenFocused: boolean[] = [];
      const nativeFocus = opener.focus.bind(opener);
      Object.defineProperty(opener, 'focus', {
        configurable: true,
        value: () => {
          rootWasLiveWhenFocused.push(!root.hasAttribute('inert'));
          nativeFocus();
        },
      });

      const { unmount } = render(<Modal onClose={vi.fn()} title="T">x</Modal>);
      expect(root).toHaveAttribute('inert');
      expect(screen.getByRole('dialog').closest('[inert]')).toBeNull();

      unmount();
      expect(root).not.toHaveAttribute('inert');
      expect(document.activeElement).toBe(opener);
      expect(rootWasLiveWhenFocused).toEqual([true]);
      root.remove();
    });

    it('a modal under another modal is inert, and live again before focus returns into it', () => {
      mountRoot();
      function Outer() {
        const [confirm, setConfirm] = useState(false);
        return (
          <Modal onClose={vi.fn()} title="Outer">
            <button onClick={() => setConfirm(true)}>open confirm</button>
            {confirm && (
              <Modal onClose={() => setConfirm(false)} title="Confirm">
                <button onClick={() => setConfirm(false)}>done</button>
              </Modal>
            )}
          </Modal>
        );
      }
      render(<Outer />);
      const openConfirm = screen.getByText('open confirm');
      const outerOverlay = screen.getByRole('dialog', { name: 'Outer' }).parentElement!;
      const outerWasLiveWhenFocused: boolean[] = [];
      const nativeFocus = openConfirm.focus.bind(openConfirm);
      // defineProperty for the same reason as above: patchFocus made the
      // prototype's `focus` a getter with no setter.
      Object.defineProperty(openConfirm, 'focus', {
        configurable: true,
        value: () => {
          outerWasLiveWhenFocused.push(!outerOverlay.hasAttribute('inert'));
          nativeFocus();
        },
      });

      openConfirm.focus();
      fireEvent.click(openConfirm);
      expect(outerOverlay).toHaveAttribute('inert');
      const inner = screen.getByRole('dialog', { name: 'Confirm' });
      expect(inner.parentElement).not.toHaveAttribute('inert');
      expect(document.activeElement).toBe(inner);

      fireEvent.click(screen.getByText('done'));
      expect(screen.queryByRole('dialog', { name: 'Confirm' })).toBeNull();
      expect(outerOverlay).not.toHaveAttribute('inert');
      expect(document.activeElement).toBe(openConfirm);
      // One call from the test itself (before the confirm opened), one from
      // useDialogFocus returning focus — that one must see a live outer modal.
      expect(outerWasLiveWhenFocused).toEqual([true, true]);
      document.getElementById('root')!.remove();
    });

    it('a lightbox under a dialog is inert like any other modal', () => {
      render(
        <Modal onClose={vi.fn()} variant="lightbox" ariaLabel="Picture">
          <img alt="" />
          <Modal onClose={vi.fn()} title="Over it">x</Modal>
        </Modal>,
      );
      expect(screen.getByRole('dialog', { name: 'Picture' })).toHaveAttribute('inert');
      expect(screen.getByRole('dialog', { name: 'Over it' }).parentElement).not.toHaveAttribute('inert');
    });

    it('the opener is read before #root becomes inert', () => {
      const root = mountRoot();
      const opener = document.createElement('button');
      root.appendChild(opener);
      opener.focus();
      // jsdom has no focus-fixup rule, so the test pins the ORDER instead:
      // every read of document.activeElement the HOOK makes while the modal
      // mounts must see a live #root — in a browser whose fixup ran
      // synchronously, a read after the stack marked #root inert would
      // already return <body>.
      // ⚠️ The hook's reads only: react-dom reads activeElement itself
      // (`getActiveElementDeep`, for selection restore) in every commit,
      // including the one the stack's own `notify()` schedules — which by
      // definition runs after #root is inert. Those are React's bookkeeping,
      // not the opener, so the stack frame is what separates them.
      const desc = Object.getOwnPropertyDescriptor(Document.prototype, 'activeElement')!;
      const rootLiveAtRead: boolean[] = [];
      Object.defineProperty(document, 'activeElement', {
        configurable: true,
        get() {
          if (new Error().stack?.includes('useDialogFocus')) rootLiveAtRead.push(!root.hasAttribute('inert'));
          return desc.get!.call(document);
        },
      });
      try {
        const { unmount } = render(<Modal onClose={vi.fn()} title="T">x</Modal>);
        expect(rootLiveAtRead.length).toBeGreaterThan(0);
        expect(rootLiveAtRead).not.toContain(false);
        unmount();
        expect(document.activeElement).toBe(opener);
      } finally {
        delete (document as { activeElement?: unknown }).activeElement;
        root.remove();
      }
    });

    it('an outer modal unmounting together with its nested modal still returns focus to the opener', async () => {
      const root = mountRoot();
      const opener = document.createElement('button');
      root.appendChild(opener);
      opener.focus();
      // Same spy as the first trap test — defineProperty because patchFocus
      // left the prototype's `focus` a getter with no setter. It records
      // whether the opener was OUT of every inert subtree when focus was
      // handed back: in a browser a focus() inside one is a silent no-op.
      const openerLiveWhenFocused: boolean[] = [];
      const nativeFocus = opener.focus.bind(opener);
      Object.defineProperty(opener, 'focus', {
        configurable: true,
        value: () => {
          openerLiveWhenFocused.push(!opener.closest('[inert]'));
          nativeFocus();
        },
      });

      function Host() {
        const [open, setOpen] = useState(true);
        const [confirm, setConfirm] = useState(false);
        if (!open) return null;
        return (
          <Modal onClose={() => setOpen(false)} title="Outer">
            <button onClick={() => setConfirm(true)}>open confirm</button>
            {confirm && (
              <Modal onClose={() => setConfirm(false)} title="Confirm">
                <button onClick={() => setOpen(false)}>discard</button>
              </Modal>
            )}
          </Modal>
        );
      }
      render(<Host />);
      fireEvent.click(screen.getByText('open confirm'));
      expect(root).toHaveAttribute('inert');
      // One click unmounts both modals in one commit: React cleans a deleted
      // subtree top-down, so the outer modal's focus return runs before the
      // inner one has unregistered and #root is still inert at that moment.
      fireEvent.click(screen.getByText('discard'));
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(root).not.toHaveAttribute('inert');
      await Promise.resolve();
      expect(document.activeElement).toBe(opener);
      expect(openerLiveWhenFocused).toEqual([true]);
      root.remove();
    });

    it('a dialog opened from a card menu returns focus to the menu trigger', () => {
      // The end-to-end pin. The menu is portalled onto `body` and unmounts with
      // the very click that opens the dialog, so the item this hook would
      // otherwise remember as the opener is detached by the time focus comes
      // back. The menu refocusing its trigger in the handler — before React
      // commits — is what leaves a live element for the layout-effect capture.
      function Host() {
        const [open, setOpen] = useState(false);
        return (
          <>
            <CardActionMenu label="Actions" testId="card-menu">
              {(close) => (
                <CardActionMenuItem
                  onSelect={() => {
                    close();
                    setOpen(true);
                  }}
                >
                  Rename
                </CardActionMenuItem>
              )}
            </CardActionMenu>
            {open && (
              <Modal onClose={() => setOpen(false)} title="Rename">
                <button>save</button>
              </Modal>
            )}
          </>
        );
      }
      render(<Host />);
      const trigger = screen.getByTestId('card-menu');
      fireEvent.click(trigger);
      fireEvent.click(screen.getByRole('menuitem', { name: 'Rename' }));

      expect(screen.queryByRole('menu')).toBeNull();
      expect(document.activeElement).toBe(screen.getByRole('dialog', { name: 'Rename' }));

      fireEvent.keyDown(window, { key: 'Escape' });
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(document.activeElement).toBe(trigger);
    });
  });

  it('two nested modals: Escape closes the inner first, then the outer', () => {
    const outer = vi.fn();
    const inner = vi.fn();
    const { rerender } = render(
      <Modal onClose={outer} title="Outer">
        <Modal onClose={inner} title="Inner">
          i
        </Modal>
      </Modal>,
    );
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(inner).toHaveBeenCalledTimes(1);
    expect(outer).not.toHaveBeenCalled();

    rerender(<Modal onClose={outer} title="Outer">o</Modal>);
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(outer).toHaveBeenCalledTimes(1);
  });

  it('a nested modal paints above its parent: z-index is 50 + stack position', () => {
    render(
      <Modal onClose={vi.fn()} title="Outer">
        <Modal onClose={vi.fn()} title="Inner">
          i
        </Modal>
      </Modal>,
    );
    const overlayOf = (name: string) => screen.getByRole('dialog', { name }).parentElement as HTMLElement;
    expect(overlayOf('Outer').style.zIndex).toBe('50');
    expect(overlayOf('Inner').style.zIndex).toBe('51');
  });

  it('panelStyle lands on the panel as inline style', () => {
    render(
      <Modal onClose={vi.fn()} title="T" panelStyle={{ width: 866, maxWidth: 'calc(100vw - 2rem)' }}>
        x
      </Modal>,
    );
    const panel = screen.getByRole('dialog');
    expect(panel.style.width).toBe('866px');
    expect(panel.style.maxWidth).toBe('calc(100vw - 2rem)');
  });

  it('a click inside the modal does not reach a React ancestor onClick through the portal', async () => {
    const user = userEvent.setup();
    const ancestor = vi.fn();
    render(
      <div onClick={ancestor}>
        <Modal onClose={vi.fn()} title="T">
          <button>inner</button>
        </Modal>
      </div>,
    );
    await user.click(screen.getByRole('button', { name: 'inner' }));
    expect(ancestor).not.toHaveBeenCalled();
  });
});
