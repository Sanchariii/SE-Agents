import { AnalyticsDashboard, AnalyticsMetric, AnalyticsProject, AnalyticsTrendPoint, AnalyticsWindow, MetricFormat, MetricType } from '../models/analytics.models';

export const DAY_MS = 86400000;

export function formatAnalyticsValue(value: number | null | undefined, format: MetricFormat = 'number'): string {
  if (value == null || !Number.isFinite(value)) return 'No data';
  const number = (amount: number, digits = 1) => new Intl.NumberFormat('en-US', {
    maximumFractionDigits: digits,
  }).format(amount);
  switch (format) {
    case 'compact': return new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 2 }).format(value);
    case 'currency': {
      // 2 decimals; 4 under $0.10 so per-call / per-run costs stay legible.
      const digits = value !== 0 && Math.abs(value) < 0.1 ? 4 : 2;
      return new Intl.NumberFormat('en-US', {
        style: 'currency', currency: 'USD', minimumFractionDigits: digits, maximumFractionDigits: digits,
      }).format(value);
    }
    case 'percent': {
      // Small rates keep 2 decimals (0.0035 -> 0.35%); everything else 1.
      const percent = value * 100;
      return `${number(percent, percent > 0 && percent < 10 ? 2 : 1)}%`;
    }
    case 'hours': return `${number(value)} h`;
    case 'leverage': return `${number(value)}x`;
    case 'duration':
      if (value === 0) return '0 s';
      if (value < 1000) return `${number(value, 0)} ms`;
      if (value < 60000) return `${number(value / 1000)} s`;
      if (value < 3600000) return `${number(value / 60000)} min`;
      return `${number(value / 3600000)} h`;
    default: return number(value, 2);
  }
}

export function metricHasDenominator(metric: AnalyticsMetric | null | undefined): boolean {
  return !!metric && metric.of !== 0 && metric.denominator !== 0;
}

export function analyticsWindow(
  preset: string, fromDate: string, toDate: string, timezone: string, now = new Date(),
): AnalyticsWindow {
  let start: Date;
  let end: Date;
  if (preset === 'custom') {
    const parseDate = (value: string): Date => {
      const parsed = new Date(`${value}T00:00:00.000Z`);
      if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || !Number.isFinite(parsed.getTime())
        || parsed.toISOString().slice(0, 10) !== value) throw new Error('Enter a valid start and end date.');
      return parsed;
    };
    start = parseDate(fromDate);
    end = new Date(parseDate(toDate).getTime() + DAY_MS);
  } else {
    if (!['7', '30', '90'].includes(preset)) throw new Error('Select a valid date range.');
    end = new Date(now);
    start = new Date(end.getTime() - Number(preset) * DAY_MS);
  }
  if (start >= end) throw new Error('The start date must be on or before the end date.');
  if (end.getTime() - start.getTime() > 366 * DAY_MS) throw new Error('Select a date range of 366 days or fewer.');
  return { from: start.toISOString(), to: end.toISOString(), timezone };
}

export const PROJECT_CSV_COLUMNS: (keyof AnalyticsProject)[] = [
  'session_id', 'owner_name', 'turns', 'multi_turn', 'published', 'slides_delivered',
  'ai_turnaround_ms', 'agent_processing_ms', 'total_tokens', 'cost_usd', 'last_active',
];

export function projectsCsv(projects: AnalyticsProject[]): string {
  const escapeCell = (value: unknown): string => {
    let text = value == null ? '' : String(value);
    if (typeof value === 'string' && /^[\s\u0000-\u001f]*[=+\-@]/.test(text)) text = `'${text}`;
    return `"${text.replace(/"/g, '""')}"`;
  };
  return [PROJECT_CSV_COLUMNS.join(','), ...projects.map((project) =>
    PROJECT_CSV_COLUMNS.map((key) => escapeCell(project[key])).join(','))].join('\r\n');
}

export type SortDirection = 'asc' | 'desc';
export interface SortState<Key extends string> { key: Key; dir: SortDirection; }

