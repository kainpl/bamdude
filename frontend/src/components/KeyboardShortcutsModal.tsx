import { useEffect } from 'react';
import { X, Keyboard, ExternalLink } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Card, CardContent } from './Card';

interface NavItem {
  id: string;
  to: string;
  labelKey: string;
}

interface SidebarItem {
  type: 'nav' | 'external';
  label: string;
  labelKey?: string;
}

interface KeyboardShortcutsModalProps {
  onClose: () => void;
  navItems?: NavItem[];
  sidebarItems?: SidebarItem[];
}

interface ShortcutItem {
  /** One badge per element. ``+`` between two elements means "press together"
   * (e.g. ``Ctrl+Click``); separate items in different rows mean "either". */
  keys: string[];
  /** Already-translated description. */
  description: string;
  isExternal?: boolean;
}

interface ShortcutSection {
  /** Already-translated category label. */
  category: string;
  items: ShortcutItem[];
}

function getShortcuts(
  sidebarItems: SidebarItem[] | undefined,
  navItems: NavItem[] | undefined,
  t: (key: string, options?: Record<string, unknown>) => string,
): ShortcutSection[] {
  // Use sidebarItems if provided (new format), otherwise fall back to navItems.
  // ``Open <X>`` for external sidebar entries, ``Go to <X>`` for in-app routes.
  const navShortcuts: ShortcutItem[] = sidebarItems
    ? sidebarItems.slice(0, 9).map((item, index) => ({
        keys: [String(index + 1)],
        description:
          item.type === 'external'
            ? t('shortcuts.openLabel', { label: item.label })
            : t('shortcuts.goToLabel', {
                label: item.labelKey ? t(item.labelKey) : item.label,
              }),
        isExternal: item.type === 'external',
      }))
    : navItems
    ? navItems.map((item, index) => ({
        keys: [String(index + 1)],
        description: t('shortcuts.goToLabel', { label: t(item.labelKey) }),
      }))
    : [
        // Fallback when neither nav source is wired in. Uses the canonical
        // labelKeys so the strings stay in sync with the actual sidebar.
        { keys: ['1'], description: t('shortcuts.goToLabel', { label: t('nav.printers') }) },
        { keys: ['2'], description: t('shortcuts.goToLabel', { label: t('nav.archives') }) },
        { keys: ['3'], description: t('shortcuts.goToLabel', { label: t('nav.queue') }) },
        { keys: ['4'], description: t('shortcuts.goToLabel', { label: t('nav.stats') }) },
        { keys: ['5'], description: t('shortcuts.goToLabel', { label: t('nav.profiles') }) },
        { keys: ['6'], description: t('shortcuts.goToLabel', { label: t('nav.settings') }) },
      ];

  return [
    { category: t('shortcuts.section.navigation'), items: navShortcuts },
    {
      category: t('shortcuts.section.printers'),
      items: [
        { keys: [t('shortcuts.click')], description: t('shortcuts.printers.checkboxClick') },
        {
          keys: [t('shortcuts.modifier.ctrl'), t('shortcuts.click')],
          description: t('shortcuts.printers.toggleSelect'),
        },
        {
          keys: [t('shortcuts.modifier.shift'), t('shortcuts.click')],
          description: t('shortcuts.printers.rangeSelect'),
        },
        { keys: ['Esc'], description: t('shortcuts.printers.clearSelection') },
      ],
    },
    {
      category: t('shortcuts.section.archives'),
      items: [
        { keys: ['/'], description: t('shortcuts.archives.focusSearch') },
        { keys: ['U'], description: t('shortcuts.archives.openUpload') },
        { keys: ['Esc'], description: t('shortcuts.archives.clearOrBlur') },
        { keys: [t('shortcuts.rightClick')], description: t('shortcuts.archives.contextMenu') },
      ],
    },
    {
      category: t('shortcuts.section.kProfiles'),
      items: [
        { keys: ['R'], description: t('shortcuts.kprofiles.refresh') },
        { keys: ['N'], description: t('shortcuts.kprofiles.new') },
        { keys: ['Esc'], description: t('shortcuts.kprofiles.exitSelection') },
      ],
    },
    {
      category: t('shortcuts.section.general'),
      items: [{ keys: ['?'], description: t('shortcuts.general.help') }],
    },
  ];
}

function KeyBadge({ children }: { children: string }) {
  return (
    <kbd className="px-2 py-1 text-xs font-mono bg-bambu-dark border border-bambu-dark-tertiary rounded text-white">
      {children}
    </kbd>
  );
}

export function KeyboardShortcutsModal({ onClose, navItems, sidebarItems }: KeyboardShortcutsModalProps) {
  const { t } = useTranslation();
  const shortcuts = getShortcuts(sidebarItems, navItems, t);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4"
      onClick={onClose}
    >
      <Card className="w-full max-w-md" onClick={(e) => e.stopPropagation()}>
        <CardContent className="p-0">
          <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
            <div className="flex items-center gap-2">
              <Keyboard className="w-5 h-5 text-bambu-green" />
              <h2 className="text-xl font-semibold text-white">{t('shortcuts.title')}</h2>
            </div>
            <button
              onClick={onClose}
              className="text-bambu-gray hover:text-white transition-colors"
              title={t('common.close', 'Close')}
            >
              <X className="w-5 h-5" />
            </button>
          </div>

          <div className="p-4 space-y-6 max-h-[60vh] overflow-y-auto">
            {shortcuts.map((section) => (
              <div key={section.category}>
                <h3 className="text-sm font-medium text-bambu-gray mb-3">{section.category}</h3>
                <div className="space-y-2">
                  {section.items.map((shortcut, idx) => (
                    <div
                      key={`${section.category}-${idx}`}
                      className="flex items-center justify-between gap-3"
                    >
                      <span className="text-white text-sm flex items-center gap-1.5">
                        {shortcut.description}
                        {shortcut.isExternal && (
                          <ExternalLink className="w-3 h-3 text-bambu-gray" />
                        )}
                      </span>
                      <div className="flex items-center gap-1 flex-shrink-0">
                        {shortcut.keys.map((key, i) => (
                          <span key={i} className="flex items-center gap-1">
                            {i > 0 && <span className="text-xs text-bambu-gray">+</span>}
                            <KeyBadge>{key}</KeyBadge>
                          </span>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>

          <div className="p-4 border-t border-bambu-dark-tertiary">
            <p className="text-xs text-bambu-gray text-center">
              {t('shortcuts.footerPrefix')} <KeyBadge>Esc</KeyBadge>{' '}
              {t('shortcuts.footerSuffix')}
            </p>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
