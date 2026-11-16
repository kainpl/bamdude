import { createInstance } from 'i18next';
import { describe, expect, it } from 'vitest';
import en from '../../i18n/locales/en';
import uk from '../../i18n/locales/uk';

describe('Git spool restore counts', () => {
  it.each(['en', 'uk'])('renders archive and usage diagnostics in %s', async (lng) => {
    const i18n = createInstance();
    await i18n.init({ lng, resources: { en: { translation: en }, uk: { translation: uk } } });
    const text = i18n.t('backup.restoreFromGit.notes.archivesInputSummary', {
      invalid_fields: 2, failed_rows: 3, skipped_rows: 4, identity_skipped: 5,
      legacy_source_artifacts_skipped: 6, energy_kept: 7, energy_unknown: 8,
    });
    for (const n of [2, 3, 4, 5, 6, 7, 8]) expect(text).toContain(String(n));
    expect(text).not.toContain('{{');
    for (const key of ['spoolUsageUnresolved', 'spoolUsageUnlinked', 'spoolUsageTimeUnknown', 'spoolUsagePrinterUnproven']) {
      const note = i18n.t(`backup.restoreFromGit.notes.${key}`, { count: 9 });
      expect(note).toContain('9');
      expect(note).not.toContain('backup.restoreFromGit');
    }
    for (const key of ['archivesInvalidPayload', 'spoolsInvalidPayload', 'spoolUsageInvalidPayload', 'spoolUsageAliasConflict']) {
      expect(i18n.t(`backup.restoreFromGit.details.${key}`)).not.toContain('backup.restoreFromGit');
    }
    expect(i18n.t('backup.restoreFromGit.idleFarmWarning')).not.toContain('backup.restoreFromGit');
  });
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