/** Sorts a copy of `rows`; null / missing values always sort last. */
export function sortRows<Row, Key extends string>(rows: Row[], sort: SortState<Key>, value: (row: Row, key: Key) => unknown): Row[] {
  const direction = sort.dir === 'asc' ? 1 : -1;
  const missing = (item: unknown) => item == null || item === '' || (typeof item === 'number' && !Number.isFinite(item));
  return [...rows].sort((first, second) => {
    const a = value(first, sort.key);
    const b = value(second, sort.key);
    if (missing(a) || missing(b)) return missing(a) && missing(b) ? 0 : missing(a) ? 1 : -1;
    if (typeof a === 'number' && typeof b === 'number') return (a - b) * direction;
    return String(a).localeCompare(String(b), 'en', { numeric: true, sensitivity: 'base' }) * direction;
  });
}

export interface TrendSlot { key: string; time: number; point: AnalyticsTrendPoint | null; }

/** One slot per UTC day across the window (days without a trend point stay empty); weekly buckets pass through. */
export function trendSlots(dashboard: AnalyticsDashboard): TrendSlot[] {
  const dayKey = (value: string) => /^\d{4}-\d{2}-\d{2}/.test(value) ? value.slice(0, 10) : new Date(value).toISOString().slice(0, 10);
  const points = (dashboard.trends?.points || [])
    .filter((point): point is AnalyticsTrendPoint & { bucket: string } => !!point.bucket && Number.isFinite(Date.parse(point.bucket)));
  const slot = (key: string, point: AnalyticsTrendPoint | null): TrendSlot => ({ key, time: Date.parse(`${key}T00:00:00Z`), point });
  if (dashboard.trends?.bucket !== 'day') return points.map((point) => slot(dayKey(point.bucket), point));
  const byDay = new Map(points.map((point) => [dayKey(point.bucket), point]));
  const dayStart = (time: number) => Date.parse(`${new Date(time).toISOString().slice(0, 10)}T00:00:00Z`);
  const times = [...byDay.keys()].map((key) => Date.parse(`${key}T00:00:00Z`));
  const from = Date.parse(dashboard.window?.from);
  const to = Date.parse(dashboard.window?.to);
  if (Number.isFinite(from)) times.push(dayStart(from));
  if (Number.isFinite(to)) times.push(dayStart(to - 1));
  if (!times.length) return [];
  const start = Math.min(...times);
  const end = Math.min(Math.max(...times), start + 400 * DAY_MS);
  const slots: TrendSlot[] = [];
  for (let time = start; time <= end; time += DAY_MS) {
    const key = new Date(time).toISOString().slice(0, 10);
    slots.push(slot(key, byDay.get(key) || null));
  }
  return slots;
}

export interface RawMetricRow { group: string; path: string; value: string; extra: string; type?: MetricType; }

/** Flattens the dashboard payload into "path -> exact value" rows (agent / model arrays have their own tables). */
export function flattenRaw(dashboard: AnalyticsDashboard): RawMetricRow[] {
  const groups = ['window', 'filters', 'overview', 'trends', 'adoption', 'performance', 'cost', 'quality', 'efficiency', 'data_quality'] as const;
  const rows: RawMetricRow[] = [];
  const integer = (value: number) => new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 }).format(value);
  const raw = (path: string, value: unknown): string => {
    if (value == null) return 'null';
    if (typeof value !== 'number') return String(value);
    if (/_usd\b/.test(path)) return `$${value}`;
    if (/_ms\b/.test(path)) return `${integer(value)} ms`;
    return Number.isInteger(value) ? integer(value) : String(value);
  };
  const walk = (value: unknown, path: string, group: string): void => {
    if (Array.isArray(value)) { value.forEach((item, index) => walk(item, `${path}[${index}]`, group)); return; }
    if (value && typeof value === 'object') {
      const record = value as Record<string, unknown>;
      const type = typeof record['type'] === 'string' ? record['type'] as MetricType : undefined;
      if ('value' in record && type) {
        const extra = (['count', 'of', 'denominator'] as const).filter((key) => key in record)
          .map((key) => `${key} ${raw(key, record[key])}`).join(', ');
        rows.push({ group, path, value: raw(path, record['value']), extra, type });
        return;
      }
      if (type && ('median' in record || 'total' in record)) {
        Object.keys(record).filter((key) => key !== 'type').forEach((key) => rows.push({
          group, path: `${path}.${key}`, value: raw(path, record[key]), extra: '', type: key === 'note' ? undefined : type,
        }));
        return;
      }
      Object.keys(record).forEach((key) => walk(record[key], `${path}.${key}`, group));
      return;
    }
    rows.push({ group, path, value: raw(path, value), extra: '' });
  };
  groups.forEach((group) => { if (group in dashboard) walk(dashboard[group], group, group); });
  return rows;
}
