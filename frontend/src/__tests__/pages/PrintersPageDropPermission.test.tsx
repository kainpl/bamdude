/**
 * The card's drop zone asks for the permissions the drop actually uses
 * (upstream 47a37618, #2849): it uploads to the library and adds a queue item,
 * so `library:upload` and `queue:create` — never `printers:control`, which it
 * checked before and never exercises. Someone holding control alone had the
 * file uploaded and then refused by the queue, a library row left behind. The
 * refusal names the permission that is missing, not a busy printer.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const permissions = { granted: [] as string[] };

const mockUseAuth = {
  user: { id: 1, username: 'operator', permissions: [] as string[] },
  authEnabled: true,
  requiresSetup: false,
  loading: false,
  isAdmin: false,
  login: vi.fn(),
  loginWithToken: vi.fn(),
  logout: vi.fn(),
  refreshUser: vi.fn(),
  refreshAuth: vi.fn(),
  hasPermission: vi.fn((permission: string) => permissions.granted.includes(permission)),
  hasAnyPermission: vi.fn(() => true),
  hasAllPermissions: vi.fn(() => true),
  canModify: vi.fn(() => true),
};

vi.mock('../../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../contexts/AuthContext')>();
  return { ...actual, useAuth: () => mockUseAuth };
});

import { render } from '../utils';
import { PrintersPage } from '../../pages/PrintersPage';

const mockPrinter = {
  id: 1,
  name: 'Workhorse',
  ip_address: '192.168.1.100',
  serial_number: '01P00A000000001',
  access_code: '12345678',
  model: 'X1C',
  enabled: true,
  nozzle_diameter: 0.4,
  nozzle_type: 'stainless_steel',
  location: 'Workshop',
  auto_archive: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

const uploads: string[] = [];

function renderPage() {
  const status = {
    connected: true, state: 'IDLE', progress: 0, layer_num: 0, total_layers: 0,
    temperatures: { nozzle: 25, bed: 25, chamber: 25 }, remaining_time: 0, filename: null,
    wifi_signal: -29, speed_level: 2, vt_tray: [], ams: [],
  };
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([mockPrinter])),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(status)),
    http.get('/api/v1/printers/status/batch', ({ request }) =>
      HttpResponse.json(Object.fromEntries(new URL(request.url).searchParams.getAll('ids').map((id) => [id, status])))),
    http.get('/api/v1/queue/', () => HttpResponse.json([])),
    http.post('/api/v1/library/files', () => {
      uploads.push('upload');
      return HttpResponse.json({ id: 7, filename: 'part.gcode.3mf', metadata: {}, file_tags: [], outcome: 'created' });
    }),
    http.delete('/api/v1/library/files/:id', () => HttpResponse.json({ success: true })),
  );
  render(<PrintersPage />);
}

const file = () => new File(['PK'], 'part.gcode.3mf', { type: 'application/octet-stream' });

describe('PrintersPage — the drop zone asks for upload + queue permissions', () => {
  beforeEach(() => {
    uploads.length = 0;
  });

  it('accepts the drop with upload and queue permissions, without printer control', async () => {
    permissions.granted = ['library:upload', 'queue:create'];
    renderPage();
    const el = await screen.findByText('Workhorse');
    fireEvent.dragEnter(el, { dataTransfer: { files: [file()] } });
    expect(await screen.findByText('Drop to print')).toBeInTheDocument();
    fireEvent.drop(el, { dataTransfer: { files: [file()] } });
    await waitFor(() => expect(uploads).toHaveLength(1));
  });

  it('refuses printer control alone, naming the upload permission', async () => {
    permissions.granted = ['printers:control'];
    renderPage();
    const el = await screen.findByText('Workhorse');
    fireEvent.dragEnter(el, { dataTransfer: { files: [file()] } });
    expect(await screen.findByText('You do not have permission to upload files')).toBeInTheDocument();
    fireEvent.drop(el, { dataTransfer: { files: [file()] } });
    expect(uploads).toHaveLength(0);
  });

  it('names the queue permission when only that is missing', async () => {
    permissions.granted = ['library:upload'];
    renderPage();
    fireEvent.dragEnter(await screen.findByText('Workhorse'), { dataTransfer: { files: [file()] } });
    expect(await screen.findByText('You do not have permission to add to queue')).toBeInTheDocument();
  });
});
