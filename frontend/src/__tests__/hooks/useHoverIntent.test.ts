/**
 * Hover intent for the sidebar popovers.
 *
 * The bug it fixes: the sensors and smart-plug popovers opened on the icon's
 * `onMouseEnter` but closed only on their own `onMouseLeave`. Hovering the icon
 * and walking away left the popover on screen indefinitely — the only way to
 * dismiss one was to move the pointer onto it and then off again.
 *
 * ⚠️ The close is delayed on purpose. The popover sits 8px above its icon
 * (`mb-2`), and that gap belongs to neither, so an immediate close would shut
 * the popover on the way to it — every single time.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, renderHook } from '@testing-library/react';

import { useHoverIntent } from '../../hooks/useHoverIntent';

describe('useHoverIntent', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('opens as soon as the pointer arrives', () => {
    const setOpen = vi.fn();
    const { result } = renderHook(() => useHoverIntent(setOpen));

    act(() => result.current.enter());

    expect(setOpen).toHaveBeenCalledWith(true);
  });

  it('closes on the way out — which is the whole bug', () => {
    const setOpen = vi.fn();
    const { result } = renderHook(() => useHoverIntent(setOpen));

    act(() => result.current.enter());
    act(() => result.current.leave());
    act(() => void vi.advanceTimersByTime(200));

    expect(setOpen).toHaveBeenLastCalledWith(false);
  });

  it('does not close instantly, so the gap to the popover is crossable', () => {
    const setOpen = vi.fn();
    const { result } = renderHook(() => useHoverIntent(setOpen));

    act(() => result.current.enter());
    act(() => result.current.leave());
    act(() => void vi.advanceTimersByTime(50));

    expect(setOpen).not.toHaveBeenCalledWith(false);
  });

  it('re-entering cancels a pending close', () => {
    const setOpen = vi.fn();
    const { result } = renderHook(() => useHoverIntent(setOpen));

    act(() => result.current.enter());
    act(() => result.current.leave());
    act(() => void vi.advanceTimersByTime(50));
    act(() => result.current.enter());
    act(() => void vi.advanceTimersByTime(500));

    expect(setOpen).not.toHaveBeenCalledWith(false);
  });

  describe('while the popover owns something outside itself', () => {
    it('stays open — closing would unmount the chart or dialog with it', () => {
      const setOpen = vi.fn();
      const { result } = renderHook(() => useHoverIntent(setOpen));

      act(() => result.current.enter());
      act(() => result.current.setPinned(true));
      act(() => result.current.leave());
      act(() => void vi.advanceTimersByTime(500));

      expect(setOpen).not.toHaveBeenCalledWith(false);
    });

    it('closes once it is released', () => {
      const setOpen = vi.fn();
      const { result } = renderHook(() => useHoverIntent(setOpen));

      act(() => result.current.enter());
      act(() => result.current.setPinned(true));
      act(() => result.current.leave());
      act(() => void vi.advanceTimersByTime(500));
      act(() => result.current.setPinned(false));
      act(() => result.current.leave());
      act(() => void vi.advanceTimersByTime(500));

      expect(setOpen).toHaveBeenLastCalledWith(false);
    });

    it('is read when the timer fires, not when it was set', () => {
      /** ⚠️ The dialog is usually opened after the pointer has already begun to
       *  leave, so a pin captured at schedule time would arrive too late. */
      const setOpen = vi.fn();
      const { result } = renderHook(() => useHoverIntent(setOpen));

      act(() => result.current.enter());
      act(() => result.current.leave());
      act(() => result.current.setPinned(true));
      act(() => void vi.advanceTimersByTime(500));

      expect(setOpen).not.toHaveBeenCalledWith(false);
    });
  });

  it('drops a pending close when it unmounts', () => {
    const setOpen = vi.fn();
    const { result, unmount } = renderHook(() => useHoverIntent(setOpen));

    act(() => result.current.enter());
    act(() => result.current.leave());
    unmount();
    act(() => void vi.advanceTimersByTime(500));

    expect(setOpen).not.toHaveBeenCalledWith(false);
  });
});
