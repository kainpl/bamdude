import { useCallback } from 'react';
import { useTranslation } from 'react-i18next';

import { ApiError } from '../api/client';

/** The refusal's machine code, when the server sent one. Never read off the sentence. */
export function manyfoldErrorCode(error: unknown): string | undefined {
  return error instanceof ApiError ? error.code : undefined;
}

/**
 * A refusal in the reader's language. The code picks our sentence; a code this
 * build does not know, or a refusal without one, shows the server's own
 * (translated at the API boundary); a request that never got an answer — the
 * browser's English "Failed to fetch" — gets our sentence instead.
 */
export function useManyfoldRefusal() {
  const { t } = useTranslation();
  return useCallback(
    (error: unknown): string => {
      if (error instanceof ApiError) {
        if (error.code) return t(`manyfold.errors.${error.code}`, { defaultValue: error.message });
        return error.message || t('manyfold.errors.manyfold_failed');
      }
      return t('manyfold.errors.no_answer');
    },
    [t],
  );
}
