import { renderHook } from '@testing-library/react';
import { expect, it } from 'vitest';
import { useMonitorTarget } from '../../../features/monitor/useMonitorTarget';

it('consumes the monitorPrinter deep link emitted by MCP order work', () => {
  window.history.replaceState({}, '', '/queue?monitorPrinter=17');
  const { result, unmount } = renderHook(() => useMonitorTarget());
  expect(result.current[0]).toBe(17);
  unmount();
});
