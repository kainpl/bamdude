/**
 * The env-managed OIDC provider is shown, but not offered for editing (#2593).
 *
 * Startup rewrites that row from BAMDUDE_OIDC_* on every boot and the API
 * answers 409, so a toggle or an edit button here would promise a change that
 * cannot land. Hidden rather than disabled: a greyed-out control invites a
 * second attempt and explains nothing.
 */
import { describe, expect, it } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';

import { render } from '../utils';
import { server } from '../mocks/server';
import { OIDCProviderSettings } from '../../components/OIDCProviderSettings';

const BASE = {
  issuer_url: 'https://id.example.com',
  client_id: 'bamdude',
  scopes: 'openid email profile',
  is_enabled: true,
  auto_create_users: false,
  auto_link_existing_accounts: false,
  email_claim: 'email',
  require_email_verified: true,
  icon_url: null,
  has_icon: false,
  default_group_id: null,
  is_autologin: false,
};

function serveProviders(providers: unknown[]) {
  server.use(
    http.get('/api/v1/auth/oidc/providers/all', () => HttpResponse.json(providers)),
    http.get('/api/v1/auth/oidc/providers', () => HttpResponse.json(providers)),
    http.get('/api/v1/auth/groups', () => HttpResponse.json([])),
  );
}

describe('OIDCProviderSettings — env-managed lock', () => {
  it('marks the env-managed provider and withholds its controls', async () => {
    serveProviders([{ ...BASE, id: 1, name: 'Authentik', is_env_managed: true }]);
    render(<OIDCProviderSettings />);

    await waitFor(() => {
      expect(screen.getByText('Authentik')).toBeInTheDocument();
    });
    expect(screen.getByText('From environment')).toBeInTheDocument();
    // The API answers 409 to both of these, so they are not offered at all.
    expect(screen.queryByTestId('edit-provider-1')).not.toBeInTheDocument();
    expect(screen.queryByTestId('delete-provider-1')).not.toBeInTheDocument();
  });

  it('leaves an ordinary provider fully editable', async () => {
    // The gate must key on the flag, not on "an OIDC provider exists".
    serveProviders([{ ...BASE, id: 2, name: 'Hand made', is_env_managed: false }]);
    render(<OIDCProviderSettings />);

    await waitFor(() => {
      expect(screen.getByText('Hand made')).toBeInTheDocument();
    });
    expect(screen.queryByText('From environment')).not.toBeInTheDocument();
    expect(screen.getByTestId('edit-provider-2')).toBeInTheDocument();
    expect(screen.getByTestId('delete-provider-2')).toBeInTheDocument();
  });

  it('locks only the env-managed row when both kinds are present', async () => {
    serveProviders([
      { ...BASE, id: 1, name: 'Authentik', is_env_managed: true },
      { ...BASE, id: 2, name: 'Hand made', is_env_managed: false },
    ]);
    render(<OIDCProviderSettings />);

    await waitFor(() => {
      expect(screen.getByText('Authentik')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('edit-provider-1')).not.toBeInTheDocument();
    expect(screen.getByTestId('edit-provider-2')).toBeInTheDocument();
  });

  it('treats a missing flag as not managed', async () => {
    // An older backend omits the field entirely; the row must stay editable
    // rather than silently locking itself.
    serveProviders([{ ...BASE, id: 3, name: 'Legacy' }]);
    render(<OIDCProviderSettings />);

    await waitFor(() => {
      expect(screen.getByText('Legacy')).toBeInTheDocument();
    });
    expect(screen.getByTestId('edit-provider-3')).toBeInTheDocument();
  });
});
