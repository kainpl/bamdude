/**
 * Tests for SecurityStatusCard — verifies the five severity levels
 * (green / yellow / orange / red / grey) are rendered for the right
 * combinations of key_source, legacy_plaintext_rows, and decryption_broken.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { SecurityStatusCard } from '../../components/SecurityStatusCard';
import { server } from '../mocks/server';
import type { EncryptionStatus } from '../../api/client';

const STATUS_URL = '/api/v1/auth/encryption-status';

function makeStatus(overrides: Partial<EncryptionStatus> = {}): EncryptionStatus {
  return {
    key_configured: true,
    key_source: 'env',
    legacy_plaintext_rows: { oidc_providers: 0, user_totp: 0 },
    encrypted_rows: { oidc_providers: 0, user_totp: 0 },
    decryption_broken: false,
    migration_error_count: 0,
    ...overrides,
  };
}

describe('SecurityStatusCard', () => {
  beforeEach(() => {
    server.use(http.get(STATUS_URL, () => HttpResponse.json(makeStatus())));
  });

  it('shows loading indicator while query is pending', () => {
    server.use(
      http.get(STATUS_URL, async () => {
        await new Promise(() => {
          /* never resolves — keeps loading state */
        });
        return HttpResponse.json(makeStatus());
      }),
    );
    render(<SecurityStatusCard />);
    expect(screen.getByTestId('encryption-loading')).toBeInTheDocument();
  });

  it('shows error state when API returns 500', async () => {
    server.use(http.get(STATUS_URL, () => new HttpResponse(null, { status: 500 })));
    render(<SecurityStatusCard />);
    await waitFor(() => {
      expect(screen.getByTestId('encryption-error')).toBeInTheDocument();
    });
  });

  it('renders status card after data loads', async () => {
    render(<SecurityStatusCard />);
    await waitFor(() => {
      expect(screen.getByTestId('encryption-status')).toBeInTheDocument();
    });
  });

  it('renders green all-encrypted state when key configured + no legacy rows', async () => {
    server.use(
      http.get(STATUS_URL, () =>
        HttpResponse.json(makeStatus({ key_source: 'env', encrypted_rows: { oidc_providers: 2, user_totp: 5 } })),
      ),
    );
    render(<SecurityStatusCard />);
    await waitFor(() => expect(screen.getByTestId('encryption-status')).toBeInTheDocument());
    // Green severity classes applied to wrapper.
    expect(screen.getByTestId('encryption-status').className).toContain('green');
  });

  it('renders amber backup-hint state when key_source is generated', async () => {
    server.use(http.get(STATUS_URL, () => HttpResponse.json(makeStatus({ key_source: 'generated' }))));
    render(<SecurityStatusCard />);
    await waitFor(() => expect(screen.getByTestId('encryption-status')).toBeInTheDocument());
    expect(screen.getByTestId('encryption-status').className).toContain('amber');
  });

  it('renders amber legacy-rows warning when plaintext rows still exist', async () => {
    server.use(
      http.get(STATUS_URL, () =>
        HttpResponse.json(
          makeStatus({
            key_source: 'env',
            legacy_plaintext_rows: { oidc_providers: 1, user_totp: 2 },
          }),
        ),
      ),
    );
    render(<SecurityStatusCard />);
    await waitFor(() => expect(screen.getByTestId('encryption-status')).toBeInTheDocument());
    expect(screen.getByTestId('encryption-status').className).toContain('amber');
  });

  it('renders red decryption-broken state when flag is true', async () => {
    server.use(
      http.get(STATUS_URL, () =>
        HttpResponse.json(
          makeStatus({
            decryption_broken: true,
            encrypted_rows: { oidc_providers: 1, user_totp: 1 },
          }),
        ),
      ),
    );
    render(<SecurityStatusCard />);
    await waitFor(() => expect(screen.getByTestId('encryption-status')).toBeInTheDocument());
    expect(screen.getByTestId('encryption-status').className).toContain('red');
  });

  it('renders grey not-configured state when key absent + no encrypted rows', async () => {
    server.use(
      http.get(STATUS_URL, () =>
        HttpResponse.json(makeStatus({ key_configured: false, key_source: 'none' })),
      ),
    );
    render(<SecurityStatusCard />);
    await waitFor(() => expect(screen.getByTestId('encryption-status')).toBeInTheDocument());
    expect(screen.getByTestId('encryption-status').className).toContain('gray');
  });

  it('shows concurrent legacy warning when generated key + plaintext rows coexist', async () => {
    server.use(
      http.get(STATUS_URL, () =>
        HttpResponse.json(
          makeStatus({
            key_source: 'generated',
            legacy_plaintext_rows: { oidc_providers: 1, user_totp: 0 },
          }),
        ),
      ),
    );
    render(<SecurityStatusCard />);
    await waitFor(() => expect(screen.getByTestId('encryption-status')).toBeInTheDocument());
    expect(screen.getByTestId('encryption-legacy-warning')).toBeInTheDocument();
  });

  it('shows migration-error warning when migration_error_count > 0', async () => {
    server.use(http.get(STATUS_URL, () => HttpResponse.json(makeStatus({ migration_error_count: 3 }))));
    render(<SecurityStatusCard />);
    await waitFor(() => expect(screen.getByTestId('encryption-migration-warning')).toBeInTheDocument());
  });
});
