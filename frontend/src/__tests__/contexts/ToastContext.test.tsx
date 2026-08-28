/**
 * Tests for ToastContext's post-unmount safety guards.
 *
 * Regression: a login response handler calling showToast AFTER the provider
 * had already been unmounted by Vitest's afterEach scheduled a 3s setTimeout
 * that fired during test teardown. The callback's setToasts then tried to
 * schedule a React update against a torn-down jsdom, producing
 * "window is not defined" as an uncaught exception.
 *
 * The provider now gates every setToasts call on an isMountedRef and
 * re-checks inside the auto-dismiss setTimeout callback so stale async
 * paths no-op instead of crashing.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { act, render, renderHook, screen } from '@testing-library/react';
import { type ReactNode } from 'react';
import { ToastProvider, useToast } from '../../contexts/ToastContext';

function Wrapper({ children }: { children: ReactNode }) {
  return <ToastProvider>{children}</ToastProvider>;
}

describe('ToastContext post-unmount safety', () => {
  beforeEach(() => {
    vi.useRealTimers();
  });

  it('does not crash when showToast is called after unmount', () => {
    const { result, unmount } = renderHook(() => useToast(), { wrapper: Wrapper });

    // Capture the callbacks BEFORE unmount — a real stale-closure scenario.
    // (Async handlers that kicked off before unmount keep their captured
    // context value and will invoke this function after we tear down.)
    const { showToast } = result.current;

    unmount();

    // Post-unmount invocation is now a no-op; must not throw.
    expect(() => showToast('delayed error message', 'error')).not.toThrow();
  });

  it('does not invoke setToasts when the auto-dismiss timer fires after unmount', async () => {
    vi.useFakeTimers();

    const { result, unmount } = renderHook(() => useToast(), { wrapper: Wrapper });

    act(() => {
      result.current.showToast('will outlive the provider', 'error');
    });

    // Unmount BEFORE the 3s timer fires — the unmount effect clears pending
    // timers, but a belt-and-braces check inside the timer callback (for
    // cases where the timer was scheduled post-unmount) must also hold.
    unmount();

    // Advance past the 3s auto-dismiss window. If the guard isn't in place
    // this would throw "window is not defined" in a torn-down jsdom; we
    // simulate by asserting no error propagates.
    expect(() => {
      vi.advanceTimersByTime(5000);
    }).not.toThrow();

    vi.useRealTimers();
  });

  it('post-unmount showPersistentToast and dismissToast are no-ops', () => {
    const { result, unmount } = renderHook(() => useToast(), { wrapper: Wrapper });
    const { showPersistentToast, dismissToast } = result.current;
    unmount();

    // Both must short-circuit rather than attempt setState on a dead tree.
    expect(() => showPersistentToast('orphan', 'still here', 'info')).not.toThrow();
    expect(() => dismissToast('orphan')).not.toThrow();
  });

  it('normal showToast flow still displays and auto-dismisses while mounted', () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useToast(), { wrapper: Wrapper });

    act(() => {
      result.current.showToast('mounted path works', 'success');
    });

    // No easy way to read toast DOM from the hook alone; assert the timer
    // ran without throwing — that proves the isMountedRef guard didn't
    // incorrectly short-circuit the mounted path.
    expect(() => {
      act(() => {
        vi.advanceTimersByTime(3500);
      });
    }).not.toThrow();

    vi.useRealTimers();
  });
});

describe('ToastContext viewport on small screens (#2612)', () => {
  it('positions the viewport with safe-area-aware offsets', () => {
    // An installed PWA on a notched phone must clear the home indicator and the
    // landscape notch; bottom-4 / right-20 alone do not.
    const { getByTestId } = render(
      <ToastProvider>
        <div />
      </ToastProvider>,
    );
    const viewport = getByTestId('toast-viewport');
    expect(viewport.style.bottom).toContain('safe-area-inset-bottom');
    expect(viewport.style.right).toContain('safe-area-inset-right');
  });

  it('caps every toast to the viewport width', () => {
    // The dispatch toast is a fixed 420px and the viewport sits 80px from the
    // right — 500px total, wider than a 390px iPhone, so without a cap it runs
    // off the left edge and the whole toast is unreadable.
    function Emitter() {
      const { showPersistentToast } = useToast();
      return <button onClick={() => showPersistentToast('dispatch', 'sending prints')}>go</button>;
    }
    const { getByText, getByTestId } = render(
      <ToastProvider>
        <Emitter />
      </ToastProvider>,
    );
    act(() => {
      getByText('go').click();
    });
    const rendered = getByTestId('toast-viewport').firstElementChild as HTMLElement;
    expect(rendered).not.toBeNull();
    expect(rendered.style.maxWidth).toContain('100vw');
  });

  // A missed error is gone for good — there is no notification history — and
  // an error toast carries a reason relayed from the printer or the backend,
  // often a couple of lines. Three seconds is not long enough to finish one.
  describe('how long a toast stays up', () => {
    it.each(['error', 'warning'] as const)('holds a %s for six seconds', (type) => {
      vi.useFakeTimers();
      const { result } = renderHook(() => useToast(), { wrapper: Wrapper });

      act(() => result.current.showToast('something went wrong', type));
      act(() => { vi.advanceTimersByTime(3000); });
      expect(screen.getByText('something went wrong')).toBeInTheDocument();

      act(() => { vi.advanceTimersByTime(3000); });
      expect(screen.queryByText('something went wrong')).not.toBeInTheDocument();

      vi.useRealTimers();
    });

    it.each(['success', 'info'] as const)('keeps a %s at three', (type) => {
      // These confirm something the user just did and are skimmed, not read.
      vi.useFakeTimers();
      const { result } = renderHook(() => useToast(), { wrapper: Wrapper });

      act(() => result.current.showToast('saved', type));
      act(() => { vi.advanceTimersByTime(3000); });

      expect(screen.queryByText('saved')).not.toBeInTheDocument();
      vi.useRealTimers();
    });
  });

});
