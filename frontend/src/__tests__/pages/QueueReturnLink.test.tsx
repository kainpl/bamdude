import { expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { api, type AutoQueueItem } from '../../api/client';
import { QueuePage } from '../../pages/QueuePage';
import { render } from '../utils';

it.each(['/queue#auto-job-27', '/queue?view=expanded#auto-job-27'])(
  'follows the return link after AutoQueue data arrives at %s', async (url) => {
    window.history.replaceState(null, '', url);
    localStorage.removeItem('queueViewMode');
    const scrolledTo: string[] = [];
    const original = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollIntoView');
    Object.defineProperty(Element.prototype, 'scrollIntoView', {
      configurable: true,
      value(this: Element) { scrolledTo.push(this.id); },
    });
    let deliver!: (items: AutoQueueItem[]) => void;
    const queue = vi.spyOn(api, 'getAutoQueue').mockImplementation(() =>
      new Promise(resolve => { deliver = resolve; }));
    const returned = {
      id: 27, status: 'pending', position: 1, target_model: 'P1S',
      created_at: '2026-11-21T12:00:00Z', unsent_return_count: 1,
      unsent_return_printer: 'P1S Workshop',
    } as AutoQueueItem;
    try {
      render(<QueuePage />);
      await waitFor(() => {
        expect(new URLSearchParams(window.location.search).get('view')).toBe('expanded');
        expect(deliver).toBeTypeOf('function');
      });
      deliver([returned]);
      await screen.findByText(/Returned after a refused upload/);
      await waitFor(() => expect(scrolledTo).toEqual(['auto-job-27']));
      expect(window.location.hash).toBe('#auto-job-27');
    } finally {
      queue.mockRestore();
      window.history.replaceState(null, '', '/');
      if (original) Object.defineProperty(Element.prototype, 'scrollIntoView', original);
      else Reflect.deleteProperty(Element.prototype, 'scrollIntoView');
    }
  },
);
