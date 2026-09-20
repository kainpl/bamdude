/**
 * The pickers write a JSON array into a settings string, so the thing worth
 * pinning is the value handed back — sorted, and with the ticked id removed on
 * a second click — plus the malformed-input contract of the parser itself.
 */

import { useState } from 'react';
import { describe, it, expect, vi } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../../utils';
import { server } from '../../mocks/server';
import { StaggerGroupPickers } from '../../../components/settings/StaggerGroupPickers';
import { parseIdList, parseLimitMap, setLimit } from '../../../components/settings/staggerGroupIds';

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
    render(<StaggerGroupPickers byTags tagIds="[2]" tagLimits="{}" byLocation={false} locationIds="[]" locationLimits="{}" globalCap={2} onChange={onChange} />);
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
    render(<StaggerGroupPickers byTags={false} tagIds="[]" tagLimits="{}" byLocation locationIds="[1,2]" locationLimits="{}" globalCap={2} onChange={onChange} />);
    await userEvent.click(await screen.findByLabelText('Ряд 1'));
    expect(onChange).toHaveBeenCalledWith('stagger_group_location_ids', '[1]');
    expect(screen.getByLabelText('Ряд 1').closest('label')).toHaveStyle({ paddingLeft: '16px' });
  });

  /**
   * The picker is where a phase is chosen, so it has to show the colour the
   * phase already wears — otherwise the operator picks blind and only finds
   * out on the queue banner which dot is which.
   */
  it('marks each tag with its own colour', async () => {
    server.use(http.get('/api/v1/printer-tags', () => HttpResponse.json({ tags: [
      { id: 1, name: 'Фаза 1', color: '#f59e0b', printer_count: 0, is_stagger_group: true },
      { id: 2, name: 'Фаза 2', color: null, printer_count: 0, is_stagger_group: false },
    ] })));
    render(<StaggerGroupPickers byTags tagIds="[1]" tagLimits="{}" byLocation={false} locationIds="[]" locationLimits="{}" globalCap={2} onChange={vi.fn()} />);
    const coloured = (await screen.findByLabelText('Фаза 1')).closest('label')!.querySelector('span[aria-hidden]') as HTMLElement;
    expect(coloured.style.backgroundColor).toBe('rgb(245, 158, 11)');
    // A colourless tag keeps the outlined placeholder, so the names stay aligned.
    const plain = screen.getByLabelText('Фаза 2').closest('label')!.querySelector('span[aria-hidden]') as HTMLElement;
    expect(plain.style.backgroundColor).toBe('transparent');
  });

  it('says the cap stays farm-wide while nothing is picked', async () => {
    server.use(http.get('/api/v1/printer-tags', () => HttpResponse.json({ tags: [] })));
    render(<StaggerGroupPickers byTags tagIds="[]" tagLimits="{}" byLocation={false} locationIds="[]" locationLimits="{}" globalCap={2} onChange={vi.fn()} />);
    expect(await screen.findByText(/cap stays farm-wide/)).toBeInTheDocument();
  });
});

describe('limit maps', () => {
  it('reads an object of id → cap and drops anything else', () => {
    expect(parseLimitMap('{"5": 2, "x": 1, "6": 0, "7": "3"}')).toEqual({ 5: 2 });
    expect(parseLimitMap('[1]')).toEqual({});
    expect(parseLimitMap('nope')).toEqual({});
    expect(parseLimitMap(undefined)).toEqual({});
  });

  it('sets and clears one id, serialised with numeric key order', () => {
    expect(setLimit('{"10": 2}', 5, 1)).toBe('{"5":1,"10":2}');
    expect(setLimit('{"5":1,"10":2}', 5, null)).toBe('{"10":2}');
  });
});

