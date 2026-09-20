import { StrictMode, Suspense, lazy } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import i18n from './i18n'

// Select the standalone shell BEFORE importing/mounting app providers. In
// particular, token TVs must not start settings, camera or auth requests.
const Entry = window.location.pathname === '/monitor'
  ? lazy(() => import('./features/monitor/MonitorApp'))
  : lazy(() => import('./App.tsx'));

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Suspense fallback={<div role="status" className="p-8 text-bambu-gray">{i18n.t('common.loading')}</div>}>
      <Entry />
    </Suspense>
  </StrictMode>,
)
