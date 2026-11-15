import { fireEvent } from '@testing-library/react';

/**
 * Drive the print dialog's slot picker the way an operator does: open it, then
 * click a slot. `trigger` is the picker's button (found by its tooltip or its
 * label); `value` is the global tray id, or '' for "no slot".
 */
export function chooseSlot(trigger: HTMLElement, value: number | ''): void {
  fireEvent.click(trigger);
  const list = document.getElementById(trigger.getAttribute('aria-controls') ?? '');
  const option = list?.querySelector<HTMLElement>(`[data-value="${value}"]`);
  if (!option) throw new Error(`slot ${value} is not offered`);
  fireEvent.click(option);
}

/** Everything the picker offers, as text — the entries exist while it is closed. */
export function offeredSlots(trigger: HTMLElement): string {
  return document.getElementById(trigger.getAttribute('aria-controls') ?? '')?.textContent ?? '';
}
