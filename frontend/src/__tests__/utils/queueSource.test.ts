/**
 * The sentences a failed add is allowed to say.
 *
 * ⚠️ `queueSourceReasonText` builds its key by INTERPOLATION
 * (`queueSpool.reason.${code}`), which is invisible to the static key-resolution
 * test — so a missing or misspelt key ships as the raw key in a toast, on the
 * one screen where the operator is trying to find out whether their job exists.
 * Every code in the closed list is therefore resolved here through the real i18n
 * instance, in both languages, and the fallbacks are pinned beside them: an
 * unknown code keeps the server's own already-localized prose, and a refusal we
 * cannot read at all still says something.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import i18n from '../../i18n';
import { ApiError } from '../../api/client';
import {
  QUEUE_SOURCE_REASONS,
  isUnknownOutcome,
  queueAddOutcomeText,
  queueSourceReasonOf,
  queueSourceReasonText,
} from '../../utils/queueSource';

const t = (key: string, opts?: Record<string, unknown>) => i18n.t(key, opts) as string;

describe('every refusal the server can answer an add with has a sentence', () => {
  afterEach(async () => {
    await i18n.changeLanguage('en');
  });

  for (const language of ['en', 'uk'] as const) {
    it(`resolves all ${QUEUE_SOURCE_REASONS.length} codes in ${language}`, async () => {
      await i18n.changeLanguage(language);
      const sentences = QUEUE_SOURCE_REASONS.map((code) =>
        queueSourceReasonText(t, new ApiError('server prose', 503, code)),
      );

      for (const [index, sentence] of sentences.entries()) {
        const code = QUEUE_SOURCE_REASONS[index];
        // Not the raw key, not the server's prose — our own sentence.
        expect(sentence, code).not.toContain('queueSpool.reason');
        expect(sentence, code).not.toBe('server prose');
        expect(sentence.length, code).toBeGreaterThan(20);
      }
      // Nine codes, nine distinct sentences: a copy-paste that gave two causes
      // the same wording would send the operator to fix the wrong thing.
      expect(new Set(sentences).size).toBe(QUEUE_SOURCE_REASONS.length);
    });
  }

  it('keeps the server’s own sentence for a code it does not know', () => {
    // A future refusal carries the only information there is in its message, and
    // the backend has already translated it.
    expect(queueSourceReasonText(t, new ApiError('Some newer refusal', 503, 'source_something_new'))).toBe(
      'Some newer refusal',
    );
    expect(queueSourceReasonOf(new ApiError('x', 503, 'source_something_new'))).toBeNull();
  });

  it('still says something when the failure carries nothing at all', () => {
    expect(queueSourceReasonText(t, {})).toBe(t('queueSpool.reason.source_copy_failed'));
  });

  it('reads the outcome from the error type, never from its text', () => {
    // A refusal the server answered vs a connection that died: the same words
    // could appear in either, so only the type decides.
    expect(isUnknownOutcome(new ApiError('Failed to fetch', 503, 'source_copy_busy'))).toBe(false);
    expect(isUnknownOutcome(new TypeError('Failed to fetch'))).toBe(true);
  });
});

describe('the outcome sentence for a whole submit', () => {
  beforeEach(async () => {
    await i18n.changeLanguage('en');
  });

  it('leads with "nothing was added" and gives the bare reason for one refusal', () => {
    const text = queueAddOutcomeText(t, {
      added: 0,
      total: 1,
      failures: [{ label: 'A1-01', error: new ApiError('x', 503, 'source_copy_busy') }],
    });
    // No printer label: one target, no possible misattribution.
    expect(text).toBe(
      'Nothing was added to the queue. The queue is already saving other files — try again in a moment.',
    );
  });

  it('names the printers of each reason as soon as there is more than one answer', () => {
    const text = queueAddOutcomeText(t, {
      added: 0,
      total: 2,
      failures: [
        { label: 'A1-01', error: new ApiError('x', 503, 'source_copy_busy') },
        { label: 'A1-02', error: new ApiError('Printer offline', 409) },
      ],
    });
    expect(text).toContain('A1-01: The queue is already saving other files');
    expect(text).toContain('A1-02: Printer offline');
  });

  it('groups printers that failed for the same reason', () => {
    const text = queueAddOutcomeText(t, {
      added: 1,
      total: 3,
      failures: [
        { label: 'A1-02', error: new ApiError('x', 503, 'source_copy_busy') },
        { label: 'A1-03', error: new ApiError('x', 503, 'source_copy_busy') },
      ],
    });
    expect(text).toContain('Added to the queue: 1 of 3. The other 2 were not added.');
    expect(text).toContain('A1-02, A1-03: The queue is already saving other files');
  });

  it('leads with uncertainty but keeps the answered refusal’s reason', () => {
    const text = queueAddOutcomeText(t, {
      added: 0,
      total: 2,
      failures: [
        { label: 'A1-01', error: new ApiError('x', 422, 'source_unreadable') },
        { label: 'A1-02', error: new TypeError('Failed to fetch') },
      ],
    });
    expect(text).toContain('It is not clear whether the job was added');
    expect(text).toContain('A1-01: The original file could not be read.');
  });
});
