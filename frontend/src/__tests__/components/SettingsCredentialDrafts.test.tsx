import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { api } from '../../api/client';
import { EmailSettings } from '../../components/EmailSettings';
import { LDAPSettings } from '../../components/LDAPSettings';

describe('Settings credential draft tests', () => {
  beforeEach(() => { vi.restoreAllMocks(); });

  it('tests an SMTP replacement draft without saving or copying configured flags into the write', async () => {
    vi.spyOn(api, 'getSMTPSettings').mockResolvedValue({ smtp_host: 'mail.old', smtp_port: 587,
      smtp_username: 'operator', smtp_security: 'starttls', smtp_auth_enabled: true,
      smtp_from_email: 'from@example.org', smtp_from_name: 'BamDude', smtp_password_configured: true });
    vi.spyOn(api, 'getAdvancedAuthStatus').mockResolvedValue({ advanced_auth_enabled: false, smtp_configured: true } as never);
    const save = vi.spyOn(api, 'saveSMTPSettings').mockResolvedValue({ message: 'ok' });
    const probe = vi.spyOn(api, 'testSMTP').mockResolvedValue({ success: true, message: 'ok' });
    render(<EmailSettings />);
    fireEvent.change(await screen.findByDisplayValue('mail.old'), { target: { value: 'mail.draft' } });
    fireEvent.change(screen.getByLabelText('Credential action'), { target: { value: 'replace' } });
    fireEvent.change(screen.getByLabelText('New credential'), { target: { value: 'draft-canary' } });
    fireEvent.change(screen.getByPlaceholderText('test@example.com'), { target: { value: 'to@example.org' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send Test Email' }));
    await waitFor(() => expect(probe).toHaveBeenCalledOnce());
    expect(probe.mock.calls[0][0]).toMatchObject({ test_recipient: 'to@example.org', settings: {
      smtp_host: 'mail.draft', smtp_password: 'draft-canary', secret_actions: { smtp_password: 'replace' } } });
    expect(probe.mock.calls[0][0].settings).not.toHaveProperty('smtp_password_configured');
    await act(() => new Promise(resolve => setTimeout(resolve, 750)));
    expect(save).not.toHaveBeenCalled();
  });

  it('tests a configured LDAP credential by keep without exposing or saving it', async () => {
    vi.spyOn(api, 'getSettings').mockResolvedValue({ ldap_enabled: true, ldap_server_url: 'ldap://directory.old',
      ldap_bind_dn: 'cn=operator', ldap_search_base: 'dc=example', ldap_bind_password_configured: true } as never);
    vi.spyOn(api, 'getLDAPStatus').mockResolvedValue({ ldap_enabled: true, ldap_configured: true } as never);
    vi.spyOn(api, 'getGroups').mockResolvedValue([]);
    const save = vi.spyOn(api, 'updateSettings').mockResolvedValue({} as never);
    const probe = vi.spyOn(api, 'testLDAP').mockResolvedValue({ success: true, message: 'ok' });
    render(<LDAPSettings />);
    await screen.findByDisplayValue('cn=operator');
    expect(screen.queryByLabelText('New credential')).toBeNull();
    fireEvent.change(screen.getByPlaceholderText('ldaps://ldap.example.com:636'), { target: { value: 'ldap://directory.draft' } });
    fireEvent.click(screen.getByRole('button', { name: 'Test Connection' }));
    await waitFor(() => expect(probe).toHaveBeenCalledOnce());
    expect(probe.mock.calls[0][0]).toMatchObject({ ldap_server_url: 'ldap://directory.draft',
      secret_actions: { ldap_bind_password: 'keep' } });
    expect(probe.mock.calls[0][0]).not.toHaveProperty('ldap_bind_password');
    await act(() => new Promise(resolve => setTimeout(resolve, 750)));
    expect(save).not.toHaveBeenCalled();
  });

  it('disables SMTP authentication without automatically clearing the account or retained credential', async () => {
    vi.spyOn(api, 'getSMTPSettings').mockResolvedValue({ smtp_host: 'mail.old', smtp_port: 587,
      smtp_username: 'operator', smtp_security: 'starttls', smtp_auth_enabled: true,
      smtp_from_email: 'from@example.org', smtp_from_name: 'BamDude', smtp_password_configured: true });
    vi.spyOn(api, 'getAdvancedAuthStatus').mockResolvedValue({ advanced_auth_enabled: false, smtp_configured: true } as never);
    const save = vi.spyOn(api, 'saveSMTPSettings').mockResolvedValue({ message: 'ok' });
    render(<EmailSettings />);
    await screen.findByDisplayValue('operator');
    fireEvent.change(screen.getByDisplayValue('Enabled'), { target: { value: 'false' } });
    await act(() => new Promise(resolve => setTimeout(resolve, 750)));
    expect(save).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Save Settings' }));
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    expect(save.mock.calls[0][0]).toMatchObject({ smtp_auth_enabled: false, smtp_username: 'operator',
      secret_actions: { smtp_password: 'keep' } });
    expect(save.mock.calls[0][0]).not.toHaveProperty('smtp_password');
  });

  it('keeps an opaque legacy LDAP URL together with its bind credential without reconstructing either', async () => {
    vi.spyOn(api, 'getSettings').mockResolvedValue({ ldap_enabled: true, ldap_server_url: '',
      ldap_server_url_opaque: true, ldap_server_url_configured: true, ldap_bind_dn: 'cn=operator',
      ldap_search_base: 'dc=example', ldap_bind_password_configured: true } as never);
    vi.spyOn(api, 'getLDAPStatus').mockResolvedValue({ ldap_enabled: true, ldap_configured: true } as never);
    vi.spyOn(api, 'getGroups').mockResolvedValue([]);
    const probe = vi.spyOn(api, 'testLDAP').mockResolvedValue({ success: true, message: 'ok' });
    render(<LDAPSettings />);
    await screen.findByDisplayValue('cn=operator');
    expect(screen.getAllByLabelText('Credential action')).toHaveLength(2);
    expect(screen.queryByLabelText('New credential')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Test Connection' }));
    await waitFor(() => expect(probe).toHaveBeenCalledOnce());
    expect(probe.mock.calls[0][0]).not.toHaveProperty('ldap_server_url');
    expect(probe.mock.calls[0][0]).not.toHaveProperty('ldap_bind_password');
    expect(probe.mock.calls[0][0]?.secret_actions).toEqual({ ldap_server_url: 'keep', ldap_bind_password: 'keep' });
  });
});
