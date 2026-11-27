import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Loader2, MapPin, Printer as PrinterIcon, Search, Users } from 'lucide-react';
import { api } from '../api/client';
import type { Group, Printer, PrinterLocationListItem } from '../api/client';
import { Card, CardContent, CardHeader } from './Card';
import { Button } from './Button';
import { Select } from './Select';

/**
 * Settings → Users → Printer access (N62, upstream Bambuddy #1727).
 *
 * A group can be limited to printers picked one by one and to whole locations — a location
 * gives every printer standing in it or in any location below it, printers added there later
 * too. A group that is not limited reaches every printer. Administrators is never limited.
 * The server decides who reaches what; this page edits the groups and shows the reach the
 * same rule gives.
 */

type Draft = { restrict: boolean; printers: Set<number>; locations: Set<number> };
type Mode = 'groups' | 'printers';

const ADMIN_GROUP = 'Administrators';

function draftOf(group: Group): Draft {
  return {
    restrict: group.restrict_printers,
    printers: new Set(group.printer_ids ?? []),
    locations: new Set(group.location_ids ?? []),
  };
}

function sameSet(a: Set<number>, b: Set<number>) {
  return a.size === b.size && [...a].every((v) => b.has(v));
}

function changed(group: Group, draft: Draft) {
  const base = draftOf(group);
  return base.restrict !== draft.restrict || !sameSet(base.printers, draft.printers) || !sameSet(base.locations, draft.locations);
}

/** Every location below each of ``roots``, the roots included. */
function subtree(locations: PrinterLocationListItem[], roots: Set<number>): Set<number> {
  const children = new Map<number | null, number[]>();
  for (const loc of locations) {
    const list = children.get(loc.parent_id) ?? [];
    list.push(loc.id);
    children.set(loc.parent_id, list);
  }
  const seen = new Set<number>();
  const stack = [...roots];
  while (stack.length) {
    const id = stack.pop()!;
    if (seen.has(id)) continue;
    seen.add(id);
    stack.push(...(children.get(id) ?? []));
  }
  return seen;
}

/** The printers a draft reaches — picked, or standing in a given location or below it. */
function reachOf(draft: Draft, printers: Printer[], locations: PrinterLocationListItem[]): Set<number> {
  const places = subtree(locations, draft.locations);
  return new Set(printers.filter((p) => draft.printers.has(p.id) || (p.location_id != null && places.has(p.location_id))).map((p) => p.id));
}

