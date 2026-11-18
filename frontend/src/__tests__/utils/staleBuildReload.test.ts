import { describe, it, expect, vi, beforeEach } from 'vitest';
import { reloadOnceForStaleBuild } from '../../utils/staleBuildReload';

beforeEach(() => window.sessionStorage.clear());

describe('reloadOnceForStaleBuild', () => {
  it('reloads the tab the first time a piece of the build is gone', () => {
    const reload = vi.fn();
    expect(reloadOnceForStaleBuild(reload, () => 1_000_000)).toBe(true);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('does not loop when the reload did not help', () => {
    const reload = vi.fn();
    reloadOnceForStaleBuild(reload, () => 1_000_000);
    expect(reloadOnceForStaleBuild(reload, () => 1_030_000)).toBe(false);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('tries again for a later update', () => {
    const reload = vi.fn();
    reloadOnceForStaleBuild(reload, () => 1_000_000);
    expect(reloadOnceForStaleBuild(reload, () => 1_000_000 + 61_000)).toBe(true);
    expect(reload).toHaveBeenCalledTimes(2);
  });
});
