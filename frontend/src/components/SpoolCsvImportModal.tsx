import { useState, useRef, type DragEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { Upload, X, FileText, Loader2, CheckCircle, XCircle, MinusCircle, Wand2, AlertTriangle, Copy } from 'lucide-react';
import { api, type CsvExportOptions, type CsvImportOptions, type CsvImportPreview, type CsvImportRow } from '../api/client';
import { getSwatchStyle } from '../utils/colors';
import { Button } from './Button';

// The chosen locale knobs outlive the dialog — the same spreadsheet produces
// the next file too.
const IMPORT_OPTS_KEY = 'bamdude-csv-import-options';
const EXPORT_OPTS_KEY = 'bamdude-csv-export-options';

function loadOpts<T>(storageKey: string, fallback: T): T {
  try {
    const stored = localStorage.getItem(storageKey);
    if (stored) return { ...fallback, ...(JSON.parse(stored) as Partial<T>) };
  } catch { /* ignore */ }
  return fallback;
}

function saveOpts(storageKey: string, value: unknown) {
  try {
    localStorage.setItem(storageKey, JSON.stringify(value));
  } catch { /* ignore */ }
}

function OptionSelect({ label, value, choices, onChange }: {
  label: string;
  value: string;
  choices: { value: string; label: string }[];
  onChange: (v: string) => void;
}) {
  return (
    <label className="flex flex-col gap-1 text-xs text-bambu-gray min-w-[9rem] flex-1">
      {label}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="px-2 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded text-sm text-white focus:outline-none focus:border-bambu-green"
      >
        {choices.map((c) => (
          <option key={c.value} value={c.value}>{c.label}</option>
        ))}
      </select>
    </label>
  );
}

interface SpoolCsvImportModalProps {
  onClose: () => void;
  /** Called after a successful import so the page can refetch the inventory. */
  onImported: (created: number) => void;
}

/**
 * CSV import flow (#1576): pick a file → backend dry-run preview (per-row
 * valid/error/skipped, colours resolved) → user reviews → confirm imports only
 * the valid rows. Nothing is written until confirm.
 */
export function SpoolCsvImportModal({ onClose, onImported }: SpoolCsvImportModalProps) {
  const { t } = useTranslation();
  const [file, setFile] = useState<File | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [preview, setPreview] = useState<CsvImportPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [options, setOptions] = useState<CsvImportOptions>(() =>
    loadOpts<CsvImportOptions>(IMPORT_OPTS_KEY, { encoding: 'auto', delimiter: 'auto', decimal: 'auto' }));
  const fileInputRef = useRef<HTMLInputElement>(null);

  const loadPreview = async (selected: File, opts: CsvImportOptions = options) => {
    setFile(selected);
    setPreview(null);
    setError(null);
    setLoading(true);
    try {
      const result = await api.importSpoolsCsvPreview(selected, opts);
      setPreview(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : t('inventory.csv.previewError', 'Could not read the CSV file'));
    } finally {
      setLoading(false);
    }
  };

  // Changing a knob with a file already picked re-runs the preview under the
  // new reading — the counts answer "did that fix it?" immediately.
  const setOption = (patch: Partial<CsvImportOptions>) => {
    const next = { ...options, ...patch };
    setOptions(next);
    saveOpts(IMPORT_OPTS_KEY, next);
    if (file) loadPreview(file, next);
  };

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const selected = e.target.files?.[0];
    if (selected) loadPreview(selected);
  };

  const handleDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(false);
    const dropped = e.dataTransfer.files?.[0];
    if (dropped) loadPreview(dropped);
  };

  const handleImport = async () => {
    if (!file) return;
    setImporting(true);
    setError(null);
    try {
      const result = await api.importSpoolsCsv(file, options);
      onImported(result.created);
    } catch (err) {
      setError(err instanceof Error ? err.message : t('inventory.csv.importError', 'Import failed'));
      setImporting(false);
    }
  };

  const statusIcon = (status: CsvImportRow['status']) => {
    if (status === 'valid') return <CheckCircle className="w-4 h-4 text-green-500 flex-shrink-0" />;
    if (status === 'error') return <XCircle className="w-4 h-4 text-red-500 flex-shrink-0" />;
    return <MinusCircle className="w-4 h-4 text-bambu-gray flex-shrink-0" />;
  };

  const validCount = preview?.valid_count ?? 0;

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4">
      <div className="bg-bambu-dark-secondary rounded-lg w-full max-w-3xl border border-bambu-dark-tertiary flex flex-col max-h-[90vh]">
        <div className="p-4 border-b border-bambu-dark-tertiary flex items-center justify-between">
          <h2 className="text-lg font-semibold text-white">{t('inventory.csv.modalTitle', 'Import spools from CSV')}</h2>
          <button onClick={onClose} className="p-1 hover:bg-bambu-dark rounded">
            <X className="w-5 h-5 text-bambu-gray" />
          </button>
        </div>

        <div className="p-4 space-y-4 overflow-y-auto flex-1">
          {/* Locale knobs — 'auto' by default; explicit choices for what auto
              cannot safely guess (legacy encodings, thousands separators). */}
          <div className="flex flex-wrap gap-3">
            <OptionSelect
              label={t('inventory.csv.optEncoding', 'Encoding')}
              value={options.encoding ?? 'auto'}
              choices={[
                { value: 'auto', label: t('inventory.csv.optAuto', 'Auto') },
                { value: 'utf-8', label: 'UTF-8' },
                { value: 'windows-1251', label: 'Windows-1251' },
                { value: 'windows-1252', label: 'Windows-1252' },
              ]}
              onChange={(v) => setOption({ encoding: v as CsvImportOptions['encoding'] })}
            />
            <OptionSelect
              label={t('inventory.csv.optDelimiter', 'Delimiter')}
              value={options.delimiter ?? 'auto'}
              choices={[
                { value: 'auto', label: t('inventory.csv.optAuto', 'Auto') },
                { value: 'comma', label: t('inventory.csv.optComma', 'Comma (,)') },
                { value: 'semicolon', label: t('inventory.csv.optSemicolon', 'Semicolon (;)') },
                { value: 'tab', label: t('inventory.csv.optTab', 'Tab') },
              ]}
              onChange={(v) => setOption({ delimiter: v as CsvImportOptions['delimiter'] })}
            />
            <OptionSelect
              label={t('inventory.csv.optDecimal', 'Decimal mark')}
              value={options.decimal ?? 'auto'}
              choices={[
                { value: 'auto', label: t('inventory.csv.optAuto', 'Auto') },
                { value: 'dot', label: t('inventory.csv.optDot', 'Dot (.)') },
                { value: 'comma', label: t('inventory.csv.optComma', 'Comma (,)') },
              ]}
              onChange={(v) => setOption({ decimal: v as CsvImportOptions['decimal'] })}
            />
          </div>

          {/* Drop zone / file picker */}
          <div
            onDragOver={(e) => {
              e.preventDefault();
              setIsDragging(true);
            }}
            onDragLeave={(e) => {
              e.preventDefault();
              setIsDragging(false);
            }}
            onDrop={handleDrop}
            onClick={() => fileInputRef.current?.click()}
            className={`border-2 border-dashed rounded-lg p-6 text-center cursor-pointer transition-colors ${
              isDragging
                ? 'border-bambu-green bg-bambu-green/10'
                : 'border-bambu-dark-tertiary hover:border-bambu-green/50'
            }`}
          >
            <Upload className={`w-9 h-9 mx-auto mb-2 ${isDragging ? 'text-bambu-green' : 'text-bambu-gray'}`} />
            {file ? (
              <p className="text-white font-medium flex items-center justify-center gap-2">
                <FileText className="w-4 h-4" /> {file.name}
              </p>
            ) : (
              <>
                <p className="text-white font-medium">{t('inventory.csv.selectFile', 'Choose a CSV file or drag it here')}</p>
                <p className="text-xs text-bambu-gray/70 mt-1">{t('inventory.csv.dragHint', 'Header: material (required), brand, subtype, color_name, rgba, …')}</p>
              </>
            )}
          </div>
          <input ref={fileInputRef} type="file" accept=".csv,text/csv" className="hidden" onChange={handleFileSelect} />

          {loading && (
            <div className="flex items-center justify-center gap-2 text-bambu-gray py-4">
              <Loader2 className="w-4 h-4 animate-spin" />
              {t('inventory.csv.parsing', 'Reading file…')}
            </div>
          )}

          {error && (
            <div className="p-3 bg-red-50 dark:bg-red-500/10 border border-red-300 dark:border-red-500/30 rounded-lg flex items-start gap-3">
              <XCircle className="w-5 h-5 text-red-600 dark:text-red-400 mt-0.5 flex-shrink-0" />
              <p className="text-sm text-red-700 dark:text-red-300 break-words">{error}</p>
            </div>
          )}

          {preview && (
            <>
              {/* Summary */}
              <div className="flex flex-wrap gap-3 text-sm">
                <span className="px-2 py-1 rounded bg-green-50 dark:bg-green-500/10 text-green-700 dark:text-green-400">
                  {t('inventory.csv.validCount', '{{count}} valid', { count: preview.valid_count })}
                </span>
                <span className="px-2 py-1 rounded bg-red-50 dark:bg-red-500/10 text-red-700 dark:text-red-400">
                  {t('inventory.csv.errorCount', '{{count}} error', { count: preview.error_count })}
                </span>
                <span className="px-2 py-1 rounded bg-bambu-dark text-bambu-gray">
                  {t('inventory.csv.skippedCount', '{{count}} skipped', { count: preview.skipped_count })}
                </span>
              </div>

              {preview.warnings.length > 0 && (
                <div className="p-3 bg-yellow-50 dark:bg-yellow-500/10 border border-yellow-300 dark:border-yellow-500/30 rounded-lg space-y-1">
                  {preview.warnings.map((w, i) => (
                    <p key={i} className="text-xs text-yellow-700 dark:text-yellow-300">{w}</p>
                  ))}
                </div>
              )}

              {/* Preview table */}
              {preview.rows.length > 0 && (
                <div className="border border-bambu-dark-tertiary rounded-lg overflow-hidden">
                  <div className="max-h-72 overflow-y-auto">
                    <table className="w-full text-sm">
                      <thead className="bg-bambu-dark sticky top-0">
                        <tr className="text-left text-bambu-gray">
                          <th className="px-3 py-2 font-medium">{t('inventory.csv.colRow', 'Row')}</th>
                          <th className="px-3 py-2 font-medium">{t('inventory.csv.colStatus', 'Status')}</th>
                          <th className="px-3 py-2 font-medium">{t('inventory.material', 'Material')}</th>
                          <th className="px-3 py-2 font-medium">{t('inventory.brand', 'Brand')}</th>
                          <th className="px-3 py-2 font-medium">{t('inventory.csv.colColor', 'Color')}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {preview.rows.map((row) => (
                          <tr key={row.row_number} className="border-t border-bambu-dark-tertiary">
                            <td className="px-3 py-2 text-bambu-gray">{row.row_number}</td>
                            <td className="px-3 py-2">
                              <div className="flex items-center gap-1.5">
                                {statusIcon(row.status)}
                                {row.status === 'error' && row.reason && (
                                  <span className="text-xs text-red-700 dark:text-red-400 break-words">{row.reason}</span>
                                )}
                              </div>
                            </td>
                            <td className="px-3 py-2 text-white">{row.material || '—'}</td>
                            <td className="px-3 py-2 text-white">{row.brand || '—'}</td>
                            <td className="px-3 py-2">
                              <div className="flex items-center gap-2">
                                {row.rgba && (
                                  <span
                                    className="inline-block w-4 h-4 rounded-full border border-bambu-dark-tertiary flex-shrink-0"
                                    style={getSwatchStyle(row.rgba)}
                                  />
                                )}
                                <span className="text-white">{row.color_name || '—'}</span>
                                {row.resolved_color && !row.cross_material_color && (
                                  <span title={t('inventory.csv.colorResolved', 'Color filled from catalog')}>
                                    <Wand2 className="w-3.5 h-3.5 text-bambu-green flex-shrink-0" />
                                  </span>
                                )}
                                {row.cross_material_color && (
                                  <span title={t('inventory.csv.colorCrossMaterial', 'Color taken from a different material — no exact match in catalog')}>
                                    <AlertTriangle className="w-3.5 h-3.5 text-yellow-500 flex-shrink-0" />
                                  </span>
                                )}
                                {row.duplicate_of_existing && (
                                  <span title={t('inventory.csv.duplicateExisting', 'A spool with this material, brand and color already exists — it will still be imported as a new spool')}>
                                    <Copy className="w-3.5 h-3.5 text-amber-600 dark:text-amber-400 flex-shrink-0" />
                                  </span>
                                )}
                              </div>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </>
          )}
        </div>

        <div className="p-4 border-t border-bambu-dark-tertiary flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose} disabled={importing}>
            {t('common.cancel')}
          </Button>
          <Button onClick={handleImport} disabled={!preview || validCount === 0 || importing}>
            {importing ? (
              <>
                <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                {t('inventory.csv.importing', 'Importing…')}
              </>
            ) : validCount > 0 ? (
              t('inventory.csv.importValidRows', 'Import {{count}} valid rows', { count: validCount })
            ) : (
              t('inventory.csv.noValidRows', 'No valid rows')
            )}
          </Button>
        </div>
      </div>
    </div>
  );
}


/**
 * Export options (#csv-locale-options): the same locale knobs as the import,
 * chosen BEFORE the download because the next stop is usually a spreadsheet —
 * a European locale wants ';' cells and ',' decimals, and Windows Excel needs
 * the BOM to read UTF-8 at all.
 */
export function SpoolCsvExportModal({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const [options, setOptions] = useState<CsvExportOptions>(() =>
    loadOpts<CsvExportOptions>(EXPORT_OPTS_KEY, { encoding: 'utf-8', delimiter: 'comma', decimal: 'dot' }));
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const setOption = (patch: Partial<CsvExportOptions>) => {
    const next = { ...options, ...patch };
    setOptions(next);
    saveOpts(EXPORT_OPTS_KEY, next);
  };

  const handleExport = async () => {
    setExporting(true);
    setError(null);
    try {
      await api.exportSpoolsCsv(options);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : t('inventory.csv.exportError', 'Export failed'));
      setExporting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4">
      <div className="bg-bambu-dark-secondary rounded-lg w-full max-w-md border border-bambu-dark-tertiary">
        <div className="p-4 border-b border-bambu-dark-tertiary flex items-center justify-between">
          <h2 className="text-lg font-semibold text-white">{t('inventory.csv.exportTitle', 'Export spools to CSV')}</h2>
          <button onClick={onClose} className="p-1 hover:bg-bambu-dark rounded">
            <X className="w-5 h-5 text-bambu-gray" />
          </button>
        </div>
        <div className="p-4 space-y-4">
          <p className="text-xs text-bambu-gray">
            {t('inventory.csv.exportHint', 'Match your spreadsheet: a European locale wants semicolons and comma decimals; Excel on Windows needs UTF-8 + BOM.')}
          </p>
          <div className="flex flex-wrap gap-3">
            <OptionSelect
              label={t('inventory.csv.optEncoding', 'Encoding')}
              value={options.encoding ?? 'utf-8'}
              choices={[
                { value: 'utf-8', label: 'UTF-8' },
                { value: 'utf-8-bom', label: t('inventory.csv.optUtf8Bom', 'UTF-8 + BOM (Excel)') },
              ]}
              onChange={(v) => setOption({ encoding: v as CsvExportOptions['encoding'] })}
            />
            <OptionSelect
              label={t('inventory.csv.optDelimiter', 'Delimiter')}
              value={options.delimiter ?? 'comma'}
              choices={[
                { value: 'comma', label: t('inventory.csv.optComma', 'Comma (,)') },
                { value: 'semicolon', label: t('inventory.csv.optSemicolon', 'Semicolon (;)') },
                { value: 'tab', label: t('inventory.csv.optTab', 'Tab') },
              ]}
              onChange={(v) => setOption({ delimiter: v as CsvExportOptions['delimiter'] })}
            />
            <OptionSelect
              label={t('inventory.csv.optDecimal', 'Decimal mark')}
              value={options.decimal ?? 'dot'}
              choices={[
                { value: 'dot', label: t('inventory.csv.optDot', 'Dot (.)') },
                { value: 'comma', label: t('inventory.csv.optComma', 'Comma (,)') },
              ]}
              onChange={(v) => setOption({ decimal: v as CsvExportOptions['decimal'] })}
            />
          </div>
          {error && <p className="text-sm text-red-700 dark:text-red-300 break-words">{error}</p>}
        </div>
        <div className="p-4 border-t border-bambu-dark-tertiary flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose} disabled={exporting}>
            {t('common.cancel')}
          </Button>
          <Button onClick={handleExport} disabled={exporting}>
            {exporting ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : null}
            {t('inventory.csv.exportButton', 'Export')}
          </Button>
        </div>
      </div>
    </div>
  );
}
