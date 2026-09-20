import { afterEach, describe, expect, it } from 'vitest';
import {
  clearLiveStatusPriority,
  liveStatusPriorityIds,
  prioritizeLiveStatusEntries,
  setLiveStatusPriority,
} from '../../utils/liveStatusPriority';

describe('live status priority', () => {
  afterEach(() => {
    clearLiveStatusPriority('printers');
    clearLiveStatusPriority('queues');
    clearLiveStatusPriority('monitor');
  });

  it('puts mounted cards first while preserving their original order', () => {
    setLiveStatusPriority('printers', [4]);
    setLiveStatusPriority('queues', [2]);

    expect([...liveStatusPriorityIds()]).toEqual([4, 2]);
    expect(prioritizeLiveStatusEntries([[1, 'one'], [2, 'two'], [3, 'three'], [4, 'four']]))
      .toEqual([[2, 'two'], [4, 'four'], [1, 'one'], [3, 'three']]);
  });
});
