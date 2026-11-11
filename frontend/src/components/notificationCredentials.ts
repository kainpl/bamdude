import type { NotificationProvider, ProviderType } from '../api/client';

export const NOTIFICATION_SECRET_FIELDS: Record<ProviderType, string[]> = {
  callmebot: ['apikey'], ntfy: ['auth_token', 'topic'], pushover: ['user_key', 'app_token'],
  bark: ['device_key'], telegram: ['bot_token'], email: ['password'], discord: ['webhook_url'],
  webhook: ['webhook_url', 'auth_header'], homeassistant: [], signal: ['auth_header'],
};

export function notificationSecretFields(type: ProviderType, provider?: NotificationProvider | null) {
  return [...new Set([...NOTIFICATION_SECRET_FIELDS[type], ...Object.keys(provider?.secrets_configured ?? {})])];
}
