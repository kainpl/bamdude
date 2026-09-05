/**
 * The pickers write a JSON array into a settings string, so the thing worth
 * pinning is the value handed back — sorted, and with the ticked id removed on
 * a second click — plus the malformed-input contract of the parser itself.
 */

import { describe, it, expect, vi } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../../utils';
import { server } from '../../mocks/server';
import { StaggerGroupPickers } from '../../../components/settings/StaggerGroupPickers';
import { parseIdList } from '../../../components/settings/staggerGroupIds';

describe('parseIdList', () => {
  it('reads a JSON array of ints and nothing else', () => {
    expect(parseIdList('[3,1]')).toEqual([3, 1]);
    expect(parseIdList('')).toEqual([]);
    expect(parseIdList(undefined)).toEqual([]);
    expect(parseIdList('nope')).toEqual([]);
    expect(parseIdList('["1", 2]')).toEqual([2]);
  });
});

describe('StaggerGroupPickers', () => {
  it('ticks a tag into the sorted JSON list', async () => {
    server.use(http.get('/api/v1/printer-tags', () => HttpResponse.json({ tags: [
      { id: 5, name: 'Фаза 2', printer_count: 0, is_stagger_group: false },
      { id: 2, name: 'Фаза 1', printer_count: 0, is_stagger_group: true },
    ] })));
    const onChange = vi.fn();
    render(<StaggerGroupPickers byTags tagIds="[2]" byLocation={false} locationIds="[]" onChange={onChange} />);
    await userEvent.click(await screen.findByLabelText('Фаза 2'));
    expect(onChange).toHaveBeenCalledWith('stagger_group_tag_ids', '[2,5]');
    expect(screen.getByText(/counts in every group/)).toBeInTheDocument();
  });

  it('unticks a location and lists the tree indented', async () => {
    server.use(http.get('/api/v1/printer-locations', () => HttpResponse.json({ locations: [
      { id: 1, name: 'Цех A', parent_id: null, path: 'Цех A', depth: 1, printer_count: 0, sensor_count: 0, queued_count: 0 },
      { id: 2, name: 'Ряд 1', parent_id: 1, path: 'Цех A / Ряд 1', depth: 2, printer_count: 0, sensor_count: 0, queued_count: 0 },
    ] })));
    const onChange = vi.fn();
    render(<StaggerGroupPickers byTags={false} tagIds="[]" byLocation locationIds="[1,2]" onChange={onChange} />);
    await userEvent.click(await screen.findByLabelText('Ряд 1'));
    expect(onChange).toHaveBeenCalledWith('stagger_group_location_ids', '[1]');
    expect(screen.getByLabelText('Ряд 1').closest('label')).toHaveStyle({ paddingLeft: '16px' });
  });

  it('says the cap stays farm-wide while nothing is picked', async () => {
    server.use(http.get('/api/v1/printer-tags', () => HttpResponse.json({ tags: [] })));
    render(<StaggerGroupPickers byTags tagIds="[]" byLocation={false} locationIds="[]" onChange={vi.fn()} />);
    expect(await screen.findByText(/cap stays farm-wide/)).toBeInTheDocument();
  });
});
