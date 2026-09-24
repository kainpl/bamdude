/**
 * A typed chamber target of 0 and a blank one do different things since a
 * derived 0 skips preheat entirely (upstream #3041): the typed 0 still heats the
 * bed and runs the soak. A user reaching for 0 to switch preheat off got the
 * delay instead — the field now says which is which.
 */
import { describe, it, expect, vi } from 'vitest';
import { screen } from '@testing-library/react';
import { render } from '../utils';
import { PrintOptionsPanel } from '../../components/PrintModal/PrintOptions';
import { DEFAULT_PRINT_OPTIONS } from '../../components/PrintModal/types';

describe('PrintOptionsPanel — the chamber target override explains 0 against blank', () => {
  it('says what a typed 0 does and what leaving it blank does', () => {
    render(<PrintOptionsPanel options={{ ...DEFAULT_PRINT_OPTIONS, preheat_override: 'on' }} onChange={vi.fn()} defaultExpanded />);

    expect(screen.getByText(/0 heats the bed and runs the soak without the chamber/)).toBeInTheDocument();
  });

  it('has nothing to explain when preheat is off for the print', () => {
    render(<PrintOptionsPanel options={{ ...DEFAULT_PRINT_OPTIONS, preheat_override: 'off' }} onChange={vi.fn()} defaultExpanded />);

    expect(screen.queryByText(/0 heats the bed/)).not.toBeInTheDocument();
  });
});
