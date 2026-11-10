import { render } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { api } from '../../api/client';
import { GitBackupSettings } from '../../components/GitBackupSettings';
import { LDAPSettings } from '../../components/LDAPSettings';
import { EmailSettings } from '../../components/EmailSettings';
import { LdapUserPicker } from '../../components/LdapUserPicker';

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ isAdmin: false, hasPermission: () => true }),
}));

describe('G01 A admin settings ceiling', () => {
  it('does not mount protected editors or make their requests for a custom role holding the underlying permissions', () => {
    const spies = [vi.spyOn(api, 'getGitBackupConfig'), vi.spyOn(api, 'getSettings'), vi.spyOn(api, 'getSMTPSettings'), vi.spyOn(api, 'searchLDAPDirectory')];
    try {
      const { container } = render(<><GitBackupSettings /><LDAPSettings /><EmailSettings /><LdapUserPicker onSuccess={() => {}} /></>);
      expect(container).toBeEmptyDOMElement();
      for (const spy of spies) expect(spy).not.toHaveBeenCalled();
    } finally {
      for (const spy of spies) spy.mockRestore();
    }
  });
});