describe('per-group limits in the pickers', () => {
  const tags = [
    { id: 1, name: 'Phase 1', color: null, printer_count: 2, is_stagger_group: true },
    { id: 2, name: 'Phase 2', color: null, printer_count: 1, is_stagger_group: false },
  ];

  /**
   * ⚠️ The harness below enables BOTH axes, so both queries run.
   *
   * Serving only the endpoint a test cares about leaves the other request to
   * msw's `onUnhandledRequest: 'bypass'`, where it fails and renders the empty
   * branch by accident — an empty list that looks intended but is really a
   * broken request. Every such test states the empty side explicitly.
   */
  const noLocations = http.get('/api/v1/printer-locations', () => HttpResponse.json({ locations: [] }));
  const noTags = http.get('/api/v1/printer-tags', () => HttpResponse.json({ tags: [] }));

  it('offers a limit field only for a picked tag, placeholder = the global cap', async () => {
    server.use(http.get('/api/v1/printer-tags', () => HttpResponse.json({ tags })));
    render(
      <StaggerGroupPickers byTags tagIds="[1]" tagLimits="{}" byLocation={false} locationIds="[]" locationLimits="{}" globalCap={3} onChange={vi.fn()} />,
    );
    const field = (await screen.findByLabelText('Limit for Phase 1')) as HTMLInputElement;
    expect(field.placeholder).toBe('3');
    expect(screen.queryByLabelText('Limit for Phase 2')).not.toBeInTheDocument();
  });

  /**
   * Feeds what the picker writes back in as props, because the limit field is
   * a controlled input: with a parent that never applies the change React
   * restores the previous value between keystrokes, and a cleared "2" typed
   * over as "1" would read as 21 — an artefact of the test, not of the field.
   */
  function Harness({ onChange }: { onChange: (key: string, value: boolean | string) => void }) {
    const [tagIds, setTagIds] = useState('[1]');
    const [tagLimits, setTagLimits] = useState('{"1":2}');
    const [locationIds, setLocationIds] = useState('[1]');
    const [locationLimits, setLocationLimits] = useState('{"1":2}');
    return (
      <StaggerGroupPickers
        byTags tagIds={tagIds} tagLimits={tagLimits}
        byLocation locationIds={locationIds} locationLimits={locationLimits}
        globalCap={3}
        onChange={(key, value) => {
          onChange(key, value);
          if (key === 'stagger_group_tag_ids') setTagIds(value as string);
          if (key === 'stagger_tag_limits') setTagLimits(value as string);
          if (key === 'stagger_group_location_ids') setLocationIds(value as string);
          if (key === 'stagger_location_limits') setLocationLimits(value as string);
        }}
      />
    );
  }

  it('writes a limit and clears it when the tag is unpicked', async () => {
    server.use(http.get('/api/v1/printer-tags', () => HttpResponse.json({ tags })), noLocations);
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const field = await screen.findByLabelText('Limit for Phase 1');
    await userEvent.clear(field);
    await userEvent.type(field, '1');
    expect(onChange).toHaveBeenCalledWith('stagger_tag_limits', '{"1":1}');

    // Cleared first, so the '{}' below can only have come from the unpick.
    onChange.mockClear();
    await userEvent.click(screen.getByRole('checkbox', { name: /Phase 1/ }));
    expect(onChange).toHaveBeenCalledWith('stagger_group_tag_ids', '[]');
    expect(onChange).toHaveBeenCalledWith('stagger_tag_limits', '{}');
  });

  /**
   * ⚠️ A cap above the farm-wide one is not a bigger cap — it is a number the
   * scheduler ignores.
   *
   * `cap_for` takes the min of the group's limit and the global one, so a "9"
   * typed against a global of 3 would sit in Settings looking like nine beds
   * while three ever heat. The field refuses to write it rather than letting
   * the two disagree.
   */
  it('clamps a limit typed above the global cap down to the global', async () => {
    server.use(http.get('/api/v1/printer-tags', () => HttpResponse.json({ tags })), noLocations);
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const field = (await screen.findByLabelText('Limit for Phase 1')) as HTMLInputElement;
    expect(field.max).toBe('3');
    await userEvent.clear(field);
    await userEvent.type(field, '9');
    expect(onChange).toHaveBeenCalledWith('stagger_tag_limits', '{"1":3}');
    // Written AND shown: a field that kept displaying 9 would be the same lie
    // one keystroke later.
    expect(field.value).toBe('3');
  });

  it('writes the location limit under its own key and clears it on unpick', async () => {
    server.use(http.get('/api/v1/printer-locations', () => HttpResponse.json({ locations: [
      { id: 1, name: 'Room A', parent_id: null, path: 'Room A', depth: 1, printer_count: 1, sensor_count: 0, queued_count: 0 },
    ] })), noTags);
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const field = await screen.findByLabelText('Limit for Room A');
    await userEvent.clear(field);
    await userEvent.type(field, '3');
    expect(onChange).toHaveBeenCalledWith('stagger_location_limits', '{"1":3}');

    onChange.mockClear();
    await userEvent.click(screen.getByRole('checkbox', { name: /Room A/ }));
    expect(onChange).toHaveBeenCalledWith('stagger_group_location_ids', '[]');
    expect(onChange).toHaveBeenCalledWith('stagger_location_limits', '{}');
  });
});
