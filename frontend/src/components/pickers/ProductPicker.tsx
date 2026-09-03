import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../../api/client';
import { selectableProducts } from '../../utils/projects';
import { useToast } from '../../contexts/ToastContext';

const INPUT_CLASS =
  'w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none';

interface ProductPickerProps {
  value: number | null;
  onChange: (id: number | null) => void;
  disabled?: boolean;
  allowCreate?: boolean;
}

/** Searchable list over the product catalog, with an inline "create product
 *  from this name" affordance when nothing matches (used for adding an order
 *  line or linking a file to a not-yet-catalogued product). */
export function ProductPicker({ value, onChange, disabled, allowCreate }: ProductPickerProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState('');

  const { data: products } = useQuery({
    queryKey: ['products', {}],
    queryFn: () => api.getProducts({}),
  });

  const bound = selectableProducts(products, value != null ? [value] : null);
  const query = filter.trim().toLowerCase();
  const filtered = query ? bound.filter((p) => p.name.toLowerCase().includes(query)) : bound;
  const offerCreate = Boolean(allowCreate) && filtered.length === 0 && query.length > 0;

  const createMutation = useMutation({
    mutationFn: (name: string) => api.createProduct({ name }),
    onSuccess: (created) => {
      queryClient.invalidateQueries({ queryKey: ['products'] });
      onChange(created.id);
      setFilter('');
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  return (
    <div>
      <input
        type="text"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        disabled={disabled}
        placeholder={t('pickers.searchProducts')}
        className={INPUT_CLASS}
      />
      <div className="max-h-48 overflow-auto mt-2 space-y-1">
        {filtered.map((p) => (
          <div key={p.id} className="flex items-center gap-2">
            <button
              type="button"
              disabled={disabled}
              onClick={() => onChange(p.id)}
              className={`flex-1 text-left px-3 py-1.5 rounded-lg text-sm transition-colors ${
                p.id === value
                  ? 'bg-bambu-green/20 text-bambu-green'
                  : 'bg-bambu-dark text-white hover:bg-bambu-dark-tertiary'
              }`}
            >
              {p.name}
            </button>
            {p.is_active === false && (
              <span className="text-xs text-bambu-gray">{t('pickers.notInCatalog')}</span>
            )}
          </div>
        ))}
        {filtered.length === 0 && !offerCreate && (
          <p className="text-sm text-bambu-gray px-1">{t('pickers.noProduct')}</p>
        )}
        {offerCreate && (
          <div className="px-1 pt-1">
            <p className="text-xs text-bambu-gray mb-1">{t('pickers.newProduct')}</p>
            <button
              type="button"
              onClick={() => createMutation.mutate(filter.trim())}
              disabled={createMutation.isPending}
              title={t('pickers.newProductName')}
              className="w-full px-3 py-1.5 rounded-lg text-sm bg-bambu-green/20 text-bambu-green hover:bg-bambu-green/30 transition-colors"
            >
              {t('pickers.create')} &quot;{filter.trim()}&quot;
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
