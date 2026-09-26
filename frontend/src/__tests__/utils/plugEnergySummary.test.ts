/**
 * "Plugs online" counts plugs that answer, not plugs that meter (upstream
 * 5a05e03c, #2859). A switch with no power sensor is online — the backend just
 * has no energy to give — and was counted offline for as long as it was linked.
 */
import { describe, it, expect } from 'vitest';
import { summarizePlugEnergy } from '../../utils/plugEnergySummary';

const plug = (id: number, plug_type = 'tasmota') => ({ id, plug_type });

describe('summarizePlugEnergy', () => {
  it('counts a reachable switch with no energy as online', () => {
    const summary = summarizePlugEnergy([
      { plug: plug(1), status: { reachable: true, energy: null } },
      { plug: plug(2), status: { reachable: true, energy: { power: 120, today: 1.5, yesterday: 2, total: 40 } } },
    ]);
    expect(summary.reachableCount).toBe(2);
    expect(summary.totalPower).toBe(120);
    expect(summary.totalLifetime).toBe(40);
  });

  it('does not count an unreachable plug, nor a failed status read', () => {
    const summary = summarizePlugEnergy([
      { plug: plug(1), status: { reachable: false, energy: null } },
      { plug: plug(2), status: null },
    ]);
    expect(summary.reachableCount).toBe(0);
  });

  it('counts an MQTT plug that reports power as reachable', () => {
    const summary = summarizePlugEnergy([
      { plug: plug(1, 'mqtt'), status: { reachable: false, energy: { power: 7 } } },
    ]);
    expect(summary.reachableCount).toBe(1);
    expect(summary.totalPower).toBe(7);
  });
});
