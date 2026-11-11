import { useState } from 'react';
import { describe, expect, it } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
import { render } from '../utils';
import { SecretInput } from '../../components/SecretInput';
import { credentialDraft } from '../../utils/credentialDraft';
import type { SecretAction } from '../../api/client';

describe('write-only credential editor', () => {
  it('keeps by presence, never invents a reusable mask, and requires an explicit replacement', () => {
    function Editor() {
      const [action, setAction] = useState<SecretAction>('keep');
      const [value, setValue] = useState('');
      return <SecretInput value={value} action={action} configured onActionChange={setAction} onChange={setValue} />;
    }
    render(<Editor />);
    expect(screen.getByText('Credential configured')).toBeInTheDocument();
    expect(screen.queryByLabelText('New credential')).toBeNull();
    fireEvent.change(screen.getByLabelText('Credential action'), { target: { value: 'replace' } });
    fireEvent.change(screen.getByLabelText('New credential'), { target: { value: 'real-asterisk-***' } });
    expect(screen.getByLabelText('New credential')).toHaveValue('real-asterisk-***');
    fireEvent.change(screen.getByLabelText('Credential action'), { target: { value: 'clear' } });
    expect(screen.queryByLabelText('New credential')).toBeNull();
  });

  it('omits secret values for keep/clear and sends literal replacements including asterisks', () => {
    expect(credentialDraft('token', '', 'keep', true)).toEqual({ secret_actions: { token: 'keep' } });
    expect(credentialDraft('token', '', 'keep', false)).toEqual({});
    expect(credentialDraft('token', 'discarded', 'clear', true)).toEqual({ secret_actions: { token: 'clear' } });
    expect(credentialDraft('token', '***', 'replace', true)).toEqual({ token: '***', secret_actions: { token: 'replace' } });
    expect(credentialDraft('token', '', 'replace', true)).toEqual({ token: '', secret_actions: { token: 'replace' } });
  });
});
