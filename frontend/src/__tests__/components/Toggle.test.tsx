/**
 * Tests for the Toggle component.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Toggle } from '../../components/Toggle';

describe('Toggle', () => {
  it('renders in unchecked state', () => {
    render(<Toggle checked={false} onChange={() => {}} />);

    const toggle = screen.getByRole('switch');
    expect(toggle).toHaveAttribute('aria-checked', 'false');
  });

  it('renders in checked state', () => {
    render(<Toggle checked={true} onChange={() => {}} />);

    const toggle = screen.getByRole('switch');
    expect(toggle).toHaveAttribute('aria-checked', 'true');
  });

  it('calls onChange with true when clicking unchecked toggle', async () => {
    const user = userEvent.setup();
    const handleChange = vi.fn();

    render(<Toggle checked={false} onChange={handleChange} />);

    await user.click(screen.getByRole('switch'));

    expect(handleChange).toHaveBeenCalledWith(true);
    expect(handleChange).toHaveBeenCalledTimes(1);
  });

  it('calls onChange with false when clicking checked toggle', async () => {
    const user = userEvent.setup();
    const handleChange = vi.fn();

    render(<Toggle checked={true} onChange={handleChange} />);

    await user.click(screen.getByRole('switch'));

    expect(handleChange).toHaveBeenCalledWith(false);
    expect(handleChange).toHaveBeenCalledTimes(1);
  });

  it('does not call onChange when disabled', async () => {
    const user = userEvent.setup();
    const handleChange = vi.fn();

    render(<Toggle checked={false} onChange={handleChange} disabled />);

    await user.click(screen.getByRole('switch'));

    expect(handleChange).not.toHaveBeenCalled();
  });

  it('has disabled attribute when disabled prop is true', () => {
    render(<Toggle checked={false} onChange={() => {}} disabled />);

    const toggle = screen.getByRole('switch');
    expect(toggle).toBeDisabled();
  });

  it('applies correct styles when checked', () => {
    render(<Toggle checked={true} onChange={() => {}} />);

    const toggle = screen.getByRole('switch');
    expect(toggle.className).toContain('bg-bambu-green');
  });

  it('applies correct styles when unchecked', () => {
    render(<Toggle checked={false} onChange={() => {}} />);

    const toggle = screen.getByRole('switch');
    expect(toggle.className).toContain('bg-bambu-dark-tertiary');
  });

  it('applies disabled styles when disabled', () => {
    render(<Toggle checked={false} onChange={() => {}} disabled />);

    const toggle = screen.getByRole('switch');
    expect(toggle.className).toContain('cursor-not-allowed');
    expect(toggle.className).toContain('opacity-50');
  });

  it('stops event propagation on click', async () => {
    const user = userEvent.setup();
    const handleParentClick = vi.fn();
    const handleChange = vi.fn();

    render(
      <div onClick={handleParentClick}>
        <Toggle checked={false} onChange={handleChange} />
      </div>
    );

    await user.click(screen.getByRole('switch'));

    expect(handleChange).toHaveBeenCalled();
    expect(handleParentClick).not.toHaveBeenCalled();
  });
});
