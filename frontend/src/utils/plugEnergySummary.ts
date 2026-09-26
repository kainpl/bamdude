/**
 * The Smart Plugs tab's totals: how many plugs answer, and the energy those
 * that meter report.
 *
 * A plug is ONLINE when it answers, not when it measures (upstream #2859): a
 * switch with no power sensor answers with no `energy`, and requiring energy
 * here counted it offline for as long as it was linked, so "plugs online"
 * quietly meant "plugs reporting energy".
 */

interface PlugLike {
  plug_type: string;
}

interface StatusLike {
  reachable?: boolean | null;
  energy?: {
    power?: number | null;
    today?: number | null;
    yesterday?: number | null;
    total?: number | null;
  } | null;
}

export interface PlugEnergySummary {
  totalPower: number;
  totalToday: number;
  totalYesterday: number;
  totalLifetime: number;
  reachableCount: number;
}

export function summarizePlugEnergy(
  statuses: readonly { plug: PlugLike; status: StatusLike | null }[],
): PlugEnergySummary {
  const summary: PlugEnergySummary = { totalPower: 0, totalToday: 0, totalYesterday: 0, totalLifetime: 0, reachableCount: 0 };
  for (const { plug, status } of statuses) {
    // An MQTT plug has no request to answer; power data arriving is its sign of life.
    const hasMqttData = plug.plug_type === 'mqtt' && status?.energy?.power != null;
    if (!(status?.reachable || hasMqttData)) continue;
    summary.reachableCount++;
    const energy = status?.energy;
    if (energy?.power != null) summary.totalPower += energy.power;
    if (energy?.today != null) summary.totalToday += energy.today;
    if (energy?.yesterday != null) summary.totalYesterday += energy.yesterday;
    if (energy?.total != null) summary.totalLifetime += energy.total;
  }
  return summary;
}
