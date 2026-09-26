/**
 * Configure Slot sends the K-profile that is selected WHEN THE CLICK RUNS
 * (upstream 5dd7bd21).
 *
 * React Query hands a mutation its options from an effect, so a click landing
 * between a commit and that effect runs the PREVIOUS render's mutationFn — one
 * that captured the selection before the K-profile query resolved. The payload
 * then carried `cali_idx: -1` and the printer bound the default K while the
 * dialog showed the calibrated profile selected. Upstream measured it as an
 * intermittent failure (2 in 15 staggered runs); a timing race is not something
 * a unit test can hit on demand, so the fix's shape is pinned instead.
 */
import { describe, expect, it } from 'vitest';

import source from '../../components/ConfigureAmsSlotModal.tsx?raw';

describe('ConfigureAmsSlotModal — the selected K-profile at execute time', () => {
  it('keeps the selection in a ref written during render, not in an effect', () => {
    expect(source).toContain('const selectedKProfileRef = useRef<KProfile | null>(null);');
    expect(source).toContain('selectedKProfileRef.current = selectedKProfile;');
    const assignment = source.indexOf('selectedKProfileRef.current = selectedKProfile;');
    const before = source.slice(Math.max(0, assignment - 200), assignment);
    expect(before).not.toContain('useEffect(');
  });

  it('reads the ref inside the mutation', () => {
    const mutation = source.slice(source.indexOf('const configureMutation = useMutation({'));
    const body = mutation.slice(0, mutation.indexOf('const resetMutation'));
    expect(body).toContain('const kProfile = selectedKProfileRef.current;');
    expect(body).not.toMatch(/selectedKProfile\?\./);
  });
});
