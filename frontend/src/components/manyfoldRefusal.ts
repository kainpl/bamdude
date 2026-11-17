import { useCallback } from 'react';
import { useTranslation } from 'react-i18next';

import { api, ApiError } from '../api/client';

/** The refusal's machine code, when the server sent one. Never read off the sentence. */
export function manyfoldErrorCode(error: unknown): string | undefined {
  return error instanceof ApiError ? error.code : undefined;
}

/** The checked transport of the Manyfold connection is gone; only a restart of BamDude brings it back. */
export const MANYFOLD_TRANSPORT_UNAVAILABLE = 'manyfold_transport_unavailable';

/**
 * Why a Manyfold that is set up cannot be used. The status says only that it cannot; one read
 * of the catalogue is refused with the reason's code. The answer is the code (null when the
 * read went through after all) — data, not an error, so a re-read never blanks it for a moment.
 */
export const manyfoldUnusableQuery = {
  queryKey: ['manyfold-unusable'] as const,
  queryFn: async (): Promise<string | null> => {
    try {
      await api.listManyfoldModels('', 1);
      return null;
    } catch (error) {
      return manyfoldErrorCode(error) ?? 'unknown';
    }
  },
  retry: false,
};

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
