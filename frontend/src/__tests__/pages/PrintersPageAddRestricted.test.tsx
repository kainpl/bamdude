/**
 * N62 §12.4: a person limited to printers cannot add one — the server refuses it, and the page
 * says why instead of offering a button that fails.
 */

import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { PrintersPage } from '../../pages/PrintersPage';

const me = (printers_restricted: boolean) => ({
  id: 9,
  username: 'alice',
  role: 'user',
  is_active: true,
  is_admin: false,
  groups: [],
  permissions: ['printers:read', 'printers:create'],
  printers_restricted,
  created_at: '2024-01-01T00:00:00Z',
});

describe('PrintersPage — adding a printer when limited to printers', () => {
  it('disables Add Printer and says to ask an administrator', async () => {
    server.use(
      http.get('/api/v1/auth/me', () => HttpResponse.json(me(true))),
      http.get('/api/v1/printers/', () => HttpResponse.json([])),
    );
    const view = render(<PrintersPage />);
    try {
      await waitFor(() => {
        const buttons = screen.getAllByRole('button', { name: /Add Printer/ });
        expect(buttons.length).toBeGreaterThan(0);
        for (const button of buttons) {
          expect(button).toBeDisabled();
          expect(button).toHaveAttribute('title', expect.stringContaining('ask an administrator'));
        }
      });
    } finally {
      view.unmount();
    }
  });

  it('offers it to the same person when no group limits them', async () => {
    server.use(
      http.get('/api/v1/auth/me', () => HttpResponse.json(me(false))),
      http.get('/api/v1/printers/', () => HttpResponse.json([])),
    );
    const view = render(<PrintersPage />);
    try {
      await waitFor(() => {
        const buttons = screen.getAllByRole('button', { name: /Add Printer/ });
        expect(buttons.some((button) => !button.hasAttribute('disabled'))).toBe(true);
      });
    } finally {
      view.unmount();
    }
  });
});
