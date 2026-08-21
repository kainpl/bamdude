/**
 * The scan is a background job now, so these events are the ONLY place its
 * numbers arrive. The two things pinned here are the two that fail quietly: an
 * ending that never reaches the tab (a strip that spins forever), and a refusal
 * to delete reported as an ordinary success.
 */
import { describe, it, expect, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { useLibraryScanProgress } from '../../hooks/useLibraryScanProgress';

function emit(type: string, data: Record<string, unknown>) {
  act(() => {
    window.dispatchEvent(new CustomEvent(type, { detail: { type, data } }));
  });
}

describe('useLibraryScanProgress', () => {
  it('shows the strip the moment the button is pressed, before any event', () => {
    const { result } = renderHook(() => useLibraryScanProgress());
    act(() => result.current.markStarted(7, 42));

    expect(result.current.scans[7]).toMatchObject({ jobId: 42, status: 'running', seen: 0, total: 0 });
  });

  it('follows progress for the folder the event names', () => {
    const { result } = renderHook(() => useLibraryScanProgress());
    emit('library-scan-progress', {
      job_id: 42,
      folder_id: 7,
      total: 900,
      files_seen: 120,
      files_added: 5,
      files_updated: 2,
      files_removed: 0,
    });

    expect(result.current.scans[7]).toMatchObject({ seen: 120, total: 900, added: 5, updated: 2 });
    expect(result.current.scans[8]).toBeUndefined();
  });

  it('keeps two folders apart', () => {
    const { result } = renderHook(() => useLibraryScanProgress());
    emit('library-scan-progress', { job_id: 1, folder_id: 7, total: 10, files_seen: 3 });
    emit('library-scan-progress', { job_id: 2, folder_id: 8, total: 20, files_seen: 11 });

    expect(result.current.scans[7].seen).toBe(3);
    expect(result.current.scans[8].seen).toBe(11);
  });

  it('reports a failure as failed, not as a finished scan', () => {
    const onFinished = vi.fn();
    const { result } = renderHook(() => useLibraryScanProgress(onFinished));
    emit('library-scan-finished', {
      job_id: 42,
      folder_id: 7,
      status: 'failed',
      error: 'external path is not accessible: //nas/models',
    });

    expect(result.current.scans[7].status).toBe('failed');
    expect(result.current.scans[7].error).toContain('not accessible');
    expect(onFinished).toHaveBeenCalledWith(7, expect.objectContaining({ status: 'failed' }));
  });

  it('carries skipped_deletions through, because silence would read as "nothing changed"', () => {
    const { result } = renderHook(() => useLibraryScanProgress());
    emit('library-scan-finished', {
      job_id: 42,
      folder_id: 7,
      status: 'finished',
      skipped_deletions: true,
      files_removed: 0,
    });

    expect(result.current.scans[7]).toMatchObject({ status: 'finished', skippedDeletions: true, removed: 0 });
  });

  it('stops listening when it unmounts', () => {
    const onFinished = vi.fn();
    const { unmount } = renderHook(() => useLibraryScanProgress(onFinished));
    unmount();
    emit('library-scan-finished', { job_id: 1, folder_id: 7, status: 'finished' });

    expect(onFinished).not.toHaveBeenCalled();
  });

  it('drops a strip only when asked', () => {
    const { result } = renderHook(() => useLibraryScanProgress());
    act(() => result.current.markStarted(7, 42));
    act(() => result.current.dismiss(7));

    expect(result.current.scans[7]).toBeUndefined();
  });
});
