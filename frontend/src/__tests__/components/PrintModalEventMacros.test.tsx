import { describe, it, expect, vi } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { EventMacrosPanel } from '../../components/PrintModal/EventMacros';
import type { Macro } from '../../api/client';

const macro = (id: number, name: string, event: string, extra: Partial<Macro> = {}): Macro => ({
  id,
  name,
  description: null,
  printer_models: ['*'],
  swap_mode_only: false,
  swap_profile: null,
  event,
  action_type: 'mqtt_action',
  mqtt_action: 'print_speed',
  mqtt_action_param: '1',
  trigger_layer: null,
  delay_seconds: 0,
  gcode: '',
  is_custom: true,
  enabled: true,
  created_at: '',
  updated_at: '',
  ...extra,
});

describe('EventMacrosPanel', () => {
  it('renders one row per macro, with the detail that tells two apart', async () => {
    const user = userEvent.setup();
    render(
      <EventMacrosPanel
        macros={[macro(7, 'Silent from 50', 'layer_reached', { trigger_layer: 50 })]}
        selectedIds={[7]}
        onChange={vi.fn()}
      />
    );

    await user.click(screen.getByText('Macros'));
    expect(await screen.findByText('Silent from 50')).toBeInTheDocument();
    // The event and the layer are what tell two same-named macros apart.
    expect(screen.getByText(/Layer reached/)).toBeInTheDocument();
    expect(screen.getByText(/· Layer 50/)).toBeInTheDocument();
  });

  it('reports the new selection when a row is unticked', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <EventMacrosPanel
        macros={[macro(7, 'Lights out', 'print_started'), macro(9, 'Lights on', 'print_finished')]}
        selectedIds={[7, 9]}
        onChange={onChange}
      />
    );

    await user.click(screen.getByText('Macros'));
    await user.click(screen.getByLabelText('Lights out'));

    expect(onChange).toHaveBeenCalledWith([9]);
  });

  it('reports the new selection when a row is ticked', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <EventMacrosPanel
        macros={[macro(7, 'Lights out', 'print_started')]}
        selectedIds={[]}
        onChange={onChange}
      />
    );

    await user.click(screen.getByText('Macros'));
    await user.click(screen.getByLabelText('Lights out'));

    expect(onChange).toHaveBeenCalledWith([7]);
  });

  it('renders nothing when no macro applies to this printer', () => {
    render(<EventMacrosPanel macros={[]} selectedIds={[]} onChange={vi.fn()} />);
    expect(screen.queryByText('Macros')).not.toBeInTheDocument();
  });
});
