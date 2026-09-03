import { Link, useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ClipboardList, Package, Users } from 'lucide-react';

const TABS = [
  // `/projects/12` is an order's own page and carries a breadcrumb instead, so
  // the Orders tab deliberately does not light up there.
  { to: '/projects', key: 'projects.tabs.orders', icon: ClipboardList, match: /^\/projects(?!\/\d)/ },
  { to: '/products', key: 'projects.tabs.products', icon: Package, match: /^\/products/ },
  { to: '/customers', key: 'projects.tabs.customers', icon: Users, match: /^\/customers/ },
] as const;

/**
 * The three faces of the Projects section.
 *
 * Tabs are navigation between sibling roots, not `?tab=` state — a product URL
 * must stand on its own when someone copies it out of the address bar.
 */
export function ProjectsTabs() {
  const { t } = useTranslation();
  const { pathname } = useLocation();

  return (
    <nav className="flex gap-1 border-b border-bambu-dark-tertiary mb-4" aria-label={t('nav.projects')}>
      {TABS.map(({ to, key, icon: Icon, match }) => {
        const active = match.test(pathname);
        return (
          // A plain Link, not NavLink: NavLink renders `aria-current` from its OWN
          // internally computed match (prefix of `to`, no `end` prop), which does not
          // know about our exclusion regex — it would mark Orders current on
          // /projects/12 even though `active` here is false. One `active` value drives
          // both the class and aria-current instead.
          <Link
            key={to}
            to={to}
            aria-current={active ? 'page' : undefined}
            className={`flex items-center gap-2 px-4 py-2 text-sm border-b-2 -mb-px transition-colors ${
              active
                ? 'border-bambu-green text-bambu-dark dark:text-white'
                : 'border-transparent text-bambu-gray hover:text-bambu-dark dark:hover:text-white'
            }`}
          >
            <Icon className="w-4 h-4" />
            {t(key)}
          </Link>
        );
      })}
    </nav>
  );
}
