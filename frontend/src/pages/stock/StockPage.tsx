import { useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Loader2, Search, Warehouse } from 'lucide-react';
import type { StockProduct, StockSummaryParams } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { ProjectsTabs } from '../../components/projects/ProjectsTabs';
import { AdjustStockDialog } from '../../components/products/AdjustStockDialog';
import { StockJournal } from '../../components/stock/StockJournal';
import { StockProductsTable } from '../../components/stock/StockProductsTable';
import { useStockSummary } from '../../hooks/useStock';

/** How long the search box waits before it becomes a request — the catalog's number. */
const DEBOUNCE_MS = 300;

/**
 * The fourth root of the Projects section: the farm-wide shelf.
 *
 * Both filters are asked of the server, like the catalog. Data before status:
 * with a summary on screen a failed refetch leaves it there and the hook's
 * `refreshToast` reports it once; with nothing yet, a spinner or the sentence.
 */
export function StockPage() {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const queryClient = useQueryClient();
  const canEdit = hasPermission('projects:update');

  const [typed, setTyped] = useState('');
  const [q, setQ] = useState('');
  const [onlyWithStock, setOnlyWithStock] = useState(true);
  const [adjusting, setAdjusting] = useState<StockProduct | null>(null);

  useEffect(() => {
    const timer = setTimeout(() => setQ(typed.trim()), DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [typed]);

  // `with_stock` is sent only when it departs from the server's default, and
  // `q` only when there is one — an absent key, not `undefined`, so the query
  // key and the request agree with each other.
  const params: StockSummaryParams = { ...(onlyWithStock ? {} : { with_stock: false }), ...(q ? { q } : {}) };
  const { data, isError } = useStockSummary(params);
  const products = data?.products ?? [];

  return (
    <div className="p-4">
      <ProjectsTabs />

      <div className="flex items-center justify-between mb-2 flex-wrap gap-3">
        <h1 className="text-2xl font-semibold text-white flex items-center gap-2">
          <Warehouse className="w-6 h-6 text-bambu-green" />
          {t('stock.page.title')}
        </h1>
      </div>
      <p className="text-sm text-bambu-gray mb-4">{t('stock.page.intro')}</p>

      <div className="flex items-center gap-3 flex-wrap mb-4">
        <div className="relative">
          <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-bambu-gray" />
          <input
            type="search"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            placeholder={t('stock.page.search')}
            className="pl-9 pr-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:outline-none"
          />
        </div>
        <label className="flex items-center gap-2 text-sm text-bambu-gray">
          <input type="checkbox" checked={onlyWithStock} onChange={(e) => setOnlyWithStock(e.target.checked)} />
          {t('stock.page.onlyWithStock')}
        </label>
      </div>

      {!data ? (
        isError ? (
          <p className="text-sm text-red-500" data-testid="stock-error">{t('stock.page.error')}</p>
        ) : (
          <p className="flex items-center gap-2 text-sm text-bambu-gray"><Loader2 className="w-4 h-4 animate-spin" />{t('common.loading')}</p>
        )
      ) : products.length === 0 ? (
        <p className="text-sm text-bambu-gray" data-testid="stock-empty">
          {q || !onlyWithStock ? t('stock.page.emptyFiltered') : t('stock.page.empty')}
        </p>
      ) : (
        <StockProductsTable products={products} canEdit={canEdit} onAdjust={setAdjusting} />
      )}

      <div className="mt-8">
        <StockJournal />
      </div>

      {adjusting && (
        <AdjustStockDialog
          productId={adjusting.id}
          parts={adjusting.parts.map((b) => ({ part_id: b.part_id, name: b.name }))}
          onClose={() => setAdjusting(null)}
          onSaved={() => {
            queryClient.invalidateQueries({ queryKey: ['stock-summary'] });
            queryClient.invalidateQueries({ queryKey: ['stock-movements'] });
          }}
        />
      )}
    </div>
  );
}
