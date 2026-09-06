/**
 * The banner is the only reader of the stagger snapshot, and the snapshot is
 * now per group. These pin the two shapes that must both keep working: the
 * single unlabelled group a farm with no split gets, and the labelled ones.
 */

import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../../utils';
import { server } from '../../mocks/server';
import { StaggerBanner } from '../../../components/Queue/StaggerBanner';

const slot = (printer_id: number, printer_name: string, wildcard = false) => ({
  printer_id, printer_name, started_at: 1, temp_reached_at: null, state: 'heating', seconds_to_free: 90, interval_seconds: 300, wildcard,
});

describe('StaggerBanner', () => {
  it('keeps the farm-wide line when nothing is split', async () => {
    server.use(http.get('/api/v1/queue/stagger-state', () => HttpResponse.json({
      enabled: true, concurrent: 2, interval_minutes: 5, wait_for_bed: true, split: { by_tags: false, by_location: false },
      groups: [{ tag_id: null, location_id: null, label: null, cap: 2, occupied: 1, free_slots: 1, next_free_in_seconds: null, slots: [slot(1, 'P1')] }],
    })));
    render(<StaggerBanner />);
    expect(await screen.findByText('Stagger: 1/2 slots occupied')).toBeInTheDocument();
  });

  it('shows one segment per group and marks a wildcard in the tooltip', async () => {
    server.use(http.get('/api/v1/queue/stagger-state', () => HttpResponse.json({
      enabled: true, concurrent: 1, interval_minutes: 5, wait_for_bed: true, split: { by_tags: true, by_location: false },
      groups: [
        { tag_id: 1, location_id: null, label: 'Фаза 1', cap: 1, occupied: 1, free_slots: 0, next_free_in_seconds: 90, slots: [slot(1, 'P1')] },
        { tag_id: 2, location_id: null, label: 'Фаза 2', cap: 1, occupied: 1, free_slots: 0, next_free_in_seconds: 40, slots: [slot(9, 'P9', true)] },
        { tag_id: 3, location_id: null, label: 'Фаза 3', cap: 1, occupied: 0, free_slots: 1, next_free_in_seconds: null, slots: [] },
      ],
    })));
    render(<StaggerBanner />);
    expect(await screen.findByText('Фаза 1: 1/1 · Фаза 2: 1/1 · Фаза 3: 0/1')).toBeInTheDocument();
    expect(screen.getByText('· next free in 40s')).toBeInTheDocument();
    const banner = screen.getByText(/Фаза 1: 1\/1/).closest('div')!;
    expect(banner.getAttribute('title')).toContain('P9 (no group, counts everywhere)');
  });

  it('reads each group’s own cap, not the farm-wide number', async () => {
    server.use(http.get('/api/v1/queue/stagger-state', () => HttpResponse.json({
      // concurrent 5 is deliberately neither cap: a banner still reading the
      // farm-wide number would print 1/5 and 0/5 here.
      enabled: true, concurrent: 5, interval_minutes: 5, wait_for_bed: true, split: { by_tags: true, by_location: false },
      groups: [
        { tag_id: 1, location_id: null, label: 'Phase 1', cap: 1, occupied: 1, free_slots: 0, next_free_in_seconds: 90, slots: [slot(1, 'P1')] },
        { tag_id: 2, location_id: null, label: 'Phase 2', cap: 2, occupied: 0, free_slots: 2, next_free_in_seconds: null, slots: [] },
      ],
    })));
    render(<StaggerBanner />);
    expect(await screen.findByText('Phase 1: 1/1 · Phase 2: 0/2')).toBeInTheDocument();
    const banner = screen.getByText(/Phase 1: 1\/1/).closest('div')!;
    expect(banner.getAttribute('title')).toContain('Phase 1 — 1/1');
  });

  it('renders nothing while disabled', async () => {
    let served = false;
    server.use(http.get('/api/v1/queue/stagger-state', () => {
      served = true;
      return HttpResponse.json({
        enabled: false, concurrent: 2, interval_minutes: 5, wait_for_bed: true, split: { by_tags: false, by_location: false }, groups: [],
      });
    }));
    const { container } = render(<StaggerBanner />);
    // Wait for the snapshot to actually arrive: otherwise "nothing rendered"
    // would only ever mean "nothing had rendered yet".
    await waitFor(() => expect(served).toBe(true));
    // The shared render wrapper mounts a toast viewport into `container`, so
    // the assertion is about the banner, not about an empty tree.
    expect(container.querySelector('[title]')).toBeNull();
    expect(screen.queryByText(/Stagger:/)).not.toBeInTheDocument();
  });
});
