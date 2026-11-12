import { createInstance } from 'i18next';
import { describe, expect, it } from 'vitest';
import en from '../../i18n/locales/en';
import uk from '../../i18n/locales/uk';

describe('Git spool restore counts', () => {
  it.each(['en', 'uk'])('renders the exact reactivation count in %s', async (lng) => {
    const i18n = createInstance();
    await i18n.init({ lng, resources: { en: { translation: en }, uk: { translation: uk } } });
    const result = i18n.t('backup.restoreFromGit.notes.spoolInputSummary', {
      invalid_fields: 8, failed_rows: 0, skipped_rows: 1, reactivated: 3, baseline_above_used: 2,
    });
    expect(result).toContain(lng === 'en' ? 'reactivated spools: 3' : 'реактивовано котушок: 3');
    expect(result).not.toContain('{{');
    expect(result).not.toContain('backup.restoreFromGit');
  });
});
