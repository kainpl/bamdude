import { useState, useEffect, useMemo, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { useSearchParams, Link } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Wrench,
  Loader2,
  Check,
  AlertTriangle,
  Clock,
  Plus,
  Trash2,
  ChevronDown,
  ChevronUp,
  Droplet,
  Flame,
  Ruler,
  Sparkles,
  Square,
  Cable,
  Edit3,
  RotateCcw,
  Calendar,
  Timer,
  Cog,
  Fan,
  Zap,
  Wind,
  Thermometer,
  Layers,
  Box,
  Target,
  RefreshCw,
  Settings,
  Filter,
  CircleDot,
  Printer,
  ExternalLink,
  History,
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
  Download,
  X,
} from 'lucide-react';
import { api, macrosApi, getAuthToken } from '../api/client';
import type { MaintenanceStatus, PrinterMaintenanceOverview, MaintenanceType, MaintenanceHistoryEntry, Permission, MacroMeta } from '../api/client';
import { Card, CardContent } from '../components/Card';
import { Button } from '../components/Button';
import { Toggle } from '../components/Toggle';
import { ConfirmModal } from '../components/ConfirmModal';
import { useToast } from '../contexts/ToastContext';
import { formatDateTime } from '../utils/date';
import { useAuth } from '../contexts/AuthContext';
import { getMaintenanceWikiUrl } from '../utils/maintenanceWikiUrls';

// Icon mapping for maintenance types
const iconMap: Record<string, React.ComponentType<{ className?: string }>> = {
  Droplet,
  Flame,
  Ruler,
  Sparkles,
  Square,
  Cable,
  Wrench,
  Calendar,
  Timer,
  Cog,
  Fan,
  Zap,
  Wind,
  Thermometer,
  Layers,
  Box,
  Target,
  RefreshCw,
  Settings,
  Filter,
  CircleDot,
};

function getIcon(iconName: string | null) {
  if (!iconName) return Wrench;
  return iconMap[iconName] || Wrench;
}

type TFunction = (key: string, options?: Record<string, unknown>) => string;

function formatDuration(value: number, type: 'hours' | 'days', t?: TFunction): string {
  if (type === 'days') {
    if (value < 1) return t ? t('common.today') : 'Today';
    if (value === 1) return t ? t('maintenance.day') : '1 day';
    if (value < 7) {
      const days = Math.round(value);
      return t ? t('maintenance.days', { count: days }) : `${days} days`;
    }
    // Show weeks for anything under 6 months for better precision
    if (value < 180) {
      const weeks = Math.round(value / 7);
      if (weeks === 1) return t ? t('maintenance.week') : '1 week';
      return t ? t('maintenance.weeks', { count: weeks }) : `${weeks} weeks`;
    }
    // 6+ months show as months
    const months = Math.round(value / 30);
    if (months === 1) return t ? t('maintenance.month') : '1 month';
    return t ? t('maintenance.months', { count: months }) : `${months} months`;
  } else {
    // Print hours - convert to readable units
    if (value < 1) return `${Math.round(value * 60)}m`;
    if (value < 24) return `${value < 10 ? value.toFixed(1) : Math.round(value)}h`;
    // 24+ hours: show as days of print time
    const days = value / 24;
    if (days < 7) return `${days < 2 ? days.toFixed(1) : Math.round(days)}d`;
    // 7+ days: show as weeks of print time
    const weeks = days / 7;
    if (weeks < 12) return `${weeks < 2 ? weeks.toFixed(1) : Math.round(weeks)}w`;
    // 12+ weeks: show as months of print time
    return `${Math.round(weeks / 4)}mo`;
  }
}

function formatIntervalLabel(value: number, type: 'hours' | 'days', t?: TFunction): string {
  if (type === 'days') {
    if (value === 1) return t ? t('maintenance.day') : '1 day';
    if (value === 7) return t ? t('maintenance.week') : '1 week';
    if (value === 14) return t ? t('maintenance.weeks', { count: 2 }) : '2 weeks';
    if (value === 30) return t ? t('maintenance.month') : '1 month';
    if (value === 60) return t ? t('maintenance.months', { count: 2 }) : '2 months';
    if (value === 90) return t ? t('maintenance.months', { count: 3 }) : '3 months';
    if (value === 180) return t ? t('maintenance.months', { count: 6 }) : '6 months';
    if (value === 365) return t ? t('maintenance.year') : '1 year';
    return t ? t('maintenance.days', { count: value }) : `${value} days`;
  }
  return `${value}h`;
}

