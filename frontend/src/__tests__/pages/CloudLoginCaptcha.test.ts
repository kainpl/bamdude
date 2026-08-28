/**
 * A CAPTCHA challenge gets a panel, not a toast.
 *
 * Ported from upstream #2790. A toast names a problem the user cannot act on
 * and then vanishes. This one stays put and carries a one-click route to "Use
 * access token instead", which is the only thing that works while the challenge
 * lasts — that path does not touch the challenged endpoint.
 */

import { readFileSync } from 'node:fs';
import { describe, it, expect } from 'vitest';

const PAGE = readFileSync('src/pages/ProfilesPage.tsx', 'utf8');
const EN = readFileSync('src/i18n/locales/en.ts', 'utf8');
const UK = readFileSync('src/i18n/locales/uk.ts', 'utf8');

describe('the captcha panel', () => {
  it('is driven by the server reason, not by the message text', () => {
    // ⚠️ The message alone cannot be told apart from a wrong password — which
    // is how Bambu's own sentence ended up flashed as an unactionable toast.
    expect(PAGE).toContain("setCaptchaBlocked(result.reason === 'captcha')");
  });

  it('both sign-in steps set it', () => {
    const sets = PAGE.split("setCaptchaBlocked(result.reason === 'captcha')").length - 1;
    expect(sets).toBe(2);
  });

  it('suppresses the toast that would otherwise fire', () => {
    const suppressions = PAGE.split("result.reason !== 'captcha'").length - 1;
    expect(suppressions).toBe(2);
  });

  it('renders as a persistent alert', () => {
    expect(PAGE).toContain('{captchaBlocked && ');
    expect(PAGE).toContain('role="alert"');
  });

  it('offers the access-token route, which is the only thing that works', () => {
    const panel = PAGE.slice(PAGE.indexOf('{captchaBlocked && '), PAGE.indexOf('<form onSubmit={handleSubmit}'));
    expect(panel).toContain("setStep('token')");
    expect(panel).toContain("t('profiles.login.useToken')");
  });

  it('hides itself once the user is on the token step', () => {
    // The panel is about the sign-in path being blocked; on the token step it
    // would be telling the user a route is closed while they walk the open one.
    expect(PAGE).toContain("{captchaBlocked && step !== 'token' && (");
  });

  it('a network error clears it rather than leaving a stale panel', () => {
    const clears = PAGE.split('setCaptchaBlocked(false)').length - 1;
    expect(clears).toBeGreaterThanOrEqual(3); // two onError handlers + the token button
  });
});

describe('copy', () => {
  for (const [name, locale] of [
    ['en', EN],
    ['uk', UK],
  ] as const) {
    it(`is translated in ${name}`, () => {
      expect(locale).toContain('captchaTitle:');
      expect(locale).toContain('captchaBody:');
      expect(locale).toContain("'bambu-cloud-captcha': {");
    });
  }

  it('says the credentials are not the problem', () => {
    // The three things the reporter had no way to find out: it is not the
    // password, it is keyed to the IP, and it clears by itself.
    const body = EN.slice(EN.indexOf('captchaBody:'), EN.indexOf('captchaBody:') + 600);
    expect(body).toMatch(/password/i);
    expect(body).toMatch(/IP address/i);
    expect(body).toMatch(/few hours/i);
  });
});
