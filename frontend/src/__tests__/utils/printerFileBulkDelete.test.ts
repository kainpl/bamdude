import { describe, expect, it, vi } from 'vitest';

import { deletePrinterFilesOneByOne, stopsTheBatch } from '../../utils/printerFileBulkDelete';

const refusal = (message: string, code?: string, status = 409) => Object.assign(new Error(message), { code, status });

describe('deleting several printer files, one request each', () => {
  it('reports every file as deleted when the printer gave them all up', async () => {
    const deleteOne = vi.fn().mockResolvedValue({});
    const report = await deletePrinterFilesOneByOne(['/a.3mf', '/b.3mf'], deleteOne);
    expect(report).toEqual({ deleted: ['/a.3mf', '/b.3mf'], kept: [], notAttempted: [] });
    expect(deleteOne).toHaveBeenCalledTimes(2);
  });

  it('goes on past a file the printer keeps, and says which one and why', async () => {
    const deleteOne = vi.fn(async (path: string) => {
      if (path === '/Cube.3mf') throw refusal('This file belongs to the print in progress', 'active_print');
      return {};
    });
    const report = await deletePrinterFilesOneByOne(['/a.3mf', '/Cube.3mf', '/b.3mf'], deleteOne);
    expect(report.deleted).toEqual(['/a.3mf', '/b.3mf']);
    expect(report.kept).toEqual([
      { path: '/Cube.3mf', message: 'This file belongs to the print in progress', code: 'active_print' },
    ]);
    expect(report.notAttempted).toEqual([]);
    expect(report.stopMessage).toBeUndefined();
  });

  it('goes on past a file that failed for its own reason', async () => {
    const deleteOne = vi.fn(async (path: string) => {
      if (path === '/gone.3mf') throw refusal('File not found on printer: /gone.3mf', undefined, 404);
      return {};
    });
    const report = await deletePrinterFilesOneByOne(['/gone.3mf', '/b.3mf'], deleteOne);
    expect(report.deleted).toEqual(['/b.3mf']);
    expect(report.kept.map((file) => file.path)).toEqual(['/gone.3mf']);
  });

  it('stops when the printer itself cannot be asked, and names the files it did not reach', async () => {
    const deleteOne = vi.fn(async (path: string) => {
      if (path !== '/a.3mf') throw refusal("The printer's current state is not known", 'printer_state_unknown');
      return {};
    });
    const report = await deletePrinterFilesOneByOne(['/a.3mf', '/b.3mf', '/c.3mf'], deleteOne);
    expect(report.deleted).toEqual(['/a.3mf']);
    expect(report.kept).toEqual([]);
    expect(report.notAttempted).toEqual(['/b.3mf', '/c.3mf']);
    expect(report.stopMessage).toBe("The printer's current state is not known");
    expect(deleteOne).toHaveBeenCalledTimes(2);
  });

  it('stops when the request is no longer allowed', async () => {
    const deleteOne = vi.fn().mockRejectedValue(refusal('Not authenticated', undefined, 401));
    const report = await deletePrinterFilesOneByOne(['/a.3mf', '/b.3mf'], deleteOne);
    expect(report.notAttempted).toEqual(['/a.3mf', '/b.3mf']);
    expect(deleteOne).toHaveBeenCalledTimes(1);
  });

  it('decides by the code and the status, never by the words', () => {
    expect(stopsTheBatch({ code: 'printer_state_unknown' })).toBe(true);
    expect(stopsTheBatch({ status: 403 })).toBe(true);
    expect(stopsTheBatch({ code: 'active_print', status: 409 })).toBe(false);
    expect(stopsTheBatch({ code: 'expected_print', status: 409 })).toBe(false);
    expect(stopsTheBatch({ message: 'printer_state_unknown', status: 500 })).toBe(false);
  });
});
