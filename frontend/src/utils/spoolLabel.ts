export interface SpoolLabel {
  spoolId: number;
  key: string;
}

export function parseSpoolLabel(params: URLSearchParams): SpoolLabel | null {
  const [spool, source, version, key] = ['spool', 'src', 'v', 'key'].map((name) => params.getAll(name));
  const spoolId = Number(spool[0]);
  if (
    spool.length !== 1 || !/^[1-9]\d*$/.test(spool[0]) || !Number.isSafeInteger(spoolId) ||
    source.length !== 1 || source[0] !== 'local' ||
    version.length !== 1 || version[0] !== '2' ||
    key.length !== 1 || !/^[a-f0-9]{32}$/.test(key[0])
  ) return null;

  return { spoolId, key: key[0] };
}

export function readSpoolLabel(payload: string): SpoolLabel | null {
  try {
    const url = new URL(payload);
    if (!['http:', 'https:'].includes(url.protocol) || !url.pathname.endsWith('/inventory')) return null;
    return parseSpoolLabel(url.searchParams);
  } catch {
    return null;
  }
}
