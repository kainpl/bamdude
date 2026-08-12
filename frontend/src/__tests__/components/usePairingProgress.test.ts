/**
 * The pairing state machine.
 *
 * Kept out of the card so it can be tested without rendering, and because the
 * countdown is purely local — nothing in the backend reports the join window
 * closing, so the UI has to track it itself.
 */

import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { usePairingProgress } from '../../components/zigbee/usePairingProgress';

describe('usePairingProgress', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('starts idle', () => {
    const { result } = renderHook(() => usePairingProgress());

    expect(result.current.phase).toBe('idle');
    expect(result.current.events).toEqual([]);
  });

  it('counts down and returns to idle', () => {
    const { result } = renderHook(() => usePairingProgress());

    act(() => result.current.start(3));
    expect(result.current.phase).toBe('pairing');
    expect(result.current.secondsLeft).toBe(3);

    act(() => void vi.advanceTimersByTime(3000));
    expect(result.current.phase).toBe('idle');
  });

  it('records a joining device', () => {
    const { result } = renderHook(() => usePairingProgress());
    act(() => result.current.start(60));

    act(() => {
      window.dispatchEvent(new CustomEvent('zigbee-device-joining', { detail: { ieee: 'aa:bb' } }));
    });

    expect(result.current.events).toEqual([{ kind: 'joining', ieee: 'aa:bb', model: null }]);
  });

  it('records a paired device with its model', () => {
    const { result } = renderHook(() => usePairingProgress());
    act(() => result.current.start(60));

    act(() => {
      window.dispatchEvent(
        new CustomEvent('zigbee-device-paired', { detail: { device: { ieee: 'aa:bb', model: 'S60ZBTPF' } } }),
      );
    });

    expect(result.current.events).toEqual([{ kind: 'paired', ieee: 'aa:bb', model: 'S60ZBTPF' }]);
  });

  it('records a rejection, which the UI has to explain rather than hide', () => {
    const { result } = renderHook(() => usePairingProgress());
    act(() => result.current.start(60));

    act(() => {
      window.dispatchEvent(
        new CustomEvent('zigbee-device-rejected', { detail: { device: { ieee: 'cc:dd', model: 'SNZB-02' } } }),
      );
    });

    expect(result.current.events).toEqual([{ kind: 'rejected', ieee: 'cc:dd', model: 'SNZB-02' }]);
  });

  it('ignores events while idle', () => {
    const { result } = renderHook(() => usePairingProgress());

    act(() => {
      window.dispatchEvent(new CustomEvent('zigbee-device-joining', { detail: { ieee: 'aa:bb' } }));
    });

    expect(result.current.events).toEqual([]);
  });

  it('clears the previous run when pairing starts again', () => {
    const { result } = renderHook(() => usePairingProgress());
    act(() => result.current.start(60));
    act(() => {
      window.dispatchEvent(new CustomEvent('zigbee-device-joining', { detail: { ieee: 'aa:bb' } }));
    });

    act(() => result.current.start(60));

    // Otherwise last session's rejection sits under this session's countdown and
    // reads as something that just happened.
    expect(result.current.events).toEqual([]);
  });
});
