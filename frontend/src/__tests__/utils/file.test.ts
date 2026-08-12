import { describe, it, expect } from 'vitest';
import { fileActivityAt, formatFileSize } from '../../utils/file';

describe('formatFileSize', () => {
  it('returns "0 B" for 0 bytes', () => {
    expect(formatFileSize(0)).toBe('0 B');
  });

  it('returns bytes without decimals for values under 1 KB', () => {
    expect(formatFileSize(1)).toBe('1 B');
    expect(formatFileSize(500)).toBe('500 B');
    expect(formatFileSize(1023)).toBe('1023 B');
  });

  it('returns KB with 1 decimal for values under 1 MB', () => {
    expect(formatFileSize(1024)).toBe('1.0 KB');
    expect(formatFileSize(1536)).toBe('1.5 KB');
    expect(formatFileSize(10240)).toBe('10.0 KB');
  });

  it('returns MB with 1 decimal for values under 1 GB', () => {
    expect(formatFileSize(1048576)).toBe('1.0 MB');
    expect(formatFileSize(1572864)).toBe('1.5 MB');
    expect(formatFileSize(10485760)).toBe('10.0 MB');
  });

  it('returns GB with 1 decimal for values under 1 TB', () => {
    expect(formatFileSize(1073741824)).toBe('1.0 GB');
    expect(formatFileSize(1610612736)).toBe('1.5 GB');
  });

  it('returns TB with 1 decimal for very large values', () => {
    expect(formatFileSize(1099511627776)).toBe('1.0 TB');
    expect(formatFileSize(1649267441664)).toBe('1.5 TB');
  });

  it('handles edge cases at unit boundaries', () => {
    expect(formatFileSize(1023)).toBe('1023 B');
    expect(formatFileSize(1024)).toBe('1.0 KB');
    expect(formatFileSize(1048575)).toBe('1024.0 KB');
    expect(formatFileSize(1048576)).toBe('1.0 MB');
  });
});

describe('fileActivityAt (#2680)', () => {
  it('uses created_at for an uploaded file, which has no on-disk mtime', () => {
    expect(fileActivityAt({ created_at: '2026-08-08T10:00:00', fs_modified_at: null })).toBe(
      '2026-08-08T10:00:00'
    );
  });

  it('prefers the on-disk mtime even when it is older than created_at', () => {
    // This is the whole point. A folder scan writes the SAME created_at onto
    // every row it discovers, so preferring the newer of the two would return
    // the scan instant for everything and reproduce the tie the column exists
    // to break. fs_modified_at replaces created_at; it does not compete with it.
    expect(
      fileActivityAt({ created_at: '2026-08-08T10:00:00', fs_modified_at: '2020-09-13T12:26:40' })
    ).toBe('2020-09-13T12:26:40');
  });

  it('falls back when the field is absent entirely, not just null', () => {
    // External rows scanned before m129 have no value, and an older backend
    // omits the key — both must still produce a date rather than undefined.
    expect(fileActivityAt({ created_at: '2026-08-08T10:00:00' })).toBe('2026-08-08T10:00:00');
  });
});
