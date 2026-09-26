/**
 * The Archives banner for prints left without their 3MF speaks to WHY
 * (audit D6, upstream 6564c740).
 *
 * "Switch on Store sent files on external storage" is the answer to only one
 * cause — the file was looked for and not there. A refused file connection, a
 * rejected access code or an unreachable printer is not a slicer setting, and
 * telling the operator to change one is how upstream's reporter read the whole
 * thing as the app being broken. Each reason is dismissed on its own, so closing
 * one never hides the next behind it.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import i18n from '../../i18n';
import { No3MFBanner } from '../../components/No3MFBanner';
import type { No3MFWarning } from '../../api/client';

function renderBanner(warning: No3MFWarning | undefined) {
  return render(
    <I18nextProvider i18n={i18n}>
      <No3MFBanner warning={warning} />
    </I18nextProvider>,
  );
}

function warning(...reasons: No3MFWarning['reasons']): No3MFWarning {
  return { has_fallback: reasons.length > 0, reason: reasons[0] ?? null, reasons };
}

const docsLink = () => screen.getByRole('link');

describe('No3MFBanner', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('shows nothing without a fallback', () => {
    const { container } = renderBanner(warning());
    expect(container).toBeEmptyDOMElement();
  });

  it('names a refused file connection and gives no slicer advice', () => {
    renderBanner(warning('ftps_refused', 'not_found'));
    expect(screen.getByText(/refused the file connection/i)).toBeInTheDocument();
    expect(screen.queryByText(/Store sent files/i)).not.toBeInTheDocument();
    expect(docsLink()).toHaveAttribute('href', 'https://docs.bamdude.top/reference/troubleshooting/#ftps-cleartext-answer');
  });

  it('names a rejected access code', () => {
    renderBanner(warning('auth_rejected'));
    expect(screen.getByText(/rejected the access code/i)).toBeInTheDocument();
    expect(screen.queryByText(/Store sent files/i)).not.toBeInTheDocument();
    expect(docsLink()).toHaveAttribute('href', 'https://docs.bamdude.top/reference/troubleshooting/#wrong-access-code');
  });

  it('names an unreachable file service', () => {
    renderBanner(warning('unreachable'));
    expect(screen.getByText(/file service didn't answer/i)).toBeInTheDocument();
    expect(screen.queryByText(/Store sent files/i)).not.toBeInTheDocument();
    expect(docsLink()).toHaveAttribute('href', 'https://docs.bamdude.top/reference/troubleshooting/#ftps-port-990-blocked');
  });

  it('keeps the install-step-4 advice for a file that was not there', () => {
    renderBanner(warning('not_found'));
    expect(screen.getByText(/Store sent files on external storage/i)).toBeInTheDocument();
    expect(docsLink()).toHaveAttribute('href', 'https://docs.bamdude.top/getting-started/');
  });

  it('gives a row with no recorded reason the same advice as before', () => {
    renderBanner(warning(null));
    expect(screen.getByText(/Store sent files on external storage/i)).toBeInTheDocument();
  });

  it('dismissing one reason shows the next, not nothing', () => {
    renderBanner(warning('ftps_refused', 'not_found'));
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }));
    expect(screen.queryByText(/refused the file connection/i)).not.toBeInTheDocument();
    expect(screen.getByText(/Store sent files on external storage/i)).toBeInTheDocument();
  });

  it('remembers a dismissal per reason', () => {
    const first = renderBanner(warning('unreachable'));
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }));
    first.unmount();

    const again = renderBanner(warning('unreachable'));
    expect(again.container).toBeEmptyDOMElement();
    again.unmount();

    renderBanner(warning('auth_rejected'));
    expect(screen.getByText(/rejected the access code/i)).toBeInTheDocument();
  });

  it('honours a dismissal of the old banner for the old advice', () => {
    // The key the banner used before it knew reasons: whoever closed it then
    // closed exactly the "Store sent files" advice, and keeps it closed.
    localStorage.setItem('archiveNo3MFWarningDismissed', 'true');
    const { container } = renderBanner(warning('not_found', null));
    expect(container).toBeEmptyDOMElement();
  });

  it('links the Ukrainian docs in Ukrainian', async () => {
    await i18n.changeLanguage('uk');
    try {
      renderBanner(warning('ftps_refused'));
      expect(docsLink()).toHaveAttribute(
        'href',
        'https://docs.bamdude.top/uk/reference/troubleshooting/#ftps-cleartext-answer',
      );
    } finally {
      await i18n.changeLanguage('en');
    }
  });
});
