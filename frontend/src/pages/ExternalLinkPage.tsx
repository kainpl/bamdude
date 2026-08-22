import { useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { AlertTriangle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { api } from '../api/client';
import { LoadingBlock } from '../components/LoadingBlock';
import { useTheme } from '../contexts/ThemeContext';

export function ExternalLinkPage() {
  const { t } = useTranslation();
  const { id } = useParams<{ id: string }>();
  const { mode } = useTheme();

  const { data: link, isLoading, error } = useQuery({
    queryKey: ['external-link', id],
    queryFn: () => api.getExternalLink(Number(id)),
    enabled: !!id,
  });

  // Nothing to split here: the page IS the embedded site, so there is no header
  // or filter of ours to draw first. The label is the whole improvement — a bare
  // spinner over a blank frame does not say whether it is us or the far end that
  // is slow.
  if (isLoading) {
    return <LoadingBlock label={t('common.loading')} className="h-full text-bambu-gray" />;
  }

  if (error || !link) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-4 text-bambu-gray">
        <AlertTriangle className="w-12 h-12" />
        <p>{t('common.linkNotFound')}</p>
      </div>
    );
  }

  return (
    <iframe
      src={link.url}
      className="h-full w-full border-0"
      style={{ colorScheme: mode }}
      title={link.name}
      sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"
    />
  );
}
