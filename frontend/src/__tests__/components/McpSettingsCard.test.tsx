import { beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { McpSettingsCard } from '../../components/settings/McpSettingsCard';

let denied = new Set<string>();
vi.mock('../../contexts/AuthContext', async importOriginal => ({
  ...await importOriginal<typeof import('../../contexts/AuthContext')>(),
  useAuth: () => ({ isAdmin: true, hasPermission: (permission: string) => !denied.has(permission) }),
}));

const endpoint = 'https://saved.example/mcp';
const key = { id: 41, name: 'Assistant', enabled: true, can_access_mcp: true, key: 'bd_CANARY_keep_out_of_config' };

describe('McpSettingsCard', () => {
  beforeEach(() => {
    denied = new Set();
    server.use(
      http.get('/api/v1/settings/', () => HttpResponse.json({ mcp_enabled: true, mcp_endpoint_url: endpoint, mcp_allowed_hosts: ['saved.example'], mcp_allowed_origins: [] })),
      http.get('/api/v1/integrations/mcp/status', () => HttpResponse.json({ state: 'ready', reason: null, desired_enabled: true, diagnostic_contract: 1, skill_contract: 1, registered_modules: ['base'] })),
      http.get('/api/v1/integrations/mcp/packages', () => HttpResponse.json({ package_version: 'test', packages: [{ id: 'skill', available: true }, { id: 'portable-plugin', available: true }] })),
      http.get('/api/v1/integrations/mcp/keys/41/connection', () => HttpResponse.json({ observed_at: null })),
      http.get('/api/v1/api-keys/', () => HttpResponse.json([key])),
      http.get('/api/v1/printers/', () => HttpResponse.json([{ id: 10, name: 'P1S test' }, { id: 20, name: 'A1 test' }])),
    );
  });

  it('distinguishes a ready server, an unobserved client and manual skill installation', async () => {
    render(<McpSettingsCard />);
    expect(await screen.findByText('Server ready')).toBeInTheDocument();
    expect(screen.getByText('No MCP request observed since this server started')).toBeInTheDocument();
    expect(screen.getByLabelText(/I installed the skill/)).not.toBeChecked();
    expect(screen.getByText(/Portable plugin install\/remove and skill discovery were verified/)).toBeInTheDocument();
  });

  it('creates the least-privilege key for only selected printers with logs off', async () => {
    let payload: Record<string, unknown> | undefined;
    server.use(http.post('/api/v1/api-keys/', async ({ request }) => { payload = await request.json() as Record<string, unknown>; return HttpResponse.json(key); }));
    const user = userEvent.setup();
    render(<McpSettingsCard />);
    await user.click(await screen.findByLabelText('P1S test'));
    await user.click(screen.getByRole('button', { name: 'Create diagnostic key' }));
    await screen.findByText(key.key);
    expect(payload?.printer_ids).toEqual([10]);
    expect(payload?.can_access_mcp).toBe(true);
    expect(payload?.can_read_status).toBe(true);
    for (const [name, value] of Object.entries(payload ?? {})) if (name.startsWith('can_') && !['can_access_mcp', 'can_read_status'].includes(name)) expect(value, name).toBe(false);
    expect(screen.getByLabelText(/Also allow diagnostic logs/)).not.toBeChecked();
  });

  it('dismisses the one-time key and releases its mutation-cache copy', async () => {
    server.use(http.post('/api/v1/api-keys/', () => HttpResponse.json(key)));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    const user = userEvent.setup();
    render(<QueryClientProvider client={client}><McpSettingsCard /></QueryClientProvider>);
    await user.click(await screen.findByLabelText('P1S test'));
    await user.click(screen.getByRole('button', { name: 'Create diagnostic key' }));
    await screen.findByText(key.key);
    expect(JSON.stringify(client.getMutationCache().getAll().map(item => item.state.data))).toContain(key.key);
    await user.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(screen.queryByText(key.key)).not.toBeInTheDocument();
    await waitFor(() => expect(JSON.stringify(client.getMutationCache().getAll().map(item => item.state.data))).not.toContain(key.key));
  });

  it('does not equate REST key usage with an MCP observation', async () => {
    const user = userEvent.setup();
    render(<McpSettingsCard />);
    await screen.findByRole('option', { name: 'Assistant' });
    await user.selectOptions(await screen.findByLabelText('Connection key'), '41');
    expect(await screen.findByText('No MCP request observed since this server started')).toBeInTheDocument();
    server.use(http.get('/api/v1/integrations/mcp/keys/41/connection', () => HttpResponse.json({ observed_at: '2026-11-01T12:00:00Z' })));
    await user.click(screen.getByRole('button', { name: 'Check connection' }));
    expect(await screen.findByText(/MCP request observed:/)).toBeInTheDocument();
    expect(screen.getByLabelText(/I installed the skill/)).not.toBeChecked();
    server.use(http.get('/api/v1/integrations/mcp/keys/41/connection', () => new HttpResponse(null, { status: 503 })));
    await user.click(screen.getByRole('button', { name: 'Check connection' }));
    expect(await screen.findByText('Could not read the connection marker. Try again.')).toBeInTheDocument();
    expect(screen.queryByText(/MCP request observed:/)).not.toBeInTheDocument();
  });

  it('saves an explicit canonical address and host, never the current browser host', async () => {
    let payload: Record<string, unknown> | undefined;
    server.use(http.put('/api/v1/settings/', async ({ request }) => { payload = await request.json() as Record<string, unknown>; return HttpResponse.json(payload); }));
    const user = userEvent.setup();
    render(<McpSettingsCard />);
    const input = await screen.findByDisplayValue(endpoint);
    await user.clear(input);
    await user.type(input, 'https://other.example:8443/mcp');
    await user.click(screen.getByRole('button', { name: 'Save MCP settings' }));
    await waitFor(() => expect(payload?.mcp_endpoint_url).toBe('https://other.example:8443/mcp'));
    expect(payload?.mcp_allowed_hosts).toEqual(['saved.example', 'other.example:8443']);
    expect(payload?.mcp_allowed_origins).toEqual([]);
  });

  it('does not render or fetch setup without settings-read permission', async () => {
    denied.add('settings:read');
    const { container } = render(<McpSettingsCard />);
    expect(container).toBeEmptyDOMElement();
  });

  it('enforces distinct settings-write, key-create and marker permissions in the UI', async () => {
    denied = new Set(['settings:update', 'api_keys:create', 'api_keys:update']);
    render(<McpSettingsCard />);
    expect(await screen.findByRole('switch', { name: 'Enable MCP' })).toBeDisabled();
    expect(screen.getByRole('switch', { name: 'Allow unencrypted HTTP addresses for MCP' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Save MCP settings' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Create diagnostic key' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Check connection' })).toBeDisabled();
  });

  it('explains unsupported ChatGPT and keeps installation separate from connection', async () => {
    const user = userEvent.setup();
    render(<McpSettingsCard />);
    await user.selectOptions(screen.getByLabelText('AI client'), 'chatgpt');
    expect(screen.getByText(/ChatGPT cloud connection is not supported in v1/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Download client package' })).not.toBeInTheDocument();
    expect(screen.getByText(/Never disable TLS verification/)).toBeInTheDocument();
  });

  it('manual command uses only the saved address and no one-time secret', async () => {
    const user = userEvent.setup();
    render(<McpSettingsCard />);
    await screen.findByDisplayValue(endpoint);
    await user.click(screen.getByText('Manual connection'));
    const details = screen.getByText('Manual connection').closest('details')!;
    expect(within(details).getByText(/codex mcp add/).textContent).toContain(endpoint);
    expect(details.textContent).not.toContain(key.key);
    expect(details.textContent).toContain('BAMDUDE_MCP_KEY');
  });

  it('explains that the Claude Code manual example bypasses the connector', async () => {
    const user = userEvent.setup();
    render(<McpSettingsCard />);
    await user.selectOptions(screen.getByLabelText('AI client'), 'claude-code');
    expect(screen.getByText(/connected its bundled connector/)).toBeInTheDocument();
    await user.click(screen.getByText('Manual connection'));
    expect(screen.getByText(/bypasses the local connector/)).toBeInTheDocument();
  });

  it('requires two dialogs and the network acknowledgement before HTTP can be saved', async () => {
    let payload: Record<string, unknown> | undefined;
    server.use(http.put('/api/v1/settings/', async ({ request }) => { payload = await request.json() as Record<string, unknown>; return HttpResponse.json(payload); }));
    const user = userEvent.setup();
    render(<McpSettingsCard />);
    await screen.findByDisplayValue(endpoint);
    const toggle = screen.getByRole('switch', { name: 'Allow unencrypted HTTP addresses for MCP' });
    await user.click(toggle);
    expect(toggle).not.toBeChecked();
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(toggle).not.toBeChecked();
    expect(payload).toBeUndefined();
    await user.click(toggle);
    await user.click(screen.getByRole('button', { name: 'I understand — continue' }));
    expect(screen.getByRole('button', { name: 'Allow HTTP at my own risk' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(toggle).not.toBeChecked();
    await user.click(toggle);
    await user.click(screen.getByRole('button', { name: 'I understand — continue' }));
    await user.click(screen.getByLabelText(/I control this private network/));
    await user.click(screen.getByRole('button', { name: 'Allow HTTP at my own risk' }));
    expect(toggle).toBeChecked();
    expect(payload).toBeUndefined();
    const input = screen.getByDisplayValue(endpoint);
    await user.clear(input);
    await user.type(input, 'http://farm.lan:8000/mcp');
    await user.click(screen.getByRole('button', { name: 'Save MCP settings' }));
    await waitFor(() => expect(payload?.mcp_allow_http).toBe(true));
    expect(payload?.mcp_endpoint_url).toBe('http://farm.lan:8000/mcp');
    expect(payload?.mcp_allowed_hosts).toContain('farm.lan:8000');
    expect(payload?.mcp_http_risk_acknowledged).toBe(true);
    expect(payload?.mcp_http_trusted_network_acknowledged).toBe(true);
    await user.click(toggle);
    expect(screen.getByLabelText('Address used by the assistant')).toHaveValue('');
    await user.click(screen.getByRole('button', { name: 'Save MCP settings' }));
    await waitFor(() => expect(payload?.mcp_allow_http).toBe(false));
    expect(payload?.mcp_http_risk_acknowledged).toBeUndefined();
    expect(payload?.mcp_endpoint_url).toBe('');
  });

  it('does not save a non-loopback HTTP address without opting in', async () => {
    const save = vi.fn(() => HttpResponse.json({}));
    server.use(http.put('/api/v1/settings/', save));
    const user = userEvent.setup();
    render(<McpSettingsCard />);
    const input = await screen.findByDisplayValue(endpoint);
    await user.clear(input);
    await user.type(input, 'http://farm.lan:8000/mcp');
    await user.click(screen.getByRole('button', { name: 'Save MCP settings' }));
    await screen.findByText('Could not complete this step. Check the address, permissions and server status.');
    expect(save).not.toHaveBeenCalled();
  });
});
