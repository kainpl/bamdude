import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Sparkles } from 'lucide-react';
import { api, mcpApi, type APIKeyCreateResponse, type McpPackageId } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { useToast } from '../../contexts/ToastContext';
import { mcpKeyPreset } from '../../lib/mcpKeyPreset';
import { Button } from '../Button';
import { Card, CardContent, CardHeader } from '../Card';
import { CopyButton } from '../CopyButton';
import { Toggle } from '../Toggle';

const inputClass = 'w-full min-w-0 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark px-3 py-2 text-white focus:border-bambu-green focus:outline-none';
type Host = 'codex' | 'claude-code' | 'claude-desktop' | 'chatgpt';
const hosts: Host[] = ['codex', 'claude-code', 'claude-desktop', 'chatgpt'];
const hostPackages: Record<Host, McpPackageId | null> = { codex: 'portable-plugin', 'claude-code': 'claude-code', 'claude-desktop': 'claude-desktop', chatgpt: null };

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function McpSettingsCard() {
  const { hasPermission } = useAuth();
  return hasPermission('settings:read') ? <McpSettingsContent /> : null;
}

function McpSettingsContent() {
  const { t, i18n } = useTranslation();
  const { hasPermission } = useAuth();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const canEdit = hasPermission('settings:update');
  const canCreate = hasPermission('api_keys:create');
  const canObserve = hasPermission('api_keys:update');
  const [dirty, setDirty] = useState(false);
  const [endpoint, setEndpoint] = useState('');
  const [enabled, setEnabled] = useState(false);
  const [hostList, setHostList] = useState('');
  const [originList, setOriginList] = useState('');
  const [host, setHost] = useState<Host>('codex');
  const [keyName, setKeyName] = useState('BamDude AI');
  const [allPrinters, setAllPrinters] = useState(false);
  const [printerIds, setPrinterIds] = useState<number[]>([]);
  const [logs, setLogs] = useState(false);
  const [created, setCreated] = useState<APIKeyCreateResponse | null>(null);
  const [keyId, setKeyId] = useState<number | null>(null);
  const [skillInstalled, setSkillInstalled] = useState(false);
  const settings = useQuery({ queryKey: ['settings'], queryFn: api.getSettings });
  const status = useQuery({ queryKey: ['mcp', 'status'], queryFn: mcpApi.status, refetchInterval: 15000 });
  const packages = useQuery({ queryKey: ['mcp', 'packages'], queryFn: mcpApi.packages });
  const printers = useQuery({ queryKey: ['printers'], queryFn: api.getPrinters, enabled: canCreate && hasPermission('printers:read') });
  const keys = useQuery({ queryKey: ['api-keys'], queryFn: api.getAPIKeys, enabled: hasPermission('api_keys:read') });
  const observed = useQuery({
    queryKey: ['mcp', 'connection', keyId], queryFn: () => mcpApi.connection(keyId!),
    enabled: canObserve && keyId !== null, refetchOnWindowFocus: false,
  });
  useEffect(() => {
    if (!settings.data || dirty) return;
    setEndpoint(settings.data.mcp_endpoint_url ?? '');
    setEnabled(settings.data.mcp_enabled ?? false);
    setHostList((settings.data.mcp_allowed_hosts ?? []).join('\n'));
    setOriginList((settings.data.mcp_allowed_origins ?? []).join('\n'));
  }, [settings.data, dirty]);
  const failure = () => showToast(t('settings.mcp.actionFailed'), 'error');
  const save = useMutation({
    mutationFn: async () => {
      let canonical = '';
      const allowed = hostList.split(/\s+/).filter(Boolean);
      if (endpoint.trim()) {
        const url = new URL(endpoint.trim());
        if (url.pathname !== '/mcp' || url.username || url.password || url.search || url.hash
          || (url.protocol !== 'https:' && !(url.protocol === 'http:' && (url.hostname === 'localhost' || url.hostname === '[::1]' || /^127(?:\.\d{1,3}){3}$/.test(url.hostname))))) {
          throw new Error('invalid_endpoint');
        }
        canonical = url.href;
        allowed.push(url.host);
      }
      return api.updateSettings({ mcp_enabled: enabled, mcp_endpoint_url: canonical,
        mcp_allowed_hosts: [...new Set(allowed)], mcp_allowed_origins: originList.split(/\s+/).filter(Boolean) });
    },
    onSuccess: data => {
      queryClient.setQueryData(['settings'], data);
      setDirty(false);
      void queryClient.invalidateQueries({ queryKey: ['mcp', 'status'] });
      showToast(t('settings.mcp.saved'));
    },
    onError: failure,
  });
  const create = useMutation({
    gcTime: 0, // Drop the one-time secret from the mutation cache once reset/unmounted.
    mutationFn: () => api.createAPIKey({ ...mcpKeyPreset, name: keyName.trim(), printer_ids: allPrinters ? null : printerIds, can_read_mcp_logs: logs }),
    onSuccess: data => {
      setCreated(data);
      setKeyId(data.id);
      void queryClient.invalidateQueries({ queryKey: ['api-keys'] });
    },
    onError: failure,
  });
  const download = useMutation({
    mutationFn: async (id: McpPackageId) => {
      const blob = await mcpApi.download(id);
      saveBlob(blob, id === 'skill' ? 'bamdude-diagnostics.zip' : `bamdude-${id}.${id === 'claude-desktop' ? 'mcpb' : 'zip'}`);
    },
    onError: failure,
  });
  const canonical = settings.data?.mcp_endpoint_url ?? '';
  const command = canonical ? `codex mcp add bamdude --url '${canonical.replaceAll("'", "''")}' --bearer-token-env-var BAMDUDE_MCP_KEY` : '';
  const configuration = host === 'codex'
    ? `[mcp_servers.bamdude]\nurl = ${JSON.stringify(canonical)}\nbearer_token_env_var = "BAMDUDE_MCP_KEY"\n`
    : JSON.stringify({ mcpServers: { bamdude: { type: 'http', url: canonical, headers: { 'X-API-Key': '${BAMDUDE_MCP_KEY}' } } } }, null, 2);
  const selectedPackage = hostPackages[host];
  const downloadable = packages.data?.packages.find(item => item.id === selectedPackage)?.available;
  const skillAvailable = packages.data?.packages.find(item => item.id === 'skill')?.available;
  const state = status.isError ? undefined : status.data?.state;
  const keyExists = keyId !== null && (keys.data?.some(key => key.id === keyId && key.enabled && key.can_access_mcp) || (!keys.data && created?.id === keyId));
  const connectionSeen = !observed.isError && keyExists && observed.data?.observed_at;
  const docs = `https://docs.bamdude.top/${i18n.language.startsWith('uk') ? 'uk/' : ''}addons/ai-assistants/`;

  return <Card id="card-mcp" className="xl:col-span-2">
    <CardHeader><h2 className="flex items-center gap-2 text-lg font-semibold text-white"><Sparkles className="h-5 w-5 text-bambu-green" />{t('settings.mcp.title')}</h2></CardHeader>
    <CardContent className="space-y-6">
      <p className="max-w-3xl text-sm text-bambu-gray">{t('settings.mcp.intro')}</p>
      <section className="space-y-3" aria-labelledby="mcp-server-heading">
        <h3 id="mcp-server-heading" className="font-medium text-white">{t('settings.mcp.serverStep')}</h3>
        <div className="flex flex-wrap items-center gap-3"><Toggle label={t('settings.mcp.enabled')} checked={enabled} disabled={!canEdit || settings.isPending} onChange={value => { setEnabled(value); setDirty(true); }} /><span>{t('settings.mcp.enabled')}</span>
          <span role="status" className={state === 'ready' ? 'text-bambu-green' : state === 'unavailable' ? 'text-red-400' : 'text-bambu-gray'}>{state ? t(`settings.mcp.states.${state}`) : t('settings.mcp.statusUnavailable')}</span>
        </div>
        {!status.isError && status.data?.reason && <p className="text-sm text-amber-400">{t(`settings.mcp.reasons.${status.data.reason}`, { defaultValue: t('settings.mcp.statusUnavailable') })}</p>}
        <label className="block text-sm text-bambu-gray">{t('settings.mcp.address')}<input type="url" className={`${inputClass} mt-1`} placeholder="https://farm.example/mcp" value={endpoint} disabled={!canEdit} onChange={event => { setEndpoint(event.target.value); setDirty(true); }} /></label>
        <p className="max-w-3xl text-sm text-bambu-gray">{t('settings.mcp.networkHint')}</p>
        <p className="max-w-3xl text-sm text-bambu-gray">{t('settings.mcp.certificateHint')}</p>
        <details><summary className="cursor-pointer text-sm text-bambu-gray">{t('settings.mcp.advanced')}</summary><div className="mt-3 grid gap-3 md:grid-cols-2">
          <label className="text-sm text-bambu-gray">{t('settings.mcp.allowedHosts')}<textarea className={`${inputClass} mt-1`} rows={3} value={hostList} disabled={!canEdit} onChange={event => { setHostList(event.target.value); setDirty(true); }} /></label>
          <label className="text-sm text-bambu-gray">{t('settings.mcp.allowedOrigins')}<textarea className={`${inputClass} mt-1`} rows={3} value={originList} disabled={!canEdit} onChange={event => { setOriginList(event.target.value); setDirty(true); }} /></label>
        </div></details>
        <Button onClick={() => save.mutate()} disabled={!canEdit || !settings.data || !dirty || save.isPending}>{t('settings.mcp.save')}</Button>
      </section>
      <section className="space-y-3 border-t border-bambu-dark-tertiary pt-5" aria-labelledby="mcp-client-heading">
        <h3 id="mcp-client-heading" className="font-medium text-white">{t('settings.mcp.clientStep')}</h3>
        <label className="block text-sm text-bambu-gray">{t('settings.mcp.client')}<select className={`${inputClass} mt-1 max-w-md`} value={host} onChange={event => { setHost(event.target.value as Host); setSkillInstalled(false); }}>{hosts.map(id => <option key={id} value={id}>{t(`settings.mcp.hosts.${id}`)}</option>)}</select></label>
        <p className="max-w-3xl text-sm text-amber-400">{t(host === 'chatgpt' ? 'settings.mcp.chatgptUnsupported' : host === 'codex' ? 'settings.mcp.codexVerified' : host === 'claude-code' ? 'settings.mcp.claudeCodeVerified' : 'settings.mcp.clientUnverified')}</p>
        {host !== 'chatgpt' && <>
          <p className="max-w-3xl text-sm text-bambu-gray">{t(`settings.mcp.instructions.${host}`)}</p>
          <div className="flex flex-wrap items-center gap-2"><Button variant="secondary" disabled={!downloadable || download.isPending} onClick={() => selectedPackage && download.mutate(selectedPackage)}>{t('settings.mcp.downloadPlugin')}</Button>
            <Button variant="secondary" disabled={!skillAvailable || download.isPending} onClick={() => download.mutate('skill')}>{t('settings.mcp.downloadSkill')}</Button></div>
          {!downloadable && <p className="text-sm text-bambu-gray">{t('settings.mcp.packageUnavailable')}</p>}
        </>}
      </section>
      {host !== 'chatgpt' && <section className="space-y-3 border-t border-bambu-dark-tertiary pt-5" aria-labelledby="mcp-key-heading">
        <h3 id="mcp-key-heading" className="font-medium text-white">{t('settings.mcp.keyStep')}</h3>
        <p className="max-w-3xl text-sm text-bambu-gray">{t('settings.mcp.ownerCeiling')}</p>
        {canCreate ? <>
          <label className="block max-w-md text-sm text-bambu-gray">{t('settings.keyName')}<input className={`${inputClass} mt-1`} value={keyName} maxLength={255} onChange={event => setKeyName(event.target.value)} /></label>
          <fieldset className="space-y-2 text-sm"><legend className="mb-2 text-bambu-gray">{t('settings.mcp.printerScope')}</legend>
            <label className="flex items-center gap-2"><input type="checkbox" checked={allPrinters} onChange={event => setAllPrinters(event.target.checked)} />{t('settings.mcp.allPrinters')}</label>
            {!allPrinters && (printers.data ?? []).map(printer => <label key={printer.id} className="flex items-center gap-2"><input type="checkbox" checked={printerIds.includes(printer.id)} onChange={event => setPrinterIds(ids => event.target.checked ? [...ids, printer.id] : ids.filter(id => id !== printer.id))} />{printer.name}</label>)}
          </fieldset>
          <label className="flex items-start gap-2 text-sm"><input className="mt-1" type="checkbox" checked={logs} onChange={event => setLogs(event.target.checked)} />{t('settings.mcp.logConsent')}</label>
          <Button disabled={!keyName.trim() || (!allPrinters && printerIds.length === 0) || create.isPending} onClick={() => create.mutate()}>{t('settings.mcp.createKey')}</Button>
        </> : <p className="text-sm text-bambu-gray">{t('settings.mcp.askKeyManager')}</p>}
        {created && <div className="space-y-2 rounded-lg border border-bambu-green p-3"><p className="text-sm">{t('settings.apiKeyCopyWarning')}</p><div className="flex min-w-0 items-start gap-2"><code className="min-w-0 flex-1 break-all text-sm text-bambu-green">{created.key}</code><CopyButton value={created.key} /></div><Button size="sm" variant="secondary" onClick={() => { setCreated(null); create.reset(); }}>{t('common.dismiss')}</Button></div>}
        <p className="max-w-3xl text-sm text-bambu-gray">{t('settings.mcp.providerWarning')}</p>
      </section>}
      <section className="space-y-3 border-t border-bambu-dark-tertiary pt-5" aria-labelledby="mcp-check-heading">
        <h3 id="mcp-check-heading" className="font-medium text-white">{t('settings.mcp.checkStep')}</h3>
        <p className="max-w-3xl text-sm text-bambu-gray">{t('settings.mcp.firstRequest')}</p>
        {hasPermission('api_keys:read') && <label className="block max-w-md text-sm text-bambu-gray">{t('settings.mcp.checkKey')}<select className={`${inputClass} mt-1`} value={keyId ?? ''} onChange={event => setKeyId(event.target.value ? Number(event.target.value) : null)}><option value="">{t('settings.mcp.chooseKey')}</option>{keys.data?.filter(key => key.enabled && key.can_access_mcp).map(key => <option value={key.id} key={key.id}>{key.name}</option>)}</select></label>}
        <div className="flex flex-wrap items-center gap-3"><Button variant="secondary" disabled={!canObserve || keyId === null || observed.isFetching} onClick={() => void observed.refetch()}>{t('settings.mcp.checkConnection')}</Button><p role="status" className={`text-sm ${connectionSeen ? 'text-bambu-green' : 'text-bambu-gray'}`}>{connectionSeen ? t('settings.mcp.observed', { time: new Date(connectionSeen).toLocaleString(i18n.language) }) : observed.isError ? t('settings.mcp.checkFailed') : t('settings.mcp.notObserved')}</p></div>
        {!canObserve && <p className="text-sm text-bambu-gray">{t('settings.mcp.askKeyManager')}</p>}
        <label className="flex items-start gap-2 text-sm"><input type="checkbox" className="mt-1" checked={skillInstalled} onChange={event => setSkillInstalled(event.target.checked)} />{t('settings.mcp.skillInstalled')}</label>
        <p className="max-w-3xl text-sm text-bambu-gray">{t('settings.mcp.restartHint')}</p>
      </section>
      {(host === 'codex' || host === 'claude-code') && <details><summary className="cursor-pointer text-sm text-bambu-gray">{t('settings.mcp.manualConfig')}</summary><div className="mt-3 space-y-3">
        {host === 'codex' && command && <div className="flex min-w-0 items-start gap-2"><code className="min-w-0 flex-1 break-all text-xs">{command}</code><CopyButton value={command} /></div>}
        <p className="text-sm text-bambu-gray">{t('settings.mcp.configHint')}</p>
        {host === 'claude-code' && <p className="text-sm text-bambu-gray">{t('settings.mcp.directClaudeHint')}</p>}
        <Button variant="secondary" disabled={!canonical} onClick={() => saveBlob(new Blob([configuration], { type: 'text/plain' }), host === 'codex' ? 'bamdude-mcp.example.toml' : 'bamdude-mcp.example.json')}>{t('settings.mcp.downloadConfig')}</Button>
      </div></details>}
      <p className="max-w-3xl text-sm text-bambu-gray">{t('settings.mcp.disconnect')}</p>
      <a className="inline-block text-sm text-bambu-green hover:underline" href={docs} target="_blank" rel="noopener noreferrer">{t('settings.mcp.guide')}</a>
    </CardContent>
  </Card>;
}
