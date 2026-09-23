import { useTranslation } from 'react-i18next';

export type ListView = 'cards' | 'table';

/**
 * The cards/table switch every list of the projects section shares — lifted
 * out of OrdersPage, same look (spec projects-lists-parity, rule 10). The
 * choice is a preference: the page keeps it with `usePersistedState`.
 */
export function ListViewToggle({ value, onChange }: { value: ListView; onChange: (view: ListView) => void }) {
  const { t } = useTranslation();
  return (
    <div
      role="group"
      aria-label={t('list.view.label')}
      className="flex rounded-lg border border-bambu-dark-tertiary overflow-hidden text-sm"
    >
      {(['cards', 'table'] as const).map((v) => (
        <button
          key={v}
          type="button"
          aria-pressed={value === v}
          onClick={() => onChange(v)}
          className={`px-3 py-1.5 ${value === v ? 'bg-bambu-dark-tertiary text-white' : 'text-bambu-gray hover:text-white'}`}
        >
          {t(v === 'cards' ? 'list.view.cards' : 'list.view.table')}
        </button>
      ))}
    </div>
  );
}
