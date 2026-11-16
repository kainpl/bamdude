/**
 * The editor's picture is the one the design's printer makes.
 *
 * A design for a driver prints as a PDF, in colour; a design for a thermal
 * head prints as one bit. The picture used to be one bit for both, so a driver
 * design built around its colour block was shown with a hole where the block
 * goes. The editor now asks for the picture its design's target prints.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { render } from '../utils';
import { LabelTemplateEditor } from '../../components/labels/LabelTemplateEditor';
import { api } from '../../api/client';

// Konva needs a real canvas; the drawing surface is not what is under test.
vi.mock('../../components/labels/LabelCanvas', () => ({ LabelCanvas: () => null }));

const DRIVER = {
  id: 4,
  name: 'Shelf tag',
  description: '',
  width_mm: 40,
  height_mm: 20,
  shape: 'rect',
  target: 'driver',
  elements: [],
  builtin_key: null,
  is_builtin: false,
};

const THERMAL = { ...DRIVER, id: 5, name: 'Roll tag', target: 'thermal' };

describe("the label editor's picture", () => {
  let preview: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getLabelTemplates').mockResolvedValue([DRIVER, THERMAL] as never);
    vi.spyOn(api, 'getLabelPlaceholders').mockResolvedValue([] as never);
    vi.spyOn(api, 'getUiPreferences').mockResolvedValue({} as never);
    preview = vi.spyOn(api, 'previewLabelTemplate').mockResolvedValue({ blob: new Blob(), warnings: [] } as never);
    Object.defineProperty(window.URL, 'createObjectURL', { value: vi.fn(() => 'blob:mock'), configurable: true });
    Object.defineProperty(window.URL, 'revokeObjectURL', { value: vi.fn(), configurable: true });
  });

  const open = async (name: string) => {
    render(<LabelTemplateEditor />);
    await userEvent.click(await screen.findByRole('button', { name: new RegExp(name) }));
    await waitFor(() => expect(preview).toHaveBeenCalled());
    return preview.mock.calls.at(-1)?.[0] as { colour?: boolean; template: { target: string } };
  };

  it('is in colour for a design printed through a driver', async () => {
    const asked = await open('Shelf tag');

    expect(asked.template.target).toBe('driver');
    expect(asked.colour).toBe(true);
  });

  it('stays one bit for a design printed on a thermal head', async () => {
    const asked = await open('Roll tag');

    expect(asked.template.target).toBe('thermal');
    expect(asked.colour).toBe(false);
  });
});
