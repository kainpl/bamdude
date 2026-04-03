// eslint-disable-next-line @typescript-eslint/no-explicit-any
type TFn = (...args: any[]) => any;

const GROUP_KEYS: Record<string, string> = {
  'Administrators': 'administrators',
  'Operators': 'operators',
  'Viewers': 'viewers',
};

/** Get localized group name. Falls back to DB value if no mapping. */
export function getGroupName(dbName: string, t: TFn): string {
  const key = GROUP_KEYS[dbName];
  return key ? t(`groups.system.${key}.name`, dbName) : dbName;
}

/** Get localized group description. Falls back to DB value if no mapping. */
export function getGroupDescription(dbName: string, dbDescription: string | null, t: TFn): string {
  const key = GROUP_KEYS[dbName];
  return key ? t(`groups.system.${key}.description`, dbDescription || '') : dbDescription || '';
}