export function PrinterAccessSettings({ initialPrinterId = null }: { initialPrinterId?: number | null }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<Mode>(initialPrinterId != null ? 'printers' : 'groups');
  const [selectedPrinter, setSelectedPrinter] = useState<number | null>(initialPrinterId);
  const [drafts, setDrafts] = useState<Map<number, Draft>>(new Map());
  const [search, setSearch] = useState('');
  const [locationFilter, setLocationFilter] = useState<number | 'all'>('all');
  const [modelFilter, setModelFilter] = useState<string>('all');
  const [error, setError] = useState<string | null>(null);

  const { data: groups, isLoading: groupsLoading } = useQuery({ queryKey: ['groups'], queryFn: api.getGroups });
  const { data: printers, isLoading: printersLoading } = useQuery({ queryKey: ['printers'], queryFn: api.getPrinters });
  const { data: locationData } = useQuery({ queryKey: ['printer-locations'], queryFn: api.getPrinterLocations });
  const locations = useMemo(() => locationData?.locations ?? [], [locationData]);

  const editable = useMemo(() => (groups ?? []).filter((g) => g.name !== ADMIN_GROUP), [groups]);

  useEffect(() => {
    if (initialPrinterId != null) {
      setMode('printers');
      setSelectedPrinter(initialPrinterId);
    }
  }, [initialPrinterId]);

  const draftFor = (group: Group) => drafts.get(group.id) ?? draftOf(group);
  const update = (group: Group, change: (d: Draft) => Draft) => {
    const current = draftFor(group);
    const next = change({ restrict: current.restrict, printers: new Set(current.printers), locations: new Set(current.locations) });
    setDrafts((prev) => new Map(prev).set(group.id, next));
  };
  const dirty = editable.filter((g) => drafts.has(g.id) && changed(g, drafts.get(g.id)!));

  const save = useMutation({
    mutationFn: async () => {
      for (const group of dirty) {
        const d = drafts.get(group.id)!;
        await api.updateGroup(group.id, {
          restrict_printers: d.restrict,
          printer_ids: [...d.printers].sort((a, b) => a - b),
          location_ids: [...d.locations].sort((a, b) => a - b),
        });
      }
    },
    onSuccess: () => {
      setDrafts(new Map());
      setError(null);
      queryClient.invalidateQueries({ queryKey: ['groups'] });
    },
    onError: (e: Error) => setError(e.message),
  });

  const models = useMemo(() => [...new Set((printers ?? []).map((p) => p.model).filter((m): m is string => !!m))].sort(), [printers]);
  const shownPrinters = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const places = locationFilter === 'all' ? null : subtree(locations, new Set([locationFilter]));
    return (printers ?? []).filter(
      (p) =>
        (!needle || p.name.toLowerCase().includes(needle)) &&
        (modelFilter === 'all' || p.model === modelFilter) &&
        (places === null || (p.location_id != null && places.has(p.location_id))),
    );
  }, [printers, search, locationFilter, modelFilter, locations]);

  if (groupsLoading || printersLoading) {
    return (
      <div className="flex justify-center py-8">
        <Loader2 className="w-6 h-6 animate-spin text-bambu-green" />
      </div>
    );
  }

  const filters = (
    <div className="flex flex-wrap gap-2 items-center">
      <div className="relative flex-1 min-w-[12rem]">
        <Search className="w-4 h-4 absolute left-2 top-1/2 -translate-y-1/2 text-bambu-gray" />
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder={t('settings.printerAccess.searchPrinters')}
          aria-label={t('settings.printerAccess.searchPrinters')}
          className="w-full pl-8 pr-2 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded text-sm"
        />
      </div>
      <Select
        size="sm"
        value={locationFilter}
        onChange={(e) => setLocationFilter(e.target.value === 'all' ? 'all' : Number(e.target.value))}
        aria-label={t('settings.printerAccess.filterLocation')}
      >
        <option value="all">{t('settings.printerAccess.allLocations')}</option>
        {locations.map((loc) => (
          <option key={loc.id} value={loc.id}>
            {loc.path}
          </option>
        ))}
      </Select>
      <Select
        size="sm"
        value={modelFilter}
        onChange={(e) => setModelFilter(e.target.value)}
        aria-label={t('settings.printerAccess.filterModel')}
      >
        <option value="all">{t('settings.printerAccess.allModels')}</option>
        {models.map((m) => (
          <option key={m} value={m}>
            {m}
          </option>
        ))}
      </Select>
    </div>
  );

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-lg font-semibold flex items-center gap-2">
            <PrinterIcon className="w-5 h-5 text-bambu-green" />
            {t('settings.printerAccess.title')}
          </h2>
          <div className="flex gap-1" role="tablist">
            {(['groups', 'printers'] as Mode[]).map((m) => (
              <button
                key={m}
                role="tab"
                aria-selected={mode === m}
                onClick={() => setMode(m)}
                className={`px-3 py-1 text-sm rounded ${mode === m ? 'bg-bambu-green/20 text-bambu-green' : 'text-bambu-gray hover:text-white'}`}
              >
                {t(m === 'groups' ? 'settings.printerAccess.byGroup' : 'settings.printerAccess.byPrinter')}
              </button>
            ))}
          </div>
        </div>
        <p className="text-sm text-bambu-gray mt-1">{t('settings.printerAccess.description')}</p>
      </CardHeader>
      <CardContent className="space-y-4">
        {mode === 'groups' &&
          editable.map((group) => {
            const draft = draftFor(group);
            const reach = reachOf(draft, printers ?? [], locations);
            return (
              <div key={group.id} data-testid={`access-group-${group.id}`} className="border border-bambu-dark-tertiary rounded-lg p-3 space-y-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <Users className="w-4 h-4 text-bambu-gray" />
                    <span className="font-medium">{group.name}</span>
                    <span className="text-xs text-bambu-gray">{t('settings.printerAccess.members', { count: group.user_count })}</span>
                  </div>
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={draft.restrict}
                      onChange={(e) => update(group, (d) => ({ ...d, restrict: e.target.checked }))}
                    />
                    {t('settings.printerAccess.limit')}
                  </label>
                </div>
                {!draft.restrict ? (
                  <p className="text-sm text-bambu-gray">{t('settings.printerAccess.reachesAll')}</p>
                ) : (
                  <>
                    <p className="text-sm">{t('settings.printerAccess.reaches', { count: reach.size })}</p>
                    <div className="grid gap-3 md:grid-cols-2">
                      <fieldset className="space-y-1">
                        <legend className="text-xs uppercase text-bambu-gray mb-1 flex items-center gap-1">
                          <MapPin className="w-3 h-3" />
                          {t('settings.printerAccess.locations')}
                        </legend>
                        {locations.length === 0 && <p className="text-xs text-bambu-gray">{t('settings.printerAccess.noLocations')}</p>}
                        {locations.map((loc) => (
                          <label key={loc.id} className="flex items-center gap-2 text-sm" style={{ paddingLeft: `${(loc.depth - 1) * 16}px` }}>
                            <input
                              type="checkbox"
                              checked={draft.locations.has(loc.id)}
                              onChange={(e) =>
                                update(group, (d) => {
                                  if (e.target.checked) d.locations.add(loc.id);
                                  else d.locations.delete(loc.id);
                                  return d;
                                })
                              }
                            />
                            {loc.name}
                            <span className="text-xs text-bambu-gray">({loc.printer_count})</span>
                          </label>
                        ))}
                      </fieldset>
                      <fieldset className="space-y-1">
                        <legend className="text-xs uppercase text-bambu-gray mb-1 flex items-center gap-1">
                          <PrinterIcon className="w-3 h-3" />
                          {t('settings.printerAccess.printers')}
                        </legend>
                        {(printers ?? []).map((p) => {
                          const viaLocation = !draft.printers.has(p.id) && reach.has(p.id);
                          return (
                            <label key={p.id} className="flex items-center gap-2 text-sm">
                              <input
                                type="checkbox"
                                checked={draft.printers.has(p.id) || viaLocation}
                                disabled={viaLocation}
                                onChange={(e) =>
                                  update(group, (d) => {
                                    if (e.target.checked) d.printers.add(p.id);
                                    else d.printers.delete(p.id);
                                    return d;
                                  })
                                }
                              />
                              {p.name}
                              {viaLocation && <span className="text-xs text-bambu-gray">{t('settings.printerAccess.viaLocation')}</span>}
                            </label>
                          );
                        })}
                      </fieldset>
                    </div>
                  </>
                )}
              </div>
            );
          })}

        {mode === 'printers' && (
          <div className="space-y-3">
            {filters}
            <div className="grid gap-3 md:grid-cols-2">
              <ul className="space-y-1 max-h-96 overflow-y-auto" aria-label={t('settings.printerAccess.printers')}>
                {shownPrinters.map((p) => (
                  <li key={p.id}>
                    <button
                      onClick={() => setSelectedPrinter(p.id)}
                      aria-pressed={selectedPrinter === p.id}
                      className={`w-full text-left px-2 py-1.5 rounded text-sm ${selectedPrinter === p.id ? 'bg-bambu-green/20 text-bambu-green' : 'hover:bg-bambu-dark'}`}
                    >
                      {p.name}
                      {p.location && <span className="text-xs text-bambu-gray ml-2">{p.location.path}</span>}
                    </button>
                  </li>
                ))}
              </ul>
              <div className="space-y-2" data-testid="access-printer-panel">
                {selectedPrinter == null ? (
                  <p className="text-sm text-bambu-gray">{t('settings.printerAccess.pickPrinter')}</p>
                ) : (
                  <>
                    <p className="text-sm text-bambu-gray">{t('settings.printerAccess.whoReaches')}</p>
                    {editable.map((group) => {
                      const draft = draftFor(group);
                      const reach = reachOf(draft, printers ?? [], locations);
                      const picked = draft.printers.has(selectedPrinter);
                      const viaLocation = draft.restrict && !picked && reach.has(selectedPrinter);
                      return (
                        <label key={group.id} className="flex items-center gap-2 text-sm">
                          <input
                            type="checkbox"
                            checked={!draft.restrict || picked || viaLocation}
                            disabled={!draft.restrict || viaLocation}
                            onChange={(e) =>
                              update(group, (d) => {
                                if (e.target.checked) d.printers.add(selectedPrinter);
                                else d.printers.delete(selectedPrinter);
                                return d;
                              })
                            }
                          />
                          {group.name}
                          {!draft.restrict && <span className="text-xs text-bambu-gray">{t('settings.printerAccess.notLimited')}</span>}
                          {viaLocation && <span className="text-xs text-bambu-gray">{t('settings.printerAccess.viaLocation')}</span>}
                        </label>
                      );
                    })}
                  </>
                )}
              </div>
            </div>
          </div>
        )}

        {error && <p className="text-sm text-red-700 dark:text-red-400" role="alert">{error}</p>}
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={() => setDrafts(new Map())} disabled={dirty.length === 0 || save.isPending}>
            {t('common.cancel')}
          </Button>
          <Button onClick={() => save.mutate()} disabled={dirty.length === 0 || save.isPending}>
            {save.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : t('settings.printerAccess.save', { count: dirty.length })}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
