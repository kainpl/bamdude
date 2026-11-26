/**
 * N07 spec §F, §G (S40): the group mapping in the provider form.
 *
 * A damaged stored mapping is never overwritten by an ordinary save — only the
 * explicit replacement sends a new one — and clearing a mapping says that sync
 * stops while the groups users already have stay.
 */
import { describe, expect, it } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';

import { render } from '../utils';
import { server } from '../mocks/server';
import { OIDCProviderSettings } from '../../components/OIDCProviderSettings';
import { rowsToMapping } from '../../components/oidcGroupMapping';

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
  client_secret_configured: true,
  group_claim: 'groups',
};

function serve(providers: unknown[]) {
  const puts: Record<string, unknown>[] = [];
  server.use(
    http.get('/api/v1/auth/oidc/providers/all', () => HttpResponse.json(providers)),
    http.get('/api/v1/groups/', () =>
      HttpResponse.json([
        { id: 3, name: 'Team', permissions: [], is_system: false, user_count: 0 },
        { id: 1, name: 'Administrators', permissions: [], is_system: true, user_count: 1 },
      ]),
    ),
    http.put('/api/v1/auth/oidc/providers/:id', async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      puts.push(body);
      return HttpResponse.json({ ...BASE, id: 5, name: 'Saved', group_mapping: {}, ...body });
    }),
  );
  return puts;
}

async function openEdit(id: number) {
  fireEvent.click(await screen.findByTestId(`edit-provider-${id}`));
  return screen.findByTestId('oidc-group-mapping');
}

describe('OIDC group mapping', () => {
  it('keeps a damaged mapping on an ordinary save', async () => {
    const puts = serve([{ ...BASE, id: 5, name: 'Damaged', group_mapping: null, group_mapping_invalid: true }]);
    render(<OIDCProviderSettings />);
    expect(await screen.findByTestId('oidc-mapping-summary-5')).toHaveTextContent('Damaged');
    await openEdit(5);
    expect(screen.getByTestId('oidc-mapping-replace')).toBeInTheDocument();
    fireEvent.change(screen.getByDisplayValue('Damaged'), { target: { value: 'Renamed' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0].name).toBe('Renamed');
    expect('group_mapping' in puts[0]).toBe(false);
  });

  it('replaces a damaged mapping only through the explicit action', async () => {
    const puts = serve([{ ...BASE, id: 5, name: 'Damaged', group_mapping: null, group_mapping_invalid: true }]);
    render(<OIDCProviderSettings />);
    await openEdit(5);
    fireEvent.click(screen.getByTestId('oidc-mapping-replace'));
    fireEvent.click(screen.getByTestId('oidc-mapping-add'));
    fireEvent.change(screen.getByLabelText('IdP group'), { target: { value: ' ops ' } });
    await waitFor(() => expect(screen.getAllByRole('option', { name: 'Team' }).length).toBeGreaterThan(0));
    fireEvent.change(screen.getByLabelText('BamDude group'), { target: { value: '3' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0].group_mapping).toEqual({ ops: 3 });
  });

  it('warns before an empty mapping turns sync off', async () => {
    const puts = serve([{ ...BASE, id: 5, name: 'Mapped', group_mapping: { ops: 3 }, group_mapping_invalid: false }]);
    render(<OIDCProviderSettings />);
    await openEdit(5);
    await waitFor(() => expect(screen.getAllByRole('option', { name: 'Team' }).length).toBeGreaterThan(0));
    expect(screen.queryByTestId('oidc-mapping-clear-warning')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Remove mapping' }));
    expect(screen.getByTestId('oidc-mapping-clear-warning')).toBeInTheDocument();
    expect(screen.getByText(/no longer managed/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0].group_mapping).toEqual({});
  });

  it('shows the hint when Administrators is mapped', async () => {
    serve([{ ...BASE, id: 5, name: 'Admins', group_mapping: { admins: 1 }, group_mapping_invalid: false }]);
    render(<OIDCProviderSettings />);
    await openEdit(5);
    await waitFor(() => expect(screen.getByText(/separate local administrator/)).toBeInTheDocument());
  });

  it('shows an env-managed mapping read-only', async () => {
    serve([{ ...BASE, id: 7, name: 'Env', is_env_managed: true, group_mapping: { ops: 3 }, group_mapping_invalid: false }]);
    render(<OIDCProviderSettings />);
    expect(await screen.findByTestId('oidc-mapping-summary-7')).toHaveTextContent('ops → Team');
    expect(screen.queryByTestId('edit-provider-7')).not.toBeInTheDocument();
  });
});

describe('rowsToMapping', () => {
  it('drops empty rows and rows whose group no longer exists', () => {
    expect(
      rowsToMapping(
        [
          { idp: ' ops ', groupId: 3 },
          { idp: '', groupId: 3 },
          { idp: 'gone', groupId: 99 },
          { idp: 'none', groupId: '' },
        ],
        new Set([3]),
      ).mapping,
    ).toEqual({ ops: 3 });
  });

  it('judges no group deleted before the group list has loaded', () => {
    expect(rowsToMapping([{ idp: 'ops', groupId: 3 }], null).mapping).toEqual({ ops: 3 });
  });

  it('reports names two rows share after trimming instead of keeping the last', () => {
    const result = rowsToMapping(
      [
        { idp: 'Team', groupId: 2 },
        { idp: ' Team ', groupId: 3 },
      ],
      new Set([2, 3]),
    );
    expect(result.duplicates).toEqual(['Team']);
    expect(result.mapping).toEqual({});
  });

  it('keeps a group literally named __proto__ as an own property', () => {
    const { mapping } = rowsToMapping([{ idp: '__proto__', groupId: 2 }], new Set([2]));
    expect(Object.keys(mapping)).toEqual(['__proto__']);
    expect(JSON.stringify(mapping)).toBe('{"__proto__":2}');
  });
});

describe('OIDC group mapping — form guards', () => {
  it('refuses to save rows that collide after trimming', async () => {
    const puts = serve([{ ...BASE, id: 5, name: 'Mapped', group_mapping: { Team: 3 }, group_mapping_invalid: false }]);
    render(<OIDCProviderSettings />);
    await openEdit(5);
    await waitFor(() => expect(screen.getAllByRole('option', { name: 'Team' }).length).toBeGreaterThan(0));
    fireEvent.click(screen.getByTestId('oidc-mapping-add'));
    const names = screen.getAllByLabelText('IdP group');
    fireEvent.change(names[1], { target: { value: ' Team ' } });
    fireEvent.change(screen.getAllByLabelText('BamDude group')[1], { target: { value: '1' } });
    expect(screen.getByTestId('oidc-mapping-duplicates')).toHaveTextContent('Team');
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    expect(puts).toHaveLength(0);
  });

  it('marks a deleted group in the read-only summary', async () => {
    serve([{ ...BASE, id: 8, name: 'Env gone', is_env_managed: true, group_mapping: { ops: 42 }, group_mapping_invalid: false }]);
    render(<OIDCProviderSettings />);
    await waitFor(() => expect(screen.getByTestId('oidc-mapping-summary-8')).toHaveTextContent('Group deleted'));
  });
});
