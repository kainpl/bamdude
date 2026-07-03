/**
 * Tests for the FilamentSlotCircle component.
 */

import { describe, it, expect } from 'vitest';
import { screen } from '@testing-library/react';
import { render } from '../utils';
import { FilamentSlotCircle } from '../../components/FilamentSlotCircle';

/**
 * JSDOM normalizes some CSS color values (e.g. #000 → rgb(0, 0, 0)),
 * so we compare against both hex and rgb forms.
 */
function expectColor(actual: string, hex: string, rgb: string) {
  expect([hex, rgb]).toContain(actual);
}

describe('FilamentSlotCircle', () => {
  it('renders the slot number', () => {
    render(<FilamentSlotCircle trayColor="FF0000" trayType="PLA" isEmpty={false} slotNumber={1} />);
    expect(screen.getByText('1')).toBeInTheDocument();
  });

  it('renders slot number for empty slot', () => {
    render(<FilamentSlotCircle isEmpty={true} slotNumber={3} />);
    expect(screen.getByText('3')).toBeInTheDocument();
  });

  it('uses dashed border for empty slots', () => {
    const { container } = render(
      <FilamentSlotCircle isEmpty={true} slotNumber={1} />
    );
    const circle = container.firstChild as HTMLElement;
    expect(circle.style.borderStyle).toBe('dashed');
  });

  it('uses solid border for filled slots', () => {
    const { container } = render(
      <FilamentSlotCircle trayColor="FF0000" isEmpty={false} slotNumber={1} />
    );
    const circle = container.firstChild as HTMLElement;
    expect(circle.style.borderStyle).toBe('solid');
  });

  it('uses a solid amber border for a loaded-but-unconfigured slot (emptyKind=reset)', () => {
    // #1694: a spool is physically loaded but has no type set — must stand out
    // from a truly empty slot's dashed grey.
    const { container } = render(
      <FilamentSlotCircle isEmpty={true} slotNumber={1} emptyKind="reset" />
    );
    const circle = container.firstChild as HTMLElement;
    expect(circle.style.borderStyle).toBe('solid');
    expectColor(circle.style.borderColor, '#f59e0b', 'rgb(245, 158, 11)');
  });

  it('keeps the dashed grey border for a firmware-empty slot (emptyKind=physical)', () => {
    const { container } = render(
      <FilamentSlotCircle isEmpty={true} slotNumber={1} emptyKind="physical" />
    );
    const circle = container.firstChild as HTMLElement;
    expect(circle.style.borderStyle).toBe('dashed');
  });

  it('sets background color from trayColor', () => {
    const { container } = render(
      <FilamentSlotCircle trayColor="00FF00" trayType="PLA" isEmpty={false} slotNumber={2} />
    );
    const circle = container.firstChild as HTMLElement;
    expectColor(circle.style.backgroundColor, '#00FF00', 'rgb(0, 255, 0)');
  });

  it('uses dark background when trayType is set but no color', () => {
    const { container } = render(
      <FilamentSlotCircle trayType="PLA" isEmpty={false} slotNumber={1} />
    );
    const circle = container.firstChild as HTMLElement;
    expectColor(circle.style.backgroundColor, '#333', 'rgb(51, 51, 51)');
  });

  it('uses transparent background when empty and no type', () => {
    const { container } = render(
      <FilamentSlotCircle isEmpty={true} slotNumber={1} />
    );
    const circle = container.firstChild as HTMLElement;
    expect(circle.style.backgroundColor).toBe('transparent');
  });

  it('uses black text on light filament colors', () => {
    // White filament (FFFFFF) is light
    render(<FilamentSlotCircle trayColor="FFFFFF" isEmpty={false} slotNumber={1} />);
    const text = screen.getByText('1');
    expectColor(text.style.color, '#000', 'rgb(0, 0, 0)');
  });

  it('uses white text on dark filament colors', () => {
    // Black filament (000000) is dark
    render(<FilamentSlotCircle trayColor="000000" isEmpty={false} slotNumber={1} />);
    const text = screen.getByText('1');
    expectColor(text.style.color, '#fff', 'rgb(255, 255, 255)');
  });

  it('uses white text when no tray color', () => {
    render(<FilamentSlotCircle isEmpty={true} slotNumber={1} />);
    const text = screen.getByText('1');
    expectColor(text.style.color, '#fff', 'rgb(255, 255, 255)');
  });
});
