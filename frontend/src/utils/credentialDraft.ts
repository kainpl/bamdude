import type { SecretAction } from '../api/client';

/** Presence is UI metadata; only an intentional replacement sends a value. */
export function credentialDraft<K extends string>(field: K, value: string | undefined, action: SecretAction = 'keep', configured = false):
  Partial<Record<K, string>> & { secret_actions?: Record<string, SecretAction> } {
  let draft: Record<string, unknown> = {};
  if (action === 'clear') draft = { secret_actions: { [field]: 'clear' } };
  else if (action === 'replace' || value) draft = { [field]: value ?? '', secret_actions: { [field]: 'replace' } };
  else if (configured) draft = { secret_actions: { [field]: 'keep' } };
  return draft as Partial<Record<K, string>> & { secret_actions?: Record<string, SecretAction> };
}
