/**
 * Tests for PasswordField — the shared account-password input.
 *
 * The regression these guard against: every create/change-password form
 * disabled its submit button on the complexity rules while the only message
 * naming the unmet rule lived behind that same button, so a rejected password
 * produced a grey button and no text anywhere on screen.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { screen, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { PasswordField } from '../../components/PasswordField';

const RULES = [
  'At least 8 characters',
  'An upper-case letter',
  'A lower-case letter',
  'A digit',
];

describe('PasswordField', () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('masks the value and reveals it on demand', async () => {
    const user = userEvent.setup();
    render(
      <PasswordField label="Password" value="hunter2" onChange={vi.fn()} />
    );

    const input = screen.getByLabelText('Password');
    expect(input).toHaveAttribute('type', 'password');

    await user.click(screen.getByRole('button', { name: 'Show password' }));
    expect(input).toHaveAttribute('type', 'text');

    await user.click(screen.getByRole('button', { name: 'Hide password' }));
    expect(input).toHaveAttribute('type', 'password');
  });

  describe('requirements', () => {
    it('lists every rule, not only the first one blocking', () => {
      // Being told "at least 8 characters", typing eight and only then hearing
      // about the digit is the same dead end one step later.
      render(
        <PasswordField label="Password" value="short" onChange={vi.fn()} showRules />
      );
      RULES.forEach((rule) => expect(screen.getByText(rule)).toBeInTheDocument());
    });

    it('marks a rule as met once it is satisfied', () => {
      render(
        <PasswordField label="Password" value="lowercase1" onChange={vi.fn()} showRules />
      );
      // 10 chars, a lower-case letter and a digit — only the upper case is left.
      expect(screen.getByText('At least 8 characters').closest('li')).toHaveTextContent('(done)');
      expect(screen.getByText('A lower-case letter').closest('li')).toHaveTextContent('(done)');
      expect(screen.getByText('A digit').closest('li')).toHaveTextContent('(done)');
      expect(screen.getByText('An upper-case letter').closest('li')).toHaveTextContent(
        '(still needed)'
      );
    });

    it('keeps showing them once every rule passes', () => {
      // Not a reason to hide the list — a field that empties its help the
      // moment it is happy makes the user wonder what changed.
      render(
        <PasswordField label="Password" value="Abcdef12" onChange={vi.fn()} showRules />
      );
      RULES.forEach((rule) =>
        expect(screen.getByText(rule).closest('li')).toHaveTextContent('(done)')
      );
    });

    it('says nothing on an untouched field', () => {
      render(
        <PasswordField label="Password" value="" onChange={vi.fn()} showRules />
      );
      // An empty field is not a mistake — it is one the user has not reached.
      expect(screen.queryByText('At least 8 characters')).not.toBeInTheDocument();
    });

    it('appears as soon as the field is focused, before anything is typed', async () => {
      const user = userEvent.setup();
      render(
        <PasswordField label="Password" value="" onChange={vi.fn()} showRules />
      );
      await user.click(screen.getByLabelText('Password'));
      RULES.forEach((rule) => expect(screen.getByText(rule)).toBeInTheDocument());
    });

    it('stays away without showRules, for a current-password field', () => {
      // The rules describe the NEW password; an account that predates them
      // still signs in with what it has.
      render(<PasswordField label="Current" value="old" onChange={vi.fn()} showRules={false} />);
      expect(screen.queryByText('At least 8 characters')).not.toBeInTheDocument();
    });

    it('points the input at the list for screen readers', () => {
      render(
        <PasswordField id="pw" label="Password" value="short" onChange={vi.fn()} showRules />
      );
      expect(screen.getByLabelText('Password')).toHaveAttribute('aria-describedby', 'pw-rules');
    });

    it('does not mark the field invalid while the user is still typing', () => {
      // A half-typed password is unfinished, not wrong; the red border belongs
      // to a confirmation that genuinely disagrees.
      render(
        <PasswordField label="Password" value="short" onChange={vi.fn()} showRules />
      );
      expect(screen.getByLabelText('Password')).not.toHaveAttribute('aria-invalid');
    });
  });

  describe('confirmation', () => {
    it('reports a confirmation that does not match', () => {
      render(
        <PasswordField
          id="confirm"
          label="Confirm"
          value="Abcdef12"
          onChange={vi.fn()}
          mustMatch="Abcdef13"
        />
      );
      expect(screen.getByText('Passwords do not match')).toBeInTheDocument();
      const input = screen.getByLabelText('Confirm');
      expect(input).toHaveAttribute('aria-invalid', 'true');
      expect(input).toHaveAttribute('aria-describedby', 'confirm-error');
    });

    it('accepts a matching confirmation', () => {
      const { container } = render(
        <PasswordField
          label="Confirm"
          value="Abcdef12"
          onChange={vi.fn()}
          mustMatch="Abcdef12"
        />
      );
      expect(container.querySelector('p')).toBeNull();
    });

    it('says nothing about an empty confirmation', () => {
      const { container } = render(
        <PasswordField label="Confirm" value="" onChange={vi.fn()} mustMatch="Abcdef12" />
      );
      expect(container.querySelector('p')).toBeNull();
    });
  });

  it('keeps the reveal button out of the tab order', () => {
    // Tabbing out of a password field should reach the next field, not a
    // control that only changes how this one looks.
    render(<PasswordField label="Password" value="x" onChange={vi.fn()} />);
    expect(screen.getByRole('button', { name: 'Show password' })).toHaveAttribute(
      'tabindex',
      '-1'
    );
  });

  it('reports every keystroke', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<PasswordField label="Password" value="" onChange={onChange} />);
    await user.type(screen.getByLabelText('Password'), 'a');
    expect(onChange).toHaveBeenCalledWith('a');
  });
});
