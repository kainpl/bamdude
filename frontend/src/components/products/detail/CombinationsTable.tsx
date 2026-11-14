import { useEffect, useState } from 'react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../../../api/client';
import type { Product } from '../../../api/client';
import { listState } from '../../../utils/listState';
import { PRODUCT_COMBINATIONS_KEY } from '../../../utils/queryInvalidation';
import { PaginationBar } from '../../PaginationBar';
import { LoadFailedNote } from '../../workshop/LoadFailedNote';
import { RefreshFailedNote } from '../../workshop/RefreshFailedNote';
import { reasonText } from '../partBinding';

/** Rows a page of the inline table holds — well under the server's cap of 500. */
export const COMBINATIONS_PER_PAGE = 50;

const HEAD = 'px-3 py-2 text-left text-xs font-normal text-bambu-gray whitespace-nowrap';
const CELL = 'px-3 py-2 align-top';
const AMBER = 'text-amber-700 dark:text-amber-400';

/**
 * Every combination of the product's variant options, and whether an order can take it (spec
 * product-part-variant-combinations п. 24): a column per group, then «State» — available, or one
 * sentence per reason (a value no part of the kit names, a printed part with no sliced source).
 *
 * ⚠️ The server decides, counts and pages (`GET /products/{id}/combinations`); this table only
 * composes the sentences (`partBinding.reasonText`). The columns are read off the answer itself, so
 * a row and its header always come from the same snapshot.
 *
 * A product without groups has no combination to list: nothing is rendered and nothing asked.
 * The key `[PRODUCT_COMBINATIONS_KEY, productId, page]` is refreshed by the part, variant and
 * file-link writes (`invalidateProductFiles`, `invalidateProductVariants`); a file sliced elsewhere
 * shows on the next read.
 */
export function CombinationsTable({ product }: { product: Pick<Product, 'id' | 'variant_groups'> }) {
  const { t } = useTranslation();
  const hasGroups = (product.variant_groups ?? []).length > 0;
  const [page, setPage] = useState(1);
  const query = useQuery({
    queryKey: [PRODUCT_COMBINATIONS_KEY, product.id, page],
    queryFn: () => api.getProductCombinations(product.id, page, COMBINATIONS_PER_PAGE),
    enabled: hasGroups,
    placeholderData: keepPreviousData,
    retry: false,
  });
  const data = query.data;
  const state = listState({
    data: data ? { meta: { total: data.total } } : undefined,
    isError: query.isError,
    isPlaceholderData: query.isPlaceholderData,
  });
  const lastPage = data ? Math.max(1, Math.ceil(data.total / COMBINATIONS_PER_PAGE)) : 1;

  // The table shrank under the page (a group or an option went): back to its last page, decided
  // only by an answer of the page's own key.
  useEffect(() => {
    if (data && !query.isPlaceholderData && page > lastPage) setPage(lastPage);
  }, [data, query.isPlaceholderData, page, lastPage]);

  if (!hasGroups || state === 'empty') return null;

  let body;
  if (state === 'loading') {
    body = (
      <p role="status" className="text-sm text-bambu-gray">
        {t('common.loading')}
      </p>
    );
  } else if (state === 'failed') {
    body = <LoadFailedNote role="status" message={t('products.combinations.failed')} onRetry={() => query.refetch()} />;
  } else {
    const items = data?.items ?? [];
    const head = items[0]?.choices ?? [];
    body = (
      <div className={state === 'transition' ? 'opacity-60' : undefined} aria-busy={state === 'transition' || undefined}>
        {state === 'refresh-failed' && <RefreshFailedNote onRetry={() => query.refetch()} />}
        {items.length > 0 && (
          <div className="overflow-x-auto rounded-lg border border-bambu-dark-tertiary">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-bambu-dark-tertiary">
                  {head.map((choice) => (
                    <th key={choice.group_id} className={HEAD}>
                      {choice.group}
                    </th>
                  ))}
                  <th className={HEAD}>{t('products.combinations.state')}</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr
                    key={item.choices.map((c) => c.option_id).join('-')}
                    data-testid="combination-row"
                    data-available={item.available ? 'true' : 'false'}
                    className="border-b border-bambu-dark-tertiary last:border-0"
                  >
                    {item.choices.map((choice) => (
                      <td key={choice.group_id} className={`${CELL} text-white wrap-anywhere`}>
                        {choice.option}
                      </td>
                    ))}
                    <td className={CELL}>
                      {item.available ? (
                        <span className="text-xs text-bambu-green">{t('products.combinations.available')}</span>
                      ) : (
                        item.reasons.map((reason, index) => (
                          <span key={index} className={`block text-xs ${AMBER}`}>
                            {reasonText(t, reason)}
                          </span>
                        ))
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {data && (
          <PaginationBar
            page={data.page}
            totalPages={lastPage}
            perPage={COMBINATIONS_PER_PAGE}
            total={data.total}
            onPageChange={setPage}
            onPerPageChange={() => {}}
            perPageOptions={[COMBINATIONS_PER_PAGE]}
            allowAll={false}
            items={t('products.combinations.count', { count: data.total })}
            variant="bare"
          />
        )}
      </div>
    );
  }

  return (
    <section data-testid="product-combinations" className="space-y-2 pt-2">
      <p className="text-sm">
        <b className="font-semibold text-white">{t('products.combinations.title')}</b>{' '}
        <small className="text-xs text-bambu-gray">{t('products.combinations.hint')}</small>
      </p>
      {body}
    </section>
  );
}
