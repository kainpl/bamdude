import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../api/client';
import type { DefectsWriteBody, WaitingPrint } from '../api/client';
import { useToast } from '../contexts/ToastContext';
import { invalidateQueueViews, invalidateOrderViews } from '../utils/queryInvalidation';

/**
 * The defects counters that ride with a plate answer — one hook for every
 * place that draws «Clear plate» / «Repeat print»: the queue widget's green
 * pair, the printer card's yellow pair (the one the operator sees once the
 * queue has run dry, and the only one on the compact card) and the Queue
 * page's card. The print the gate is about is fetched only while the pair is
 * on screen; the numbers are a starting point pre-filled from it and travel
 * with whichever button is pressed, and only when a counter was touched — an
 * untouched row sends no body, so the backend records nothing. The row itself
 * is `components/PlateDefectsRow`.
 */
export interface PlateDefects {
  waiting: WaitingPrint | undefined;
  /** The print on the plate can be graded: it completed and made something. */
  gradable: boolean;
  open: boolean;
  toggle: () => void;
  values: Record<number, number>;
  flat: number;
  touched: boolean;
  /** The total the toggle advertises: the operator's numbers once touched, else the server's. */
  shown: number;
  setPart: (id: number, next: number) => void;
  setFlat: (next: number) => void;
  /** The request body for either answer, or nothing when the counters were not touched. */
  body: () => { defects: DefectsWriteBody } | undefined;
  /** After a successful answer: refresh everything the defects may have moved, close the row. */
  afterAnswer: (ledgerRefused?: number) => void;
  /** After a refused answer: the server rolled the defects back, so stop showing what was typed. */
  afterFailedAnswer: () => void;
}

export function usePlateDefects(printerId: number, enabled: boolean): PlateDefects {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const [open, setOpen] = useState(false);
  const [values, setValues] = useState<Record<number, number>>({});
  const [flat, setFlatState] = useState(0);
  const [touched, setTouched] = useState(false);

  const { data: waiting } = useQuery({
    queryKey: ['waiting-print', printerId],
    queryFn: () => api.getWaitingPrint(printerId),
    enabled,
    retry: false,
  });
  useEffect(() => {
    if (waiting && !touched) {
      setValues(Object.fromEntries(waiting.parts.map((p) => [p.id, p.defective])));
      setFlatState(waiting.defective_count);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [waiting]);

  const gradable = !!waiting && waiting.status === 'completed' && waiting.quantity > 0;
  // ⚠️ Branch on whether the print HAS part rows, exactly as `body()` does —
  // never `sum || flat`: zeroing every counter makes the sum 0, and the `||`
  // then fell through to the server-seeded flat count, so the toggle kept
  // advertising the old total while every field read 0.
  const shown = !waiting
    ? 0
    : !touched
      ? waiting.defective_count
      : waiting.parts.length > 0
        ? Object.values(values).reduce((a, n) => a + n, 0)
        : flat;

  const body = (): { defects: DefectsWriteBody } | undefined => {
    if (!touched || !waiting) return undefined;
    return waiting.parts.length > 0
      ? { defects: { parts: waiting.parts.map((p) => ({ id: p.id, defective: values[p.id] ?? 0 })) } }
      : { defects: { defective_count: flat } };
  };

  const afterAnswer = (ledgerRefused?: number) => {
    invalidateQueueViews(queryClient);
    queryClient.invalidateQueries({ queryKey: ['printerStatus', printerId] });
    queryClient.invalidateQueries({ queryKey: ['waiting-print', printerId] });
    // The shelf may have moved with the defects.
    invalidateOrderViews(queryClient);
    // And so did the archive's defective count: the Archives page's column and
    // the statistics page's «Defects by printer» both read it.
    queryClient.invalidateQueries({ queryKey: ['archives'] });
    queryClient.invalidateQueries({ queryKey: ['archiveStats'] });
    queryClient.invalidateQueries({ queryKey: ['archiveAggregate'] });
    setTouched(false);
    setOpen(false);
    // Reported here because here it can happen — the print on the plate is
    // usually filed under no order, so its defects correct a free-stock credit,
    // and parts already spent cannot come back off the shelf. After the success
    // toast, so the operator reads "saved" first and then "fix the shelf".
    if (ledgerRefused && ledgerRefused > 0) {
      showToast(t('queue.defects.ledgerRefused', { count: ledgerRefused }), 'error');
    }
  };
  const afterFailedAnswer = () => {
    queryClient.invalidateQueries({ queryKey: ['waiting-print', printerId] });
  };

  return {
    waiting,
    gradable,
    open,
    toggle: () => setOpen((o) => !o),
    values,
    flat,
    touched,
    shown,
    setPart: (id, next) => {
      setValues((prev) => ({ ...prev, [id]: next }));
      setTouched(true);
    },
    setFlat: (next) => {
      setFlatState(next);
      setTouched(true);
    },
    body,
    afterAnswer,
    afterFailedAnswer,
  };
}
