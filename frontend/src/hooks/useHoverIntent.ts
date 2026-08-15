import { useCallback, useEffect, useRef } from 'react';

/**
 * Open on hover, close a moment after the pointer leaves.
 *
 * ⚠️ The delay is the point, not politeness. A popover anchored above its icon
 * sits across an 8 px gap (`mb-2`), and the pointer crossing that gap is
 * outside both boxes — an immediate close would shut the popover on the way to
 * it, every time. The pending close is cancelled by entering either.
 *
 * ⚠️ And the close must live on the WRAPPER, not inside the popover. The
 * sidebar popovers only closed on their own `onMouseLeave`, so hovering the
 * icon and walking away left them open forever: the only way to dismiss one
 * was to first move onto it and then off again.
 *
 * `pinned` suppresses the close for as long as the popover owns something
 * rendered outside itself — a chart or a confirmation — because closing
 * unmounts that too, and the pointer travelling to it counts as leaving.
 */
export function useHoverIntent(
  setOpen: (open: boolean) => void,
  { delay = 150 }: { delay?: number } = {},
) {
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pinned = useRef(false);

  const cancel = useCallback(() => {
    if (timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
  }, []);

  useEffect(() => cancel, [cancel]);

  const enter = useCallback(() => {
    cancel();
    setOpen(true);
  }, [cancel, setOpen]);

  const leave = useCallback(() => {
    cancel();
    timer.current = setTimeout(() => {
      // Re-read at fire time, not at schedule time: the chart or dialog is
      // usually opened after the pointer has already started to leave.
      if (!pinned.current) setOpen(false);
    }, delay);
  }, [cancel, delay, setOpen]);

  /** Called by the popover while it owns something rendered outside itself. */
  const setPinned = useCallback(
    (value: boolean) => {
      pinned.current = value;
      if (value) cancel();
    },
    [cancel],
  );

  return { enter, leave, setPinned };
}