// Maintenance item card - cleaner, more visual design
function MaintenanceCard({
  item,
  types,
  macroMeta,
  onPerform,
  onToggle,
  hasPermission,
  t,
}: {
  item: MaintenanceStatus;
  types: MaintenanceType[];
  macroMeta: MacroMeta | undefined;
  onPerform: (id: number) => void;
  onToggle: (id: number, enabled: boolean) => void;
  hasPermission: (permission: Permission) => boolean;
  t: TFunction;
}) {
  const Icon = getIcon(item.maintenance_type_icon);
  const intervalType = item.interval_type || 'hours';
  const typeInfo = types.find(t => t.id === item.maintenance_type_id);
  const printerModels = typeInfo?.printer_models;
  const hasModelFilter = printerModels && !(printerModels.length === 1 && printerModels[0] === '*');

  // Calculate progress based on interval type
  const getProgress = () => {
    if (intervalType === 'days') {
      const daysSince = item.days_since_maintenance ?? 0;
      return Math.max(0, Math.min(100, (daysSince / item.interval_hours) * 100));
    }
    return Math.max(0, Math.min(100,
      ((item.interval_hours - item.hours_until_due) / item.interval_hours) * 100
    ));
  };

  const progressPercent = getProgress();

  const getStatusColor = () => {
    if (!item.enabled) return 'text-bambu-gray';
    if (item.is_due) return 'text-red-400';
    if (item.is_warning) return 'text-amber-400';
    return 'text-bambu-green';
  };

  const getProgressColor = () => {
    if (!item.enabled) return 'bg-bambu-gray/30';
    if (item.is_due) return 'bg-red-500';
    if (item.is_warning) return 'bg-amber-500';
    return 'bg-bambu-green';
  };

  const getBgColor = () => {
    if (!item.enabled) return 'bg-bambu-dark-secondary/50';
    if (item.is_due) return 'bg-red-500/5 border-red-500/20';
    if (item.is_warning) return 'bg-amber-500/5 border-amber-500/20';
    return 'bg-bambu-dark-secondary border-bambu-dark-tertiary';
  };

  const getStatusText = () => {
    if (!item.enabled) return t('common.disabled');

    if (intervalType === 'days') {
      const daysUntil = item.days_until_due ?? 0;
      if (item.is_due) return t('maintenance.overdueBy', { duration: formatDuration(Math.abs(daysUntil), 'days', t) });
      if (item.is_warning) return t('maintenance.dueIn', { duration: formatDuration(daysUntil, 'days', t) });
      return t('maintenance.timeLeft', { duration: formatDuration(daysUntil, 'days', t) });
    } else {
      if (item.is_due) return t('maintenance.overdueBy', { duration: formatDuration(Math.abs(item.hours_until_due), 'hours', t) });
      if (item.is_warning) return t('maintenance.dueIn', { duration: formatDuration(item.hours_until_due, 'hours', t) });
      return t('maintenance.timeLeft', { duration: formatDuration(item.hours_until_due, 'hours', t) });
    }
  };

  return (
    <div className={`rounded-xl border p-4 transition-all ${getBgColor()}`}>
      <div className="flex items-start gap-3 max-[550px]:flex-wrap">
        {/* Icon with status indicator */}
        <div className={`relative p-2.5 rounded-lg shrink-0 ${
          item.is_due ? 'bg-red-500/20' :
          item.is_warning ? 'bg-amber-500/20' :
          item.enabled ? 'bg-bambu-dark' : 'bg-bambu-dark/50'
        }`}>
          <Icon className={`w-5 h-5 ${getStatusColor()}`} />
          {item.enabled && (item.is_due || item.is_warning) && (
            <span className={`absolute -top-1 -right-1 w-2.5 h-2.5 rounded-full ${
              item.is_due ? 'bg-red-500' : 'bg-amber-500'
            } animate-pulse`} />
          )}
        </div>

        {/* Content */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <h3 className={`font-medium truncate ${item.enabled ? 'text-white' : 'text-bambu-gray'}`}>
              {item.maintenance_type_name}
            </h3>
            {intervalType === 'days' && (
              <span title={t('maintenance.timeBasedInterval')}>
                <Calendar className="w-3.5 h-3.5 text-bambu-gray shrink-0" />
              </span>
            )}
            {/* Wiki link - next to name */}
            {(() => {
              // Use custom wiki_url from type if available, otherwise use computed URL
              const wikiUrl = item.maintenance_type_wiki_url || getMaintenanceWikiUrl(item.maintenance_type_code, item.printer_model);
              return wikiUrl ? (
                <a
                  href={wikiUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-bambu-gray hover:text-bambu-green transition-colors shrink-0"
                  title={t('maintenance.viewDocumentation')}
                  onClick={(e) => e.stopPropagation()}
                >
                  <ExternalLink className="w-3.5 h-3.5" />
                </a>
              ) : null;
            })()}
            {hasModelFilter && (
              <span className="px-1.5 py-0.5 bg-bambu-dark-tertiary text-bambu-gray text-[10px] rounded shrink-0">
                {printerModels.map(code => macroMeta?.printer_models?.[code] || code).join(', ')}
              </span>
            )}
          </div>

          {/* Progress bar */}
          <div className="mt-2 mb-1.5">
            <div className="w-full h-1.5 bg-bambu-dark rounded-full overflow-hidden">
              <div
                className={`h-full rounded-full transition-all duration-500 ${getProgressColor()}`}
                style={{ width: `${progressPercent}%` }}
              />
            </div>
          </div>

          {/* Status text */}
          <div className={`text-xs flex items-center gap-1 ${getStatusColor()}`}>
            {item.is_due && <AlertTriangle className="w-3 h-3" />}
            {item.is_warning && !item.is_due && <Clock className="w-3 h-3" />}
            {!item.is_due && !item.is_warning && item.enabled && <Check className="w-3 h-3" />}
            {getStatusText()}
          </div>
        </div>

        {/* Actions */}
        <div className="flex items-center gap-2 shrink-0 max-[550px]:w-full max-[550px]:justify-end max-[550px]:mt-1">
          <span title={!hasPermission('maintenance:update') ? t('maintenance.noPermissionUpdate') : undefined}>
            <Toggle
              checked={item.enabled}
              onChange={(checked) => onToggle(item.id, checked)}
              disabled={!hasPermission('maintenance:update')}
            />
          </span>
          <Button
            size="sm"
            variant={item.is_due ? 'primary' : 'secondary'}
            onClick={() => onPerform(item.id)}
            disabled={!item.enabled || !hasPermission('maintenance:update')}
            title={!hasPermission('maintenance:update') ? t('maintenance.noPermissionPerform') : undefined}
            className="!px-3"
          >
            <RotateCcw className="w-3.5 h-3.5" />
            {t('common.reset')}
          </Button>
        </div>
      </div>
    </div>
  );
}

// Printer section with improved visual hierarchy
function PrinterSection({
  overview,
  types,
  macroMeta,
  onPerform,
  onToggle,
  onSetHours,
  hasPermission,
  t,
}: {
  overview: PrinterMaintenanceOverview;
  types: MaintenanceType[];
  macroMeta: MacroMeta | undefined;
  onPerform: (id: number) => void;
  onToggle: (id: number, enabled: boolean) => void;
  onSetHours: (printerId: number, hours: number) => void;
  hasPermission: (permission: Permission) => boolean;
  t: TFunction;
}) {
  const [expanded, setExpanded] = useState(false);
  const [editingHours, setEditingHours] = useState(false);
  const [hoursInput, setHoursInput] = useState(overview.total_print_hours.toFixed(1));

  const sortedItems = [...overview.maintenance_items].sort((a, b) => {
    // Sort by urgency first, then by type
    if (a.is_due && !b.is_due) return -1;
    if (!a.is_due && b.is_due) return 1;
    if (a.is_warning && !b.is_warning) return -1;
    if (!a.is_warning && b.is_warning) return 1;
    return a.maintenance_type_id - b.maintenance_type_id;
  });

  const nextTask = sortedItems.find(item => item.enabled && (item.is_due || item.is_warning));

  const handleSaveHours = () => {
    const hours = parseFloat(hoursInput);
    if (!isNaN(hours) && hours >= 0) {
      onSetHours(overview.printer_id, hours);
      setEditingHours(false);
    }
  };

  return (
    <Card className="overflow-hidden">
      {/* Header */}
      <div className="p-5">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            <h2 className="text-xl font-semibold text-white">{overview.printer_name}</h2>
            <Link to={`/maintenance?printer=${overview.printer_id}`} className="p-1 rounded hover:bg-bambu-dark-tertiary text-bambu-gray hover:text-white transition-colors" title={t('maintenance.viewHistory')}>
              <History className="w-4 h-4" />
            </Link>
            <div className="flex items-center gap-2">
              {overview.due_count > 0 && (
                <span className="px-2.5 py-1 bg-red-500/20 text-red-400 text-xs font-medium rounded-full flex items-center gap-1.5">
                  <AlertTriangle className="w-3 h-3" />
                  {t('maintenance.overdueCount', { count: overview.due_count })}
                </span>
              )}
              {overview.warning_count > 0 && (
                <span className="px-2.5 py-1 bg-amber-500/20 text-amber-400 text-xs font-medium rounded-full flex items-center gap-1.5">
                  <Clock className="w-3 h-3" />
                  {t('maintenance.dueSoonCount', { count: overview.warning_count })}
                </span>
              )}
              {overview.due_count === 0 && overview.warning_count === 0 && (
                <span className="px-2.5 py-1 bg-bambu-green/20 text-bambu-green text-xs font-medium rounded-full flex items-center gap-1.5">
                  <Check className="w-3 h-3" />
                  {t('maintenance.allGood')}
                </span>
              )}
            </div>
          </div>
          <button
            onClick={() => setExpanded(!expanded)}
            className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-bambu-gray hover:text-white hover:bg-bambu-dark rounded-lg transition-colors"
          >
            {expanded ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
            {expanded ? t('common.collapse') : t('common.expand')}
          </button>
        </div>

        {/* Quick stats row */}
        <div className="flex items-center gap-6 mt-4">
          {/* Print Hours */}
          <div className="flex items-center gap-3">
            <div className="p-2 bg-bambu-dark/50 rounded-lg">
              <Timer className="w-4 h-4 text-bambu-gray" />
            </div>
            {editingHours ? (
              <div className="flex items-center gap-2">
                <input
                  type="number"
                  value={hoursInput}
                  onChange={(e) => setHoursInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') handleSaveHours();
                    if (e.key === 'Escape') setEditingHours(false);
                  }}
                  className="w-24 px-2 py-1 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-sm"
                  min="0"
                  step="1"
                  autoFocus
                />
                <span className="text-xs text-bambu-gray">{t('common.hours')}</span>
                <Button size="sm" onClick={handleSaveHours}>{t('common.save')}</Button>
                <Button size="sm" variant="secondary" onClick={() => setEditingHours(false)}>{t('common.cancel')}</Button>
              </div>
            ) : (
              <button
                onClick={() => {
                  if (!hasPermission('maintenance:update')) return;
                  setHoursInput(Math.round(overview.total_print_hours).toString());
                  setEditingHours(true);
                }}
                className={`group ${!hasPermission('maintenance:update') ? 'cursor-not-allowed opacity-60' : ''}`}
                title={!hasPermission('maintenance:update') ? t('maintenance.noPermissionEditHours') : undefined}
              >
                <div className={`text-sm font-medium text-white ${hasPermission('maintenance:update') ? 'group-hover:text-bambu-green' : ''} transition-colors flex items-center gap-1`}>
                  {Math.round(overview.total_print_hours)} {t('common.hours')}
                  <Edit3 className={`w-3 h-3 text-bambu-gray ${hasPermission('maintenance:update') ? 'group-hover:text-bambu-green' : ''}`} />
                </div>
                <div className="text-xs text-bambu-gray">{t('maintenance.totalPrintTime')}</div>
              </button>
            )}
          </div>

          {/* Divider */}
          <div className="w-px h-10 bg-bambu-dark-tertiary" />

          {/* Next Maintenance */}
          {nextTask && (
            <div className="flex items-center gap-3">
              <div className={`p-2 rounded-lg ${
                nextTask.is_due ? 'bg-red-500/20' : 'bg-amber-500/20'
              }`}>
                {(() => {
                  const Icon = getIcon(nextTask.maintenance_type_icon);
                  return <Icon className={`w-4 h-4 ${nextTask.is_due ? 'text-red-400' : 'text-amber-400'}`} />;
                })()}
              </div>
              <div>
                <div className={`text-sm font-medium ${nextTask.is_due ? 'text-red-400' : 'text-amber-400'}`}>
                  {nextTask.maintenance_type_name}
                </div>
                <div className={`text-xs ${nextTask.is_due ? 'text-red-400/70' : 'text-amber-400/70'}`}>
                  {nextTask.is_due ? t('common.overdue') : t('maintenance.dueSoon')}
                </div>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Maintenance items */}
      {expanded && (
        <CardContent className="pt-0 border-t border-bambu-dark-tertiary">
          <div className="space-y-3 pt-4">
            {sortedItems.map((item) => (
              <MaintenanceCard
                key={item.id}
                item={item}
                types={types}
                macroMeta={macroMeta}
                onPerform={onPerform}
                onToggle={onToggle}
                hasPermission={hasPermission}
                t={t}
              />
            ))}
          </div>
        </CardContent>
      )}
    </Card>
  );
}

// Settings section - maintenance types configuration
function SettingsSection({
  overview,
  types,
  macroMeta,
  onUpdateInterval,
  onAddType,
  onUpdateType,
  onDeleteType,
  onRestoreDefaults,
  isRestoringDefaults,
  onAssignType,
  onRemoveItem,
  hasPermission,
  t,
}: {
  overview: PrinterMaintenanceOverview[] | undefined;
  types: MaintenanceType[];
  macroMeta: MacroMeta | undefined;
  onUpdateInterval: (id: number, data: { custom_interval_hours?: number | null; custom_interval_type?: 'hours' | 'days' | null }) => void;
  onAddType: (data: { name: string; description?: string; default_interval_hours: number; interval_type: 'hours' | 'days'; icon?: string; wiki_url?: string | null; printer_models?: string[] }, printerIds: number[]) => void;
  onUpdateType: (id: number, data: { name?: string; default_interval_hours?: number; interval_type?: 'hours' | 'days'; icon?: string; wiki_url?: string | null; printer_models?: string[] }) => void;
  onDeleteType: (id: number) => void;
  onRestoreDefaults: () => void;
  isRestoringDefaults: boolean;
  onAssignType: (printerId: number, typeId: number) => void;
  onRemoveItem: (itemId: number) => void;
  hasPermission: (permission: Permission) => boolean;
  t: TFunction;
}) {
  const [editingInterval, setEditingInterval] = useState<number | null>(null);
  const [intervalInput, setIntervalInput] = useState('');
  const [intervalTypeInput, setIntervalTypeInput] = useState<'hours' | 'days'>('hours');
  const [showAddType, setShowAddType] = useState(false);
  const [newTypeName, setNewTypeName] = useState('');
  const [newTypeInterval, setNewTypeInterval] = useState('100');
  const [newTypeIntervalType, setNewTypeIntervalType] = useState<'hours' | 'days'>('hours');
  const [newTypeIcon, setNewTypeIcon] = useState('Wrench');
  const [newTypeWikiUrl, setNewTypeWikiUrl] = useState('');
  const [newTypePrinterModels, setNewTypePrinterModels] = useState<string[]>(['*']);
  const [selectedPrinters, setSelectedPrinters] = useState<Set<number>>(new Set());
  const [expandedType, setExpandedType] = useState<number | null>(null);
  const [pendingSystemDelete, setPendingSystemDelete] = useState<MaintenanceType | null>(null);

  // Get unique printers from overview
  const printers = useMemo(() => {
    if (!overview) return [];
    return overview.map(o => ({ id: o.printer_id, name: o.printer_name }));
  }, [overview]);

  // Get which printers have a specific maintenance type assigned
  const getAssignedPrinters = (typeId: number) => {
    if (!overview) return [];
    return overview
      .filter(p => p.maintenance_items.some(item => item.maintenance_type_id === typeId))
      .map(p => ({
        printerId: p.printer_id,
        printerName: p.printer_name,
        itemId: p.maintenance_items.find(item => item.maintenance_type_id === typeId)?.id,
      }));
  };

  // Get printers that DON'T have a specific type assigned
  const getUnassignedPrinters = (typeId: number) => {
    if (!overview) return [];
    const assignedIds = new Set(getAssignedPrinters(typeId).map(p => p.printerId));
    return printers.filter(p => !assignedIds.has(p.id));
  };

  // Edit type state
  const [editingType, setEditingType] = useState<MaintenanceType | null>(null);
  const [editTypeName, setEditTypeName] = useState('');
  const [editTypeInterval, setEditTypeInterval] = useState('');
  const [editTypeIntervalType, setEditTypeIntervalType] = useState<'hours' | 'days'>('hours');
  const [editTypeIcon, setEditTypeIcon] = useState('Wrench');
  const [editTypeWikiUrl, setEditTypeWikiUrl] = useState('');
  const [editTypePrinterModels, setEditTypePrinterModels] = useState<string[]>(['*']);

  const startEditType = (type: MaintenanceType) => {
    setEditingType(type);
    setEditTypeName(type.name);
    setEditTypeInterval(type.default_interval_hours.toString());
    setEditTypeIntervalType(type.interval_type || 'hours');
    setEditTypeIcon(type.icon || 'Wrench');
    setEditTypeWikiUrl(type.wiki_url || '');
    setEditTypePrinterModels(type.printer_models || ['*']);
  };

  const handleSaveEditType = () => {
    if (editingType && editTypeName.trim() && parseFloat(editTypeInterval) > 0) {
      onUpdateType(editingType.id, {
        name: editTypeName.trim(),
        default_interval_hours: parseFloat(editTypeInterval),
        interval_type: editTypeIntervalType,
        icon: editTypeIcon,
        wiki_url: editTypeWikiUrl.trim() || null,
        printer_models: editTypePrinterModels,
      });
      setEditingType(null);
    }
  };

  const handleSaveInterval = (itemId: number, defaultInterval: number, defaultIntervalType: 'hours' | 'days') => {
    const newInterval = parseFloat(intervalInput);
    if (!isNaN(newInterval) && newInterval > 0) {
      const customInterval = Math.abs(newInterval - defaultInterval) < 0.01 ? null : newInterval;
      const customIntervalType = intervalTypeInput !== defaultIntervalType ? intervalTypeInput : null;
      onUpdateInterval(itemId, {
        custom_interval_hours: customInterval,
        custom_interval_type: customIntervalType
      });
    }
    setEditingInterval(null);
  };

  const handleAddType = (e: React.FormEvent) => {
    e.preventDefault();
    if (newTypeName.trim() && parseFloat(newTypeInterval) > 0 && selectedPrinters.size > 0) {
      onAddType({
        name: newTypeName.trim(),
        default_interval_hours: parseFloat(newTypeInterval),
        interval_type: newTypeIntervalType,
        icon: newTypeIcon,
        wiki_url: newTypeWikiUrl.trim() || null,
        printer_models: newTypePrinterModels,
      }, Array.from(selectedPrinters));
      setNewTypeName('');
      setNewTypeInterval('100');
      setNewTypeIntervalType('hours');
      setNewTypeWikiUrl('');
      setNewTypePrinterModels(['*']);
      setSelectedPrinters(new Set());
      setShowAddType(false);
    }
  };

  const togglePrinterSelection = (printerId: number) => {
    setSelectedPrinters(prev => {
      const next = new Set(prev);
      if (next.has(printerId)) {
        next.delete(printerId);
      } else {
        next.add(printerId);
      }
      return next;
    });
  };

  const printerItems = overview?.map(p => ({
    printerId: p.printer_id,
    printerName: p.printer_name,
    items: p.maintenance_items.sort((a, b) => a.maintenance_type_id - b.maintenance_type_id),
  })).sort((a, b) => a.printerName.localeCompare(b.printerName)) || [];

  const systemTypes = types.filter(t => t.is_system);
  const customTypes = types.filter(t => !t.is_system);

  return (
    <div className="space-y-4">
      {/* Maintenance Types */}
      <div>
        <div className="flex items-center justify-between mb-4">
          <div>
            <h2 className="text-lg font-semibold text-white">{t('maintenance.maintenanceTypes')}</h2>
            <p className="text-sm text-bambu-gray">{t('maintenance.maintenanceTypesDescription')}</p>
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="secondary"
              onClick={onRestoreDefaults}
              disabled={!hasPermission('maintenance:delete') || isRestoringDefaults}
              title={!hasPermission('maintenance:delete') ? t('maintenance.noPermissionDeleteTypes') : undefined}
            >
              {t('maintenance.restoreDefaults')}
            </Button>
            <Button
              onClick={() => setShowAddType(!showAddType)}
              disabled={!hasPermission('maintenance:create')}
              title={!hasPermission('maintenance:create') ? t('maintenance.noPermissionEditTypes') : undefined}
            >
              <Plus className="w-4 h-4" />
              {t('maintenance.addCustomType')}
            </Button>
          </div>
        </div>

        {/* Add custom type form */}
        {showAddType && (
          <Card className="mb-6">
            <CardContent className="py-4">
              <form onSubmit={handleAddType}>
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
                  <div className="lg:col-span-2">
                    <label className="block text-xs text-bambu-gray mb-1.5">{t('common.name')}</label>
                    <input
                      type="text"
                      value={newTypeName}
                      onChange={(e) => setNewTypeName(e.target.value)}
                      className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:outline-none"
                      placeholder={t('maintenance.exampleName')}
                      autoFocus
                    />
                  </div>
                  <div>
                    <label className="block text-xs text-bambu-gray mb-1.5">{t('maintenance.intervalType')}</label>
                    <select
                      value={newTypeIntervalType}
                      onChange={(e) => {
                        setNewTypeIntervalType(e.target.value as 'hours' | 'days');
                        // Set sensible default based on type
                        if (e.target.value === 'days') {
                          setNewTypeInterval('30');
                        } else {
                          setNewTypeInterval('100');
                        }
                      }}
                      className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:outline-none"
                    >
                      <option value="hours">{t('maintenance.printHours')}</option>
                      <option value="days">{t('maintenance.calendarDays')}</option>
                    </select>
                  </div>
                  <div>
                    <label className="block text-xs text-bambu-gray mb-1.5">
                      {t('maintenance.intervalValue', { type: newTypeIntervalType === 'days' ? t('maintenance.calendarDays').toLowerCase() : t('common.hours') })}
                    </label>
                    <input
                      type="number"
                      value={newTypeInterval}
                      onChange={(e) => setNewTypeInterval(e.target.value)}
                      className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:outline-none"
                      min="1"
                    />
                  </div>
                </div>
                <div className="mt-4 flex items-end justify-between">
                  <div>
                    <label className="block text-xs text-bambu-gray mb-1.5">{t('maintenance.icon')}</label>
                    <div className="flex gap-1">
                      {Object.keys(iconMap).map((iconName) => {
                        const IconComp = iconMap[iconName];
                        return (
                          <button
                            key={iconName}
                            type="button"
                            onClick={() => setNewTypeIcon(iconName)}
                            className={`p-2 rounded-lg transition-colors ${
                              newTypeIcon === iconName
                                ? 'bg-bambu-green text-white'
                                : 'bg-bambu-dark text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary'
                            }`}
                          >
                            <IconComp className="w-4 h-4" />
                          </button>
                        );
                      })}
                    </div>
                  </div>
                </div>
                {/* Wiki URL */}
                <div className="mt-4">
                  <label className="block text-xs text-bambu-gray mb-1.5">{t('maintenance.documentationLink')}</label>
                  <input
                    type="url"
                    value={newTypeWikiUrl}
                    onChange={(e) => setNewTypeWikiUrl(e.target.value)}
                    className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:outline-none"
                    placeholder="https://wiki.bambulab.com/..."
                  />
                </div>
                {/* Printer models */}
                <div className="mt-4">
                  <label className="block text-xs text-bambu-gray mb-1.5">{t('maintenance.printerModels')}</label>
                  <div className="flex flex-wrap gap-1 mb-1.5">
                    {newTypePrinterModels.map(code => (
                      <span key={code} className="text-xs px-1.5 py-0.5 bg-bambu-dark-tertiary text-white rounded flex items-center gap-1">
                        {code === '*' ? t('maintenance.allModels') : (macroMeta?.printer_models?.[code] || code)}
                        <button
                          type="button"
                          onClick={() => setNewTypePrinterModels(prev => prev.filter(m => m !== code))}
                          className="hover:text-red-400 transition-colors"
                        >
                          <X className="w-3 h-3" />
                        </button>
                      </span>
                    ))}
                  </div>
                  <select
                    value=""
                    onChange={(e) => {
                      const val = e.target.value;
                      if (!val) return;
                      if (val === '*') {
                        setNewTypePrinterModels(['*']);
                      } else {
                        setNewTypePrinterModels(prev => {
                          const without_wildcard = prev.filter(m => m !== '*');
                          if (without_wildcard.includes(val)) return prev;
                          return [...without_wildcard, val];
                        });
                      }
                    }}
                    className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:outline-none"
                  >
                    <option value="">{t('maintenance.addModel')}</option>
                    <option value="*" disabled={newTypePrinterModels.includes('*')}>{t('maintenance.allModels')}</option>
                    {macroMeta?.printer_models && Object.entries(macroMeta.printer_models).filter(([code]) => code !== '*').map(([code, name]) => (
                      <option key={code} value={code} disabled={newTypePrinterModels.includes(code) || newTypePrinterModels.includes('*')}>
                        {name}
                      </option>
                    ))}
                  </select>
                </div>
                {/* Printer selection */}
                <div className="mt-4">
                  <label className="block text-xs text-bambu-gray mb-1.5">{t('maintenance.assignToPrinters')}</label>
                  <div className="flex flex-wrap gap-2">
                    {printers.map(p => (
                      <button
                        key={p.id}
                        type="button"
                        onClick={() => togglePrinterSelection(p.id)}
                        className={`px-3 py-1.5 rounded-lg text-sm transition-colors ${
                          selectedPrinters.has(p.id)
                            ? 'bg-bambu-green text-white'
                            : 'bg-bambu-dark text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary'
                        }`}
                      >
                        {p.name}
                      </button>
                    ))}
                  </div>
                  {selectedPrinters.size === 0 && (
                    <p className="text-xs text-orange-400 mt-1">{t('maintenance.selectAtLeastOnePrinter')}</p>
                  )}
                </div>
                <div className="mt-4 flex justify-end gap-2">
                  <Button type="button" variant="secondary" onClick={() => { setShowAddType(false); setSelectedPrinters(new Set()); }}>
                    {t('common.cancel')}
                  </Button>
                  <Button type="submit" disabled={!newTypeName.trim() || selectedPrinters.size === 0}>
                    {t('maintenance.addType')}
                  </Button>
                </div>
              </form>
            </CardContent>
          </Card>
        )}

        {/* Types grid */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          {/* System types */}
          {systemTypes.map((type) => {
            const Icon = getIcon(type.icon);
            const intervalType = type.interval_type || 'hours';
            return (
              <div key={type.id} className="bg-bambu-dark-secondary rounded-xl p-4 border border-bambu-dark-tertiary">
                <div className="flex items-center gap-3">
                  <div className="p-2.5 bg-bambu-dark rounded-lg">
                    <Icon className="w-5 h-5 text-bambu-gray" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium text-white truncate">{type.name}</div>
                    <div className="text-xs text-bambu-gray mt-0.5 flex items-center gap-1">
                      {intervalType === 'days' ? <Calendar className="w-3 h-3" /> : <Timer className="w-3 h-3" />}
                      {formatIntervalLabel(type.default_interval_hours, intervalType, t)}
                    </div>
                    {type.printer_models && !(type.printer_models.length === 1 && type.printer_models[0] === '*') && (
                      <div className="text-[10px] text-bambu-gray mt-0.5">
                        {type.printer_models.map(code => macroMeta?.printer_models?.[code] || code).join(', ')}
                      </div>
                    )}
                  </div>
                  <button
                    onClick={() => {
                      if (!hasPermission('maintenance:delete')) return;
                      setPendingSystemDelete(type);
                    }}
                    disabled={!hasPermission('maintenance:delete')}
                    title={!hasPermission('maintenance:delete') ? t('maintenance.noPermissionDeleteTypes') : undefined}
                    className={`p-2 rounded-lg hover:bg-bambu-dark text-bambu-gray hover:text-red-400 transition-colors ${!hasPermission('maintenance:delete') ? 'opacity-50 cursor-not-allowed' : ''}`}
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>
              </div>
            );
          })}
          {/* Custom types */}
          {customTypes.map((type) => {
            const Icon = getIcon(type.icon);
            const intervalType = type.interval_type || 'hours';
            const isEditing = editingType?.id === type.id;

            if (isEditing) {
              return (
                <div key={type.id} className="bg-bambu-dark-secondary rounded-xl p-4 border border-bambu-green">
                  <div className="space-y-3">
                    <input
                      type="text"
                      value={editTypeName}
                      onChange={(e) => setEditTypeName(e.target.value)}
                      className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:outline-none"
                      placeholder={t('common.name')}
                      autoFocus
                    />
                    <div className="flex gap-2">
                      <select
                        value={editTypeIntervalType}
                        onChange={(e) => setEditTypeIntervalType(e.target.value as 'hours' | 'days')}
                        className="flex-1 px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:outline-none"
                      >
                        <option value="hours">{t('maintenance.printHours')}</option>
                        <option value="days">{t('maintenance.calendarDays')}</option>
                      </select>
                      <input
                        type="number"
                        value={editTypeInterval}
                        onChange={(e) => setEditTypeInterval(e.target.value)}
                        className="w-24 px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:outline-none"
                        min="1"
                      />
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {Object.keys(iconMap).map((iconName) => {
                        const IconComp = iconMap[iconName];
                        return (
                          <button
                            key={iconName}
                            type="button"
                            onClick={() => setEditTypeIcon(iconName)}
                            className={`p-1.5 rounded transition-colors ${
                              editTypeIcon === iconName
                                ? 'bg-bambu-green text-white'
                                : 'bg-bambu-dark text-bambu-gray hover:text-white'
                            }`}
                          >
                            <IconComp className="w-3.5 h-3.5" />
                          </button>
                        );
                      })}
                    </div>
                    <input
                      type="url"
                      value={editTypeWikiUrl}
                      onChange={(e) => setEditTypeWikiUrl(e.target.value)}
                      className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:outline-none"
                      placeholder={t('maintenance.documentationLink')}
                    />
                    <div>
                      <label className="block text-xs text-bambu-gray mb-1">{t('maintenance.printerModels')}</label>
                      <div className="flex flex-wrap gap-1 mb-1.5">
                        {editTypePrinterModels.map(code => (
                          <span key={code} className="text-xs px-1.5 py-0.5 bg-bambu-dark-tertiary text-white rounded flex items-center gap-1">
                            {code === '*' ? t('maintenance.allModels') : (macroMeta?.printer_models?.[code] || code)}
                            <button
                              type="button"
                              onClick={() => setEditTypePrinterModels(prev => prev.filter(m => m !== code))}
                              className="hover:text-red-400 transition-colors"
                            >
                              <X className="w-3 h-3" />
                            </button>
                          </span>
                        ))}
                      </div>
                      <select
                        value=""
                        onChange={(e) => {
                          const val = e.target.value;
                          if (!val) return;
                          if (val === '*') {
                            setEditTypePrinterModels(['*']);
                          } else {
                            setEditTypePrinterModels(prev => {
                              const without_wildcard = prev.filter(m => m !== '*');
                              if (without_wildcard.includes(val)) return prev;
                              return [...without_wildcard, val];
                            });
                          }
                        }}
                        className="w-full px-2 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:outline-none"
                      >
                        <option value="">{t('maintenance.addModel')}</option>
                        <option value="*" disabled={editTypePrinterModels.includes('*')}>{t('maintenance.allModels')}</option>
                        {macroMeta?.printer_models && Object.entries(macroMeta.printer_models).filter(([code]) => code !== '*').map(([code, name]) => (
                          <option key={code} value={code} disabled={editTypePrinterModels.includes(code) || editTypePrinterModels.includes('*')}>
                            {name}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div className="flex gap-2">
                      <Button size="sm" onClick={handleSaveEditType} disabled={!editTypeName.trim()}>
                        {t('common.save')}
                      </Button>
                      <Button size="sm" variant="secondary" onClick={() => setEditingType(null)}>
                        {t('common.cancel')}
                      </Button>
                    </div>
                  </div>
                </div>
              );
            }

            const assignedPrinters = getAssignedPrinters(type.id);
            const unassignedPrinters = getUnassignedPrinters(type.id);
            const isExpanded = expandedType === type.id;

            return (
              <div key={type.id} className="bg-bambu-dark-secondary rounded-xl p-4 border border-bambu-green/30">
                <div className="flex items-center gap-3">
                  <div className="p-2.5 bg-bambu-green/20 rounded-lg">
                    <Icon className="w-5 h-5 text-bambu-green" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium text-white truncate">{type.name}</span>
                      <span className="px-1.5 py-0.5 bg-bambu-green/20 text-bambu-green text-[10px] font-medium rounded">
                        {t('maintenance.custom')}
                      </span>
                    </div>
                    <div className="text-xs text-bambu-gray mt-0.5 flex items-center gap-1">
                      {intervalType === 'days' ? <Calendar className="w-3 h-3" /> : <Timer className="w-3 h-3" />}
                      {formatIntervalLabel(type.default_interval_hours, intervalType, t)}
                    </div>
                    {type.printer_models && !(type.printer_models.length === 1 && type.printer_models[0] === '*') && (
                      <div className="text-[10px] text-bambu-gray mt-0.5">
                        {type.printer_models.map(code => macroMeta?.printer_models?.[code] || code).join(', ')}
                      </div>
                    )}
                  </div>
                  <button
                    onClick={() => setExpandedType(isExpanded ? null : type.id)}
                    className={`px-2 py-1 rounded-lg border transition-colors flex items-center gap-1 ${
                      assignedPrinters.length > 0
                        ? 'border-bambu-green/50 bg-bambu-green/10 text-bambu-green hover:bg-bambu-green/20'
                        : 'border-orange-400/50 bg-orange-400/10 text-orange-400 hover:bg-orange-400/20'
                    }`}
                    title={t('maintenance.printersAssignedClick', { count: assignedPrinters.length })}
                  >
                    <Printer className="w-3 h-3" />
                    <span className="text-xs font-medium">{assignedPrinters.length}</span>
                    <ChevronDown className={`w-3 h-3 transition-transform ${isExpanded ? 'rotate-180' : ''}`} />
                  </button>
                  <button
                    onClick={() => startEditType(type)}
                    disabled={!hasPermission('maintenance:update')}
                    title={!hasPermission('maintenance:update') ? t('maintenance.noPermissionEditTypes') : undefined}
                    className={`p-2 rounded-lg hover:bg-bambu-dark text-bambu-gray hover:text-white transition-colors ${!hasPermission('maintenance:update') ? 'opacity-50 cursor-not-allowed' : ''}`}
                  >
                    <Edit3 className="w-4 h-4" />
                  </button>
                  <button
                    onClick={() => {
                      if (confirm(t('maintenance.deleteTypeConfirm', { name: type.name }))) {
                        onDeleteType(type.id);
                      }
                    }}
                    disabled={!hasPermission('maintenance:delete')}
                    title={!hasPermission('maintenance:delete') ? t('maintenance.noPermissionDeleteTypes') : undefined}
                    className={`p-2 rounded-lg hover:bg-bambu-dark text-bambu-gray hover:text-red-400 transition-colors ${!hasPermission('maintenance:delete') ? 'opacity-50 cursor-not-allowed' : ''}`}
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>

                {/* Printer assignment management */}
                {isExpanded && (
                  <div className="mt-3 pt-3 border-t border-bambu-dark-tertiary">
                    <p className="text-xs text-bambu-gray mb-2">{t('maintenance.assignedToPrinters')}</p>
                    {assignedPrinters.length === 0 ? (
                      <p className="text-xs text-orange-400">{t('maintenance.noPrintersAssigned')}</p>
                    ) : (
                      <div className="flex flex-wrap gap-1 mb-2">
                        {assignedPrinters.map(p => (
                          <span
                            key={p.printerId}
                            className="inline-flex items-center gap-1 px-2 py-1 bg-bambu-dark rounded text-xs text-white"
                          >
                            {p.printerName}
                            <button
                              onClick={() => p.itemId && onRemoveItem(p.itemId)}
                              disabled={!hasPermission('maintenance:delete')}
                              title={!hasPermission('maintenance:delete') ? t('maintenance.noPermissionRemovePrinter') : t('maintenance.removeFromPrinter')}
                              className={`ml-1 ${hasPermission('maintenance:delete') ? 'hover:text-red-400' : 'opacity-50 cursor-not-allowed'}`}
                            >
                              ×
                            </button>
                          </span>
                        ))}
                      </div>
                    )}
                    {unassignedPrinters.length > 0 && (
                      <div className="flex flex-wrap gap-1">
                        <span className="text-xs text-bambu-gray mr-1">{t('maintenance.addPrinterShort')}</span>
                        {unassignedPrinters.map(p => (
                          <button
                            key={p.id}
                            onClick={() => onAssignType(p.id, type.id)}
                            disabled={!hasPermission('maintenance:create')}
                            title={!hasPermission('maintenance:create') ? t('maintenance.noPermissionAssignPrinter') : undefined}
                            className={`px-2 py-1 bg-bambu-dark rounded text-xs transition-colors ${hasPermission('maintenance:create') ? 'hover:bg-bambu-green/20 text-bambu-gray hover:text-bambu-green' : 'opacity-50 cursor-not-allowed text-bambu-gray'}`}
                          >
                            + {p.name}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* Per-printer interval overrides */}
      {printerItems.length > 0 && (
        <div>
          <div className="mb-4">
            <h2 className="text-lg font-semibold text-white">{t('maintenance.intervalOverrides')}</h2>
            <p className="text-sm text-bambu-gray mt-1">{t('maintenance.intervalOverridesDescription')}</p>
          </div>
          <div className="space-y-4">
            {printerItems.map((printer) => (
              <Card key={printer.printerId}>
                <CardContent className="py-4">
                  <h3 className="text-sm font-medium text-white mb-3">{printer.printerName}</h3>
                  <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-2">
                    {printer.items.map((item) => {
                      const Icon = getIcon(item.maintenance_type_icon);
                      const typeInfo = types.find(t => t.id === item.maintenance_type_id);
                      const defaultInterval = typeInfo?.default_interval_hours || item.interval_hours;
                      const defaultIntervalType = typeInfo?.interval_type || 'hours';
                      const intervalType = item.interval_type || 'hours';
                      const isEditing = editingInterval === item.id;

                      return (
                        <div key={item.id} className="flex items-center gap-2 p-2.5 bg-bambu-dark rounded-lg">
                          <Icon className="w-4 h-4 text-bambu-gray shrink-0" />
                          <span className="text-xs text-bambu-gray flex-1 truncate">{item.maintenance_type_name}</span>

                          {isEditing ? (
                            <div className="flex items-center gap-1">
                              {intervalTypeInput === 'days' ? (
                                <Calendar className="w-3.5 h-3.5 text-bambu-gray shrink-0" />
                              ) : (
                                <Timer className="w-3.5 h-3.5 text-bambu-gray shrink-0" />
                              )}
                              <select
                                value={intervalTypeInput}
                                onChange={(e) => setIntervalTypeInput(e.target.value as 'hours' | 'days')}
                                className="px-1.5 py-1 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded text-white text-xs"
                              >
                                <option value="hours">{t('maintenance.printHours')}</option>
                                <option value="days">{t('maintenance.calendarDays')}</option>
                              </select>
                              <input
                                type="number"
                                value={intervalInput}
                                onChange={(e) => setIntervalInput(e.target.value)}
                                onKeyDown={(e) => {
                                  if (e.key === 'Enter') handleSaveInterval(item.id, defaultInterval, defaultIntervalType);
                                  if (e.key === 'Escape') setEditingInterval(null);
                                }}
                                className="w-16 px-2 py-1 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded text-white text-xs"
                                min="1"
                              />
                              <Button size="sm" onClick={() => handleSaveInterval(item.id, defaultInterval, defaultIntervalType)}>OK</Button>
                            </div>
                          ) : (
                            <button
                              onClick={() => {
                                if (!hasPermission('maintenance:update')) return;
                                setEditingInterval(item.id);
                                setIntervalInput(item.interval_hours.toString());
                                setIntervalTypeInput(intervalType);
                              }}
                              disabled={!hasPermission('maintenance:update')}
                              title={!hasPermission('maintenance:update') ? t('maintenance.noPermissionEditIntervals') : undefined}
                              className={`px-2 py-1 bg-bambu-dark-tertiary border border-bambu-dark-tertiary rounded text-xs font-medium text-white transition-colors flex items-center gap-1 ${hasPermission('maintenance:update') ? 'hover:bg-bambu-dark-secondary hover:border-bambu-green' : 'opacity-50 cursor-not-allowed'}`}
                            >
                              {intervalType === 'days' ? <Calendar className="w-3 h-3" /> : <Timer className="w-3 h-3" />}
                              {formatIntervalLabel(item.interval_hours, intervalType, t)}
                              <Edit3 className="w-3 h-3 text-bambu-gray" />
                            </button>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>
        </div>
      )}

      {printerItems.length === 0 && (
        <Card>
          <CardContent className="text-center py-12">
            <Clock className="w-12 h-12 mx-auto mb-4 text-bambu-gray/30" />
            <p className="text-bambu-gray">{t('common.noPrinters')}</p>
            <p className="text-sm text-bambu-gray/70 mt-1">
              {t('maintenance.intervalOverridesDescription')}
            </p>
          </CardContent>
        </Card>
      )}

      {pendingSystemDelete && (
        <ConfirmModal
          title={t('maintenance.deleteSystemTypeTitle')}
          message={t('maintenance.deleteSystemTypeMessage', { name: pendingSystemDelete.name })}
          confirmText={t('common.delete')}
          cancelText={t('common.cancel')}
          variant="danger"
          cancelVariant="primary"
          cardClassName="bg-red-950/70 border border-red-800/70"
          onConfirm={() => {
            onDeleteType(pendingSystemDelete.id);
            setPendingSystemDelete(null);
          }}
          onCancel={() => setPendingSystemDelete(null)}
        />
      )}
    </div>
  );
}

// --- History Section ---
type HistorySortField = 'date' | 'user';
type HistorySortDir = 'asc' | 'desc';

function HistorySection({ t, printerId }: { t: (key: string, opts?: Record<string, unknown>) => string; printerId?: number }) {
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(25);
  const [sortField, setSortField] = useState<HistorySortField>('date');
  const [sortDir, setSortDir] = useState<HistorySortDir>('desc');
  const [filterPrinterId, setFilterPrinterId] = useState<number | undefined>(printerId);

  useEffect(() => {
    setFilterPrinterId(printerId);
  }, [printerId]);

  const { data: printers } = useQuery({
    queryKey: ['printers'],
    queryFn: api.getPrinters,
  });

  const { data: settings } = useQuery({
    queryKey: ['settings'],
    queryFn: api.getSettings,
  });

  const { data, isLoading } = useQuery({
    queryKey: ['maintenanceHistory', page, perPage, sortField, sortDir, filterPrinterId],
    queryFn: () => api.getAllMaintenanceHistory(page, perPage, sortField, sortDir, filterPrinterId),
  });

  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const lastPage = data?.last_page ?? 1;

  const getPerformedBy = useCallback((entry: MaintenanceHistoryEntry): string => {
    if (entry.performed_by_username) return entry.performed_by_username;
    if (entry.performed_by_chat_label) return entry.performed_by_chat_label;
    return '-';
  }, []);

  const toggleSort = (field: HistorySortField) => {
    if (sortField === field) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    } else {
      setSortField(field);
      setSortDir(field === 'date' ? 'desc' : 'asc');
    }
    setPage(1);
  };

  const SortIcon = ({ field }: { field: HistorySortField }) => {
    if (sortField !== field) return null;
    return sortDir === 'asc'
      ? <ChevronUp className="w-3 h-3 inline ml-0.5" />
      : <ChevronDown className="w-3 h-3 inline ml-0.5" />;
  };

  if (isLoading) {
    return (
      <Card>
        <CardContent className="text-center py-16">
          <Loader2 className="w-8 h-8 mx-auto mb-4 text-bambu-green animate-spin" />
        </CardContent>
      </Card>
    );
  }

  if (items.length === 0 && page === 1) {
    return (
      <Card>
        <CardContent className="text-center py-16">
          <History className="w-16 h-16 mx-auto mb-4 text-bambu-gray/30" />
          <p className="text-lg font-medium text-white mb-2">{t('maintenance.historyEmpty')}</p>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      {/* Toolbar: printer filter + per-page + pagination */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm text-bambu-gray">
          <select
            value={filterPrinterId ?? ''}
            onChange={(e) => { setFilterPrinterId(e.target.value ? Number(e.target.value) : undefined); setPage(1); }}
            className="bg-bambu-dark border border-bambu-dark-tertiary text-white rounded px-2 py-1.5 text-sm focus:border-bambu-green focus:outline-none"
            title={t('maintenance.filterByPrinter')}
          >
            <option value="">{t('maintenance.allPrinters')}</option>
            {printers?.map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
          <span className="hidden sm:inline">{t('common.show')}:</span>
          <select
            value={perPage}
            onChange={(e) => { setPerPage(Number(e.target.value)); setPage(1); }}
            className="w-16 bg-bambu-dark border border-bambu-dark-tertiary text-white rounded px-2 py-1.5 text-center text-sm focus:border-bambu-green focus:outline-none"
          >
            <option value={10}>10</option>
            <option value={25}>25</option>
            <option value={50}>50</option>
          </select>
          <span>{total} {t('common.total')}</span>
          <button
            onClick={async () => {
              try {
                const token = getAuthToken();
                const exportUrl = filterPrinterId ? `/api/v1/maintenance/history/export?printer_id=${filterPrinterId}` : '/api/v1/maintenance/history/export';
                const res = await fetch(exportUrl, {
                  headers: token ? { Authorization: `Bearer ${token}` } : {},
                });
                if (!res.ok) throw new Error('Export failed');
                const blob = await res.blob();
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = res.headers.get('content-disposition')?.match(/filename="(.+)"/)?.[1] || 'maintenance_history.xlsx';
                a.click();
                URL.revokeObjectURL(url);
              } catch { /* ignore */ }
            }}
            className="p-1.5 rounded text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary transition-colors"
            title={t('maintenance.historyExport')}
          >
            <Download className="w-4 h-4" />
          </button>
        </div>
        {lastPage > 1 && (
          <div className="flex items-center gap-1">
            <button onClick={() => setPage(1)} disabled={page <= 1} className="p-1.5 rounded text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary disabled:opacity-30">
              <ChevronsLeft className="w-4 h-4" />
            </button>
            <button onClick={() => setPage(p => Math.max(1, p - 1))} disabled={page <= 1} className="p-1.5 rounded text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary disabled:opacity-30">
              <ChevronLeft className="w-4 h-4" />
            </button>
            <span className="text-sm text-bambu-gray px-2">{page} / {lastPage}</span>
            <button onClick={() => setPage(p => Math.min(lastPage, p + 1))} disabled={page >= lastPage} className="p-1.5 rounded text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary disabled:opacity-30">
              <ChevronRight className="w-4 h-4" />
            </button>
            <button onClick={() => setPage(lastPage)} disabled={page >= lastPage} className="p-1.5 rounded text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary disabled:opacity-30">
              <ChevronsRight className="w-4 h-4" />
            </button>
          </div>
        )}
      </div>

      {/* Table */}
      <Card>
        <CardContent className="p-0">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-bambu-dark-tertiary text-bambu-gray text-left">
                  <th className="px-4 py-3 font-medium cursor-pointer hover:text-white select-none" onClick={() => toggleSort('date')}>
                    {t('maintenance.historyDate')}<SortIcon field="date" />
                  </th>
                  <th className="px-4 py-3 font-medium">{t('maintenance.historyPrinter')}</th>
                  <th className="px-4 py-3 font-medium">{t('maintenance.historyType')}</th>
                  <th className="px-4 py-3 font-medium">{t('maintenance.historyHours')}</th>
                  <th className="px-4 py-3 font-medium cursor-pointer hover:text-white select-none" onClick={() => toggleSort('user')}>
                    {t('maintenance.historyPerformedBy')}<SortIcon field="user" />
                  </th>
                  <th className="px-4 py-3 font-medium">{t('maintenance.historyNotes')}</th>
                </tr>
              </thead>
              <tbody>
                {items.map((entry) => (
                  <tr key={entry.id} className="border-b border-bambu-dark-tertiary/50 hover:bg-bambu-dark-tertiary/30">
                    <td className="px-4 py-3 text-white whitespace-nowrap">
                      {formatDateTime(entry.performed_at, settings?.time_format || 'system', settings?.date_format || 'system')}
                    </td>
                    <td className="px-4 py-3 text-white">{entry.printer_name ?? '-'}</td>
                    <td className="px-4 py-3 text-white">{entry.maintenance_type_name ?? '-'}</td>
                    <td className="px-4 py-3 text-bambu-gray">{entry.hours_at_maintenance.toFixed(1)}h</td>
                    <td className="px-4 py-3 text-bambu-gray">{getPerformedBy(entry)}</td>
                    <td className="px-4 py-3 text-bambu-gray max-w-[200px]">
                      {entry.notes ? <span className="truncate block" title={entry.notes}>{entry.notes}</span> : '-'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

type TabType = 'status' | 'history' | 'settings';

export function MaintenancePage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { hasPermission } = useAuth();
  const [searchParams] = useSearchParams();
  const printerIdParam = searchParams.get('printer') ? Number(searchParams.get('printer')) : undefined;
  const [activeTab, setActiveTab] = useState<TabType>('status');

  useEffect(() => {
    if (printerIdParam && activeTab !== 'history') {
      setActiveTab('history');
    }
  }, [printerIdParam]); // eslint-disable-line react-hooks/exhaustive-deps

  const { data: overview, isLoading } = useQuery({
    queryKey: ['maintenanceOverview'],
    queryFn: api.getMaintenanceOverview,
  });

  const { data: types } = useQuery({
    queryKey: ['maintenanceTypes'],
    queryFn: api.getMaintenanceTypes,
  });

  const { data: macroMeta } = useQuery({
    queryKey: ['macroMeta'],
    queryFn: macrosApi.getMacroMeta,
  });

  const performMutation = useMutation({
    mutationFn: ({ id, notes }: { id: number; notes?: string }) =>
      api.performMaintenance(id, notes),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['maintenanceOverview'] });
      queryClient.invalidateQueries({ queryKey: ['maintenanceSummary'] });
      showToast(t('maintenance.maintenanceComplete'));
    },
    onError: (error: Error) => {
      showToast(error.message, 'error');
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: { custom_interval_hours?: number | null; custom_interval_type?: 'hours' | 'days' | null; enabled?: boolean } }) =>
      api.updateMaintenanceItem(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['maintenanceOverview'] });
    },
    onError: (error: Error) => {
      showToast(error.message, 'error');
    },
  });

  // addTypeMutation removed - we now handle type creation with printer assignment
  // directly in onAddType callback

  const updateTypeMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: Partial<{ name: string; default_interval_hours: number; interval_type: 'hours' | 'days'; icon: string }> }) =>
      api.updateMaintenanceType(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['maintenanceTypes'] });
      queryClient.invalidateQueries({ queryKey: ['maintenanceOverview'] });
      showToast(t('maintenance.typeUpdated'));
    },
    onError: (error: Error) => {
      showToast(error.message, 'error');
    },
  });

  const deleteTypeMutation = useMutation({
    mutationFn: api.deleteMaintenanceType,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['maintenanceTypes'] });
      queryClient.invalidateQueries({ queryKey: ['maintenanceOverview'] });
      showToast(t('maintenance.typeDeleted'));
    },
    onError: (error: Error) => {
      showToast(error.message, 'error');
    },
  });

  const restoreDefaultsMutation = useMutation({
    mutationFn: api.restoreDefaultMaintenanceTypes,
    onSuccess: (data: { restored: number }) => {
      queryClient.invalidateQueries({ queryKey: ['maintenanceTypes'] });
      queryClient.invalidateQueries({ queryKey: ['maintenanceOverview'] });
      showToast(t('maintenance.defaultsRestored', { count: data.restored }));
    },
    onError: (error: Error) => {
      showToast(error.message, 'error');
    },
  });

  const setHoursMutation = useMutation({
    mutationFn: ({ printerId, hours }: { printerId: number; hours: number }) =>
      api.setPrinterHours(printerId, hours),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['maintenanceOverview'] });
      queryClient.invalidateQueries({ queryKey: ['maintenanceSummary'] });
      showToast(t('maintenance.printHoursUpdated'));
    },
    onError: (error: Error) => {
      showToast(error.message, 'error');
    },
  });

  const assignTypeMutation = useMutation({
    mutationFn: ({ printerId, typeId }: { printerId: number; typeId: number }) =>
      api.assignMaintenanceType(printerId, typeId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['maintenanceOverview'] });
      showToast(t('maintenance.printerAssigned'));
    },
    onError: (error: Error) => {
      showToast(error.message, 'error');
    },
  });

  const removeItemMutation = useMutation({
    mutationFn: api.removeMaintenanceItem,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['maintenanceOverview'] });
      showToast(t('maintenance.printerRemoved'));
    },
    onError: (error: Error) => {
      showToast(error.message, 'error');
    },
  });

  const handlePerform = (id: number) => {
    performMutation.mutate({ id });
  };

  const handleToggle = (id: number, enabled: boolean) => {
    updateMutation.mutate({ id, data: { enabled } });
  };

  const handleSetHours = (printerId: number, hours: number) => {
    setHoursMutation.mutate({ printerId, hours });
  };

  if (isLoading) {
    return (
      <div className="p-4 md:p-6 flex justify-center">
        <Loader2 className="w-8 h-8 text-bambu-green animate-spin" />
      </div>
    );
  }

  const totalDue = overview?.reduce((sum, p) => sum + p.due_count, 0) || 0;
  const totalWarning = overview?.reduce((sum, p) => sum + p.warning_count, 0) || 0;

  return (
    <div className="p-4 md:p-6 space-y-4">
      {/* Header */}
      <div>
        <div className="flex items-center gap-3">
          {/*<Disc3 className="w-6 h-6 text-bambu-green" />*/}
          <h1 className="text-2xl font-bold text-white">{t('maintenance.title')}</h1>
        </div>
        <p className="text-sm text-bambu-gray">
          {activeTab === 'status' ? (
              <>
                {totalDue > 0 && <span className="text-red-400">{t('maintenance.dueCount', { count: totalDue })}</span>}
                {totalDue > 0 && totalWarning > 0 && ' · '}
                {totalWarning > 0 && <span className="text-amber-400">{t('maintenance.warningCount', { count: totalWarning })}</span>}
                {totalDue === 0 && totalWarning === 0 && <span className="text-bambu-green">{t('maintenance.allOk')}</span>}
              </>
          ) : (
              t('maintenance.configureSettings')
          )}
        </p>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 mb-4 border-b border-bambu-dark-tertiary">
        <button
          onClick={() => setActiveTab('status')}
          className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
            activeTab === 'status'
              ? 'text-bambu-green border-bambu-green'
              : 'text-bambu-gray border-transparent hover:text-white'
          }`}
        >
          {t('maintenance.statusTab')}
        </button>
        <button
          onClick={() => setActiveTab('history')}
          className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors flex items-center gap-1.5 ${
            activeTab === 'history'
              ? 'text-bambu-green border-bambu-green'
              : 'text-bambu-gray border-transparent hover:text-white'
          }`}
        >
          <History className="w-4 h-4" />
          {t('maintenance.historyTab')}
        </button>
        <button
          onClick={() => setActiveTab('settings')}
          className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
            activeTab === 'settings'
              ? 'text-bambu-green border-bambu-green'
              : 'text-bambu-gray border-transparent hover:text-white'
          }`}
        >
          {t('maintenance.settingsTab')}
        </button>
      </div>

      {/* Tab content */}
      {activeTab === 'status' ? (
        <div>
          {overview && overview.length > 0 ? (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start">
            {[...overview].sort((a, b) => {
              // Sort printers with issues first
              const aScore = a.due_count * 10 + a.warning_count;
              const bScore = b.due_count * 10 + b.warning_count;
              if (aScore !== bScore) return bScore - aScore;
              return a.printer_name.localeCompare(b.printer_name);
            }).map((printerOverview) => (
              <PrinterSection
                key={printerOverview.printer_id}
                overview={printerOverview}
                types={types || []}
                macroMeta={macroMeta}
                onPerform={handlePerform}
                onToggle={handleToggle}
                onSetHours={handleSetHours}
                hasPermission={hasPermission}
                t={t}
              />
            ))}
            </div>
          ) : (
            <Card>
              <CardContent className="text-center py-16">
                <Wrench className="w-16 h-16 mx-auto mb-4 text-bambu-gray/30" />
                <p className="text-lg font-medium text-white mb-2">{t('common.noPrinters')}</p>
                <p className="text-bambu-gray">{t('maintenance.configureSettings')}</p>
              </CardContent>
            </Card>
          )}
        </div>
      ) : activeTab === 'history' ? (
        <HistorySection t={t} printerId={printerIdParam} />
      ) : (
        <SettingsSection
          overview={overview}
          types={types || []}
          macroMeta={macroMeta}
          onUpdateInterval={(id, data) =>
            updateMutation.mutate({ id, data })
          }
          onAddType={async (data, printerIds) => {
            // Create the type first, then assign to selected printers
            const newType = await api.createMaintenanceType(data);
            // Assign to each selected printer
            for (const printerId of printerIds) {
              await api.assignMaintenanceType(printerId, newType.id);
            }
            queryClient.invalidateQueries({ queryKey: ['maintenanceTypes'] });
            queryClient.invalidateQueries({ queryKey: ['maintenanceOverview'] });
            showToast(t('maintenance.typeUpdated'));
          }}
          onUpdateType={(id, data) => updateTypeMutation.mutate({ id, data })}
          onDeleteType={(id) => deleteTypeMutation.mutate(id)}
          onRestoreDefaults={() => restoreDefaultsMutation.mutate()}
          isRestoringDefaults={restoreDefaultsMutation.isPending}
          onAssignType={(printerId, typeId) => assignTypeMutation.mutate({ printerId, typeId })}
          onRemoveItem={(itemId) => removeItemMutation.mutate(itemId)}
          hasPermission={hasPermission}
          t={t}
        />
      )}
    </div>
  );
}
