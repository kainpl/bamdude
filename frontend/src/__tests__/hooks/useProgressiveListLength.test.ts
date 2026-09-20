import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  INITIAL_PRINTER_CARD_COUNT,
  PRINTER_CARD_MOUNT_DELAY_MS,
  PRINTER_CARD_MOUNT_STEP,
  useProgressiveListLength,
} from '../../hooks/useProgressiveListLength';

describe('useProgressiveListLength', () => {
  afterEach(() => vi.useRealTimers());

  it('shows the first viewport-sized card slice immediately and mounts the remainder in tasks', async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useProgressiveListLength(30));

    expect(result.current).toBe(INITIAL_PRINTER_CARD_COUNT);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(PRINTER_CARD_MOUNT_DELAY_MS);
    });
    expect(result.current).toBe(INITIAL_PRINTER_CARD_COUNT + PRINTER_CARD_MOUNT_STEP);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(PRINTER_CARD_MOUNT_DELAY_MS);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PRINTER_CARD_MOUNT_DELAY_MS);
    });
    expect(result.current).toBe(30);
  });

  it('never exposes more cards than the current list contains', () => {
    const { result } = renderHook(() => useProgressiveListLength(3));
    expect(result.current).toBe(3);
  });
});
