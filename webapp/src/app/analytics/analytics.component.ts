import { Component, ElementRef, EventEmitter, Input, NgZone, OnChanges, OnDestroy, Output, ViewChild } from '@angular/core';
import { AnalyticsAgent, AnalyticsDashboard, AnalyticsMetric, AnalyticsModel, AnalyticsProject, AnalyticsProjectDetail, AnalyticsProjectSort, AnalyticsWindow, MetricFormat, MetricType, TrendMetric } from '../models/analytics.models';
import { ApiService } from '../shared/services/api.service';
import { AnalyticsChartSeries } from './analytics-chart.component';
import { analyticsWindow, flattenRaw, formatAnalyticsValue, projectsCsv, RawMetricRow, sortRows, SortState, trendSlots } from './analytics.helpers';

interface KvRow { label: string; value: string; type?: MetricType; }
interface StatTile { label: string; value: string; caption?: string; }
interface HBar { label: string; value: string; width: number; tone?: 'teal' | 'gold'; }
interface SparkRect { x: number; y: number; width: number; height: number; kind: 'bar' | 'zero' | 'empty'; }
interface Owner { name: string; initials: string; sessions: number; slides: number; }
interface TokenSegment { label: string; color: string; width: number; value: string; share: string; }
interface ModelRow { model: AnalyticsModel; color: string; share: number | null; }
interface RawGroup { name: string; rows: (RawMetricRow & { key: string })[]; }
interface DataQualityItem { label: string; why: string; affects: string; count: number | null; }

interface AnalyticsView {
  slides: { value: string; spark: SparkRect[]; sparkLabel: string; details: KvRow[] };
  published: { value: string; of: string; rate: number | null; rateText: string; published: string; notPublished: string; details: KvRow[] };
  people: { value: string; caption: string; owners: Owner[]; details: KvRow[] };
  hours: { value: string; caption: string; confidence: string; agentShare: number; agentShareText: string; details: KvRow[]; note: string };
  reliability: { value: string; rate: number | null; caption: string; details: KvRow[] };
  cost: { perSlide: string; perProject: string; total: string; calls: string; perHour: string; details: KvRow[]; note: string };
  adoption: { stats: StatTile[]; multiTurn: HBar[]; turns: HBar[]; turnsLabel: string; details: KvRow[]; note: string };
  performance: { stats: StatTile[]; wait: HBar[]; active: HBar[]; parallelNote: string; details: KvRow[]; note: string };
  quality: { stats: StatTile[]; noRatings: boolean; feedback: StatTile[]; details: KvRow[] };
  tokens: { stats: StatTile[]; split: TokenSegment[]; details: KvRow[] };
}

type AgentSortKey = keyof AnalyticsAgent | 'tokens';
type ModelSortKey = keyof AnalyticsModel | 'tokens' | 'share';
type TierId = 'analytics-glance' | 'analytics-operations' | 'analytics-technical';

const SPARK_WIDTH = 132;
const SPARK_HEIGHT = 40;
const MODEL_COLORS = ['--analytics-chart-blue', '--analytics-chart-teal', '--analytics-chart-gold', '--analytics-chart-red', '--muted'];

@Component({
  selector: 'app-analytics',
  standalone: false,
  templateUrl: './analytics.component.html',
  styleUrls: ['./analytics.component.scss'],
})
export class AnalyticsComponent implements OnChanges, OnDestroy {
  @Input() active = false;
  @Input() isAdmin = false;
  @Output() readonly accessDenied = new EventEmitter<void>();

  dashboard: AnalyticsDashboard | null = null;
  view: AnalyticsView | null = null;
  loading = false;
  error = '';
  validationError = '';
  filtersOpen = false;
  rangePreset = '30';
  fromDate = '';
  toDate = '';
  timezone = 'UTC';
  readonly timezones = [...new Set(['UTC', Intl.DateTimeFormat().resolvedOptions().timeZone,
    'America/Chicago', 'America/New_York', 'America/Los_Angeles', 'Europe/London', 'Europe/Berlin',
    'Asia/Kolkata', 'Asia/Singapore', 'Asia/Tokyo', 'Australia/Sydney'])];
  appliedWindow: AnalyticsWindow = analyticsWindow('30', '', '', 'UTC');
  trendMetric: TrendMetric = 'slides_delivered';
  readonly trendOptions: { value: TrendMetric; label: string; unit: string; format: MetricFormat; color: string; peak?: boolean }[] = [
    { value: 'slides_delivered', label: 'Slides delivered', unit: 'slides delivered', format: 'number', color: '--analytics-chart-red' },
    { value: 'workflow_runs', label: 'Workflow runs', unit: 'workflow runs', format: 'number', color: '--analytics-chart-blue' },
    { value: 'active_users', label: 'Active users', unit: 'active users', format: 'number', color: '--analytics-chart-teal', peak: true },
    { value: 'cost_usd', label: 'AI cost', unit: 'in AI cost', format: 'currency', color: '--analytics-chart-gold' },
  ];
  trendLabels: string[] = [];
  trendSeries: AnalyticsChartSeries[] = [];
  trendSummary = { value: '', caption: '' };
  readonly tiers: { id: TierId; label: string }[] = [
    { id: 'analytics-glance', label: 'At a glance' },
    { id: 'analytics-operations', label: 'Operations' },
    { id: 'analytics-technical', label: 'Technical details' },
  ];
  activeTier: TierId = 'analytics-glance';
  agentsSort: SortState<AgentSortKey> = { key: 'cost_usd', dir: 'desc' };
  modelsSort: SortState<ModelSortKey> = { key: 'cost_usd', dir: 'desc' };
  readonly agentColumns: { key: AgentSortKey; label: string; num?: boolean }[] = [
    { key: 'agent_name', label: 'Agent' }, { key: 'model', label: 'Model' }, { key: 'runs', label: 'Runs', num: true },
    { key: 'success_rate', label: 'Success', num: true }, { key: 'fallback_rate', label: 'Fallback', num: true },
    { key: 'median_duration_ms', label: 'Median', num: true }, { key: 'p95_duration_ms', label: 'p95', num: true },
    { key: 'llm_calls', label: 'LLM calls', num: true }, { key: 'tokens', label: 'Tokens in / out', num: true },
    { key: 'cost_usd', label: 'Cost', num: true }, { key: 'cost_per_run_usd', label: 'Cost / run', num: true },
  ];
  readonly modelColumns: { key: ModelSortKey; label: string; num?: boolean }[] = [
    { key: 'model', label: 'Model' }, { key: 'runs', label: 'Runs', num: true }, { key: 'failure_rate', label: 'Failure rate', num: true },
    { key: 'median_duration_ms', label: 'Median duration', num: true }, { key: 'llm_calls', label: 'LLM calls', num: true },
    { key: 'tokens', label: 'Tokens in / out', num: true }, { key: 'cost_usd', label: 'Cost', num: true }, { key: 'share', label: 'Share of cost' },
  ];
  agentRows: AnalyticsAgent[] = [];
  modelRows: ModelRow[] = [];
  agentsBelowTarget: string[] = [];
  agentRunTotal = 0;
  modelsTotalCost = 0;
  topModel: ModelRow | null = null;
  projectStats = { published: 0, owners: 0 };
  dataQuality: DataQualityItem[] = [];
  dataQualityFlagged: DataQualityItem[] = [];
  dataQualityTotal = 0;
  rawGroups: RawGroup[] = [];
  rawCount = 0;
  readonly format = formatAnalyticsValue;
  protected destroyed = false;
  protected denied = false;
  private attempted = false;
  private dashboardController?: AbortController;
  projects: AnalyticsProject[] = [];
  projectRows: AnalyticsProject[] = [];
  projectQuery = '';
  projectsLoading = false;
  projectsError = '';
  projectTotal = 0;
  pageSize = 50;
  pageOffset = 0;
  projectSort: AnalyticsProjectSort = 'last_active';
  projectOrder: 'asc' | 'desc' = 'desc';
  exporting = false;
  exportedRows = 0;
  exportTotal = 0;
  exportError = '';
  selectedProject: string | null = null;
  projectDetail: AnalyticsProjectDetail | null = null;
  detailLoading = false;
  detailError = '';
  detailTab: 'workflow' | 'agents' | 'ingestion' = 'workflow';
  detailRunFilter = '';
  @ViewChild('projectDialog', { static: true }) projectDialog!: ElementRef<HTMLDialogElement>;
  private projectsController?: AbortController;
  private detailController?: AbortController;
  private exportController?: AbortController;
  private returnFocus?: HTMLElement;
  private scrollElement?: HTMLElement;
  private readonly onScroll = () => this.updateActiveTier();

  /** The analytics tab panel is the scroll container; track it to highlight the current tier chip. */
  @ViewChild('scroller') set scroller(ref: ElementRef<HTMLElement> | undefined) {
    const element = ref?.nativeElement;
    if (element === this.scrollElement) return;
    this.scrollElement?.removeEventListener('scroll', this.onScroll);
    this.scrollElement = element;
    this.zone.runOutsideAngular(() => element?.addEventListener('scroll', this.onScroll, { passive: true }));
  }

  constructor(protected api: ApiService, private host: ElementRef<HTMLElement>, private zone: NgZone) { this.setPreset('30'); }

  ngOnChanges(): void {
    if (!this.active) { this.closeProject(); this.cancelExport(); }
    if (!this.isAdmin) {
      this.dashboardController?.abort();
      this.projectsController?.abort();
      this.projects = [];
      this.dashboard = null;
      this.buildView();
      this.attempted = false;
      return;
    }
    if (this.active && !this.attempted && !this.denied) this.applyWindow();
  }

  ngOnDestroy(): void {
    this.destroyed = true;
    this.scrollElement?.removeEventListener('scroll', this.onScroll);
    this.dashboardController?.abort();
    this.projectsController?.abort();
    this.closeProject();
    this.cancelExport();
  }

  setPreset(preset: string): void {
    this.rangePreset = preset;
    if (preset === 'custom') return;
    const window = analyticsWindow(preset, '', '', this.timezone);
    this.fromDate = window.from.slice(0, 10);
    this.toDate = window.to.slice(0, 10);
  }

  submitFilters(): void {
    this.applyWindow();
    if (!this.validationError) this.filtersOpen = false;
  }

  applyWindow(): void {
    this.validationError = '';
    try {
      this.appliedWindow = analyticsWindow(this.rangePreset, this.fromDate, this.toDate, this.timezone);
    } catch (error) {
      this.validationError = this.errorMessage(error);
      return;
    }
    this.closeProject();
    this.cancelExport();
    this.exportError = '';
    void this.loadDashboard();
    void this.loadProjects(0);
  }

  async loadDashboard(): Promise<void> {
    if (!this.isAdmin || !this.api.hasRole('admin') || this.denied || this.destroyed) return;
    this.attempted = true;
    this.dashboardController?.abort();
    const controller = this.dashboardController = new AbortController();
    this.loading = true;
    this.error = '';
    this.dashboard = null;
    try {
      const data = await this.api.getAnalyticsDashboard({
        from: this.appliedWindow.from, to: this.appliedWindow.to, tz: this.appliedWindow.timezone,
      }, controller.signal);
      if (controller.signal.aborted || this.destroyed || this.denied) return;
      this.dashboard = data;
      this.buildView();
    } catch (error) {
      if (!controller.signal.aborted && !this.destroyed && !this.handleAccessError(error)) this.error = this.errorMessage(error);
    } finally {
      if (this.dashboardController === controller) this.loading = false;
    }
  }

  get trendSelection() { return this.trendOptions.find((option) => option.value === this.trendMetric)!; }
  get hasTrendData(): boolean { return this.trendSeries.some((series) => series.values.some((value) => value != null)); }
  get filterSummary(): string { return `${this.windowLabel} · ${this.dashboard?.window.timezone || this.appliedWindow.timezone}`; }
  updateTrend(): void {
    const slots = this.dashboard ? trendSlots(this.dashboard) : [];
    const option = this.trendSelection;
    const values = slots.map((slot) => slot.point ? slot.point[this.trendMetric] : null);
    const present = values.filter((value): value is number => value != null && Number.isFinite(value));
    this.trendLabels = slots.map((slot) => this.bucketLabel(slot.key));
    this.trendSeries = [{ label: option.label, color: option.color, values }];
    const total = !present.length ? null : option.peak ? Math.max(...present) : present.reduce((sum, value) => sum + value, 0);
    const activeSlots = present.filter((value) => value > 0).length;
    const bucket = this.dashboard?.trends.bucket === 'week' ? 'weeks' : 'days';
    this.trendSummary = {
      value: this.format(total, option.format),
      caption: `${option.peak ? 'peak' : 'total'} ${option.unit} · activity on ${this.format(activeSlots)} of ${this.format(slots.length)} ${bucket}`,
    };
  }

  bucketLabel(value: string | null): string {
    if (!value || !Number.isFinite(Date.parse(value))) return 'No date';
    return new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', month: 'short', day: 'numeric' }).format(new Date(value));
  }

  dateTime(value: string | null | undefined): string {
    if (!value || !Number.isFinite(Date.parse(value))) return 'No data';
    return new Intl.DateTimeFormat('en-US', { timeZone: this.appliedWindow.timezone,
      month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZoneName: 'short' }).format(new Date(value));
  }

  shortDateTime(value: string | null | undefined): string {
    if (!value || !Number.isFinite(Date.parse(value))) return 'No data';
    return new Intl.DateTimeFormat('en-US', { timeZone: this.appliedWindow.timezone,
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZoneName: 'short' }).format(new Date(value));
  }

  get windowLabel(): string {
    const window = this.dashboard?.window || this.appliedWindow;
    const options: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' };
    const formatDate = (value: string) => new Intl.DateTimeFormat('en-US', options).format(new Date(value));
    return `${formatDate(window.from)} – ${formatDate(new Date(Date.parse(window.to) - 1).toISOString())}`;
  }

  agentLabel(name: string): string { return name.replace(/_/g, ' '); }
  totalTokens(row: { input_tokens: number | null; output_tokens: number | null }): number | null {
    return row.input_tokens == null || row.output_tokens == null ? null : row.input_tokens + row.output_tokens;
  }
  rate(value: number | null, denominator: number | null): string {
    return denominator === 0 ? 'No data' : this.format(value, 'percent');
  }

  /** stroke-dasharray for a progress ring of radius `radius` filled to `rate` (0-1). */
  ringDash(rate: number | null, radius: number): string {
    const circumference = 2 * Math.PI * radius;
    return `${(Math.max(0, Math.min(1, rate ?? 0)) * circumference).toFixed(2)} ${circumference.toFixed(2)}`;
  }

  barsLabel(bars: HBar[]): string { return bars.map((bar) => `${bar.label}: ${bar.value}`).join(', '); }
  tokenSplitLabel(split: TokenSegment[]): string { return `Token split: ${split.map((segment) => `${segment.label} ${segment.value}`).join(', ')}`; }
  ownerNames(owners: Owner[]): string { return owners.map((owner) => owner.name).join(', '); }

  statusClass(value: string | null | undefined): string {
    switch ((value || '').toLowerCase()) {
      case 'succeeded': case 'success': case 'completed': case 'priced': return 'done';
      case 'failed': case 'error': return 'failed';
      case 'fallback': case 'unpriced': return 'pending';
      case 'running': return 'running';
      default: return 'neutral';
    }
  }

  scrollToTier(id: string): void {
    const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    this.host.nativeElement.querySelector<HTMLElement>(`#${id}`)?.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'start' });
  }

  openDataQuality(): void {
    const block = this.host.nativeElement.querySelector<HTMLDetailsElement>('#analytics-dq');
    if (block) block.open = true;
    this.scrollToTier('analytics-dq');
  }

  private updateActiveTier(): void {
    const scroller = this.scrollElement;
    if (!scroller) return;
    const tiers = Array.from(scroller.querySelectorAll<HTMLElement>('[data-tier]'));
    if (!tiers.length) return;
    const top = scroller.getBoundingClientRect().top + 120;
    const atBottom = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2;
    let active = tiers[0].id as TierId;
    if (atBottom) active = tiers[tiers.length - 1].id as TierId;
    else tiers.forEach((tier) => { if (tier.getBoundingClientRect().top <= top) active = tier.id as TierId; });
    if (active !== this.activeTier) this.zone.run(() => this.activeTier = active);
  }

  sortAgents(key: AgentSortKey): void {
    this.agentsSort = this.nextSort(this.agentsSort, key, key === 'agent_name' || key === 'model');
    this.buildAgentRows();
  }

  sortModels(key: ModelSortKey): void {
    this.modelsSort = this.nextSort(this.modelsSort, key, key === 'model');
    this.buildModelRows();
  }

  localSortState<Key extends string>(sort: SortState<Key>, key: Key): string {
    return sort.key !== key ? 'none' : sort.dir === 'asc' ? 'ascending' : 'descending';
  }

  private nextSort<Key extends string>(sort: SortState<Key>, key: Key, textual: boolean): SortState<Key> {
    if (sort.key === key) return { key, dir: sort.dir === 'asc' ? 'desc' : 'asc' };
    return { key, dir: textual ? 'asc' : 'desc' };
  }

  protected handleAccessError(error: unknown): boolean {
    const status = (error as { status?: number })?.status;
    if (status !== 401 && status !== 403) return false;
    if (this.denied) return true;
    this.denied = true;
    this.dashboardController?.abort();
    this.projectsController?.abort();
    this.projects = [];
    this.closeProject();
    this.cancelExport();
    this.dashboard = null;
    this.buildView();
    this.accessDenied.emit();
    return true;
  }

  protected errorMessage(error: unknown): string {
    return error instanceof Error ? error.message : 'Analytics could not be loaded. Please try again.';
  }

  async loadProjects(offset = this.pageOffset): Promise<void> {
    if (!this.isAdmin || !this.api.hasRole('admin') || this.denied || this.destroyed) return;
    this.projectsController?.abort();
    const controller = this.projectsController = new AbortController();
    this.pageOffset = offset;
    this.projectsLoading = true;
    this.projectsError = '';
    this.projects = [];
    this.filterProjects();
    try {
      const page = await this.api.getAnalyticsProjects({
        from: this.appliedWindow.from, to: this.appliedWindow.to, limit: this.pageSize, offset,
        sort: this.projectSort, order: this.projectOrder,
      }, controller.signal);
      if (controller.signal.aborted || this.destroyed || this.denied) return;
      if (offset >= page.pagination.total && offset > 0) {
        await this.loadProjects(Math.max(0, Math.floor((page.pagination.total - 1) / this.pageSize) * this.pageSize));
        return;
      }
      this.projects = page.items;
      this.projectTotal = page.pagination.total;
      this.pageOffset = page.pagination.offset;
      this.buildView();
    } catch (error) {
      if (!controller.signal.aborted && !this.destroyed && !this.handleAccessError(error)) {
        this.projectsError = this.errorMessage(error);
        this.projectTotal = 0;
      }
    } finally {
      if (this.projectsController === controller) this.projectsLoading = false;
    }
  }

  sortProjects(sort?: AnalyticsProjectSort): void {
    if (sort) {
      this.projectOrder = this.projectSort === sort && this.projectOrder === 'desc' ? 'asc' : 'desc';
      this.projectSort = sort;
    }
    this.cancelExport();
    void this.loadProjects(0);
  }

  sortState(sort: AnalyticsProjectSort): string {
    return this.projectSort !== sort ? 'none' : this.projectOrder === 'asc' ? 'ascending' : 'descending';
  }

  filterProjects(): void {
    const query = this.projectQuery.trim().toLowerCase();
    this.projectRows = !query ? this.projects : this.projects.filter((project) =>
      (project.owner_name || 'unassigned').toLowerCase().includes(query) || project.session_id.toLowerCase().includes(query));
    this.projectStats = {
      published: this.projects.filter((project) => project.published).length,
      owners: new Set(this.projects.map((project) => project.owner_name).filter(Boolean)).size,
    };
  }

  get projectRange(): string {
    return this.projectTotal === 0 ? '0 projects'
      : `${this.pageOffset + 1}-${this.pageOffset + this.projects.length} of ${this.format(this.projectTotal)} projects`;
  }

  cancelExport(): void {
    this.exportController?.abort();
    this.exportController = undefined;
    this.exporting = false;
  }

  async exportProjects(): Promise<void> {
    if (this.exporting || !this.isAdmin || !this.api.hasRole('admin') || this.denied || this.destroyed) return;
    const controller = this.exportController = new AbortController();
    const window = { ...this.appliedWindow };
    const sort = this.projectSort;
    const order = this.projectOrder;
    this.exporting = true;
    this.exportError = '';
    this.exportedRows = 0;
    this.exportTotal = this.projectTotal;
    try {
      const rows: AnalyticsProject[] = [];
      const ids = new Set<string>();
      let expectedTotal: number | undefined;
      do {
        const page = await this.api.getAnalyticsProjects({ from: window.from, to: window.to,
          sort, order, limit: 200, offset: rows.length }, controller.signal);
        if (controller.signal.aborted || this.destroyed || this.denied) return;
        if (expectedTotal !== undefined && page.pagination.total !== expectedTotal) throw new Error('Project activity changed during export. Please retry.');
        expectedTotal = page.pagination.total;
        if (!page.items.length && rows.length < expectedTotal) throw new Error('The project export was incomplete. Please retry.');
        for (const row of page.items) {
          if (ids.has(row.session_id)) throw new Error('Project ordering changed during export. Please retry.');
          ids.add(row.session_id);
          rows.push(row);
        }
        this.exportedRows = rows.length;
        this.exportTotal = expectedTotal;
      } while (rows.length < expectedTotal);
      if (!rows.length) throw new Error('No projects match this date window.');
      const url = URL.createObjectURL(new Blob(['﻿', projectsCsv(rows)], { type: 'text/csv;charset=utf-8;' }));
      const link = document.createElement('a');
      link.href = url;
      link.download = `analytics-projects-${window.from.slice(0, 10)}-${new Date(Date.parse(window.to) - 1).toISOString().slice(0, 10)}.csv`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      if (!controller.signal.aborted && !this.destroyed && !this.handleAccessError(error)) this.exportError = this.errorMessage(error);
    } finally {
      if (this.exportController === controller) this.exporting = false;
    }
  }

  async openProject(sessionId: string): Promise<void> {
    if (!this.isAdmin || !this.api.hasRole('admin') || this.denied || this.destroyed) return;
    this.detailController?.abort();
    const controller = this.detailController = new AbortController();
    const dialog = this.projectDialog.nativeElement;
    if (!dialog.open) this.returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : undefined;
    this.selectedProject = sessionId;
    this.projectDetail = null;
    this.detailLoading = true;
    this.detailError = '';
    this.detailTab = 'workflow';
    this.detailRunFilter = '';
    if (!dialog.open) dialog.showModal();
    try {
      const detail = await this.api.getAnalyticsProject(sessionId, controller.signal);
      if (!controller.signal.aborted && !this.destroyed && !this.denied) this.projectDetail = detail;
    } catch (error) {
      if (!controller.signal.aborted && !this.destroyed && !this.handleAccessError(error)) this.detailError = this.errorMessage(error);
    } finally {
      if (this.detailController === controller) this.detailLoading = false;
    }
  }

  closeProject(event?: Event): void {
    event?.preventDefault();
    this.detailController?.abort();
    if (this.projectDialog?.nativeElement.open) this.projectDialog.nativeElement.close();
    this.projectDetail = null;
    this.selectedProject = null;
    this.detailLoading = false;
    if (!this.destroyed && this.returnFocus?.isConnected) this.returnFocus.focus({ preventScroll: true });
    this.returnFocus = undefined;
  }

  onDialogClick(event: MouseEvent): void {
    if (event.target !== this.projectDialog.nativeElement) return;
    const bounds = this.projectDialog.nativeElement.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) this.closeProject();
  }

  onDetailKey(event: KeyboardEvent): void {
    const tabs = ['workflow', 'agents', 'ingestion'] as const;
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const index = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
      : (tabs.indexOf(this.detailTab) + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    this.detailTab = tabs[index];
    (event.currentTarget as HTMLElement).querySelector<HTMLButtonElement>(`[data-view="${this.detailTab}"]`)?.focus();
  }

  get filteredAgentRuns() {
    return (this.projectDetail?.agent_runs || []).filter((run) => !this.detailRunFilter || run.workflow_run_id === this.detailRunFilter);
  }

  workflowLabel(runId: string): string {
    const run = this.projectDetail?.workflow_runs.find((workflow) => workflow.run_id === runId);
    return run?.turn_index == null ? runId : `Turn ${run.turn_index}`;
  }

  formatBytes(value: number | null): string {
    if (value == null) return 'No data';
    if (value < 1024) return `${this.format(value)} B`;
    if (value < 1048576) return `${this.format(value / 1024)} KB`;
    return `${this.format(value / 1048576)} MB`;
  }

  /* ---------- View-model builders ---------- */

  private buildView(): void {
    this.filterProjects();
    const data = this.dashboard;
    if (!data) {
      this.view = null;
      this.agentRows = [];
      this.modelRows = [];
      this.rawGroups = [];
      return;
    }
    this.updateTrend();
    this.buildAgentRows();
    this.buildModelRows();
    this.buildDataQuality(data);
    const raw = flattenRaw(data);
    this.rawCount = raw.length;
    // Keys drop the group prefix (already shown as the group heading); the full path stays in the tooltip.
    this.rawGroups = [...new Set(raw.map((row) => row.group))].map((name) => ({ name, rows: raw.filter((row) => row.group === name)
      .map((row) => ({ ...row, key: row.path.startsWith(`${name}.`) ? row.path.slice(name.length + 1) : row.path })) }));
    const { overview, cost, efficiency } = data;
    const hours = efficiency.estimated_hours_saved;
    const perHour = cost.total_cost_usd.value != null && hours.value ? cost.total_cost_usd.value / hours.value : null;
    this.view = {
      ...this.buildOverview(data),
      cost: {
        perSlide: this.format(cost.cost_per_slide_usd.value, 'currency'),
        perProject: this.format(cost.cost_per_project_usd.value, 'currency'),
        total: this.format(cost.total_cost_usd.value, 'currency'),
        calls: this.format(cost.llm_calls.value),
        perHour: this.format(perHour, 'currency'),
        details: [
          this.kv('Total AI cost', cost.total_cost_usd, 'currency'),
          this.kv('Cost per slide', cost.cost_per_slide_usd, 'currency'),
          this.kv('Cost per project', cost.cost_per_project_usd, 'currency'),
          this.kv('Cost per AI call', cost.cost_per_call_usd, 'currency'),
          this.kv('AI calls', cost.llm_calls),
          { label: 'Cost per hour saved', value: this.format(perHour, 'currency'), type: 'estimated' },
        ],
        note: `Cost per hour saved = total AI cost ÷ ${this.format(hours.value, 'hours')} estimated hours saved `
          + `(${efficiency.estimate?.confidence || 'unknown'} confidence). Total cost covers all activity in the window: ${this.format(overview.total_ai_cost_usd.value, 'currency')}.`,
      },
      adoption: this.buildAdoption(data),
      performance: this.buildPerformance(data),
      quality: this.buildQuality(data),
      tokens: this.buildTokens(data),
    };
  }

  private buildOverview(data: AnalyticsDashboard): Pick<AnalyticsView, 'slides' | 'published' | 'people' | 'hours' | 'reliability'> {
    const { overview, quality, efficiency } = data;
    const slots = trendSlots(data);
    const daily = slots.map((slot) => slot.point?.slides_delivered ?? null);
    const activeDays = daily.filter((value) => value != null && value > 0).length;
    const successful = overview.successful_projects.value;
    const worked = overview.projects_worked_on.value;
    const publishRate = worked && successful != null ? successful / worked : null;
    const owners = this.ownerSummary();
    const firstNames = owners.map((owner) => owner.name.split(/\s+/)[0]);
    const namesText = firstNames.length > 1 ? `${firstNames.slice(0, -1).join(', ')} and ${firstNames[firstNames.length - 1]}` : firstNames[0];
    const leverage = efficiency.time_leverage.value;
    const agentShare = leverage && leverage > 0 ? 100 / leverage : 0;
    const confidence = efficiency.estimate?.confidence || 'unknown';
    const failure = quality.agent_failure_rate;
    const reliability = failure.value == null ? null : 1 - failure.value;
    const completed = failure.of != null && failure.count != null ? failure.of - failure.count : null;
    const precise = (value: number | null) => value == null ? 'No data'
      : `${new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value * 100)}%`;
    return {
      slides: {
        value: this.format(overview.slides_delivered.value),
        spark: this.sparkBars(daily),
        sparkLabel: `Slides delivered per ${data.trends.bucket}: ${this.format(overview.slides_delivered.value)} total across ${slots.length} ${data.trends.bucket}s`,
        details: [
          this.kv('Slides delivered', overview.slides_delivered),
          this.kv('Workflow runs', overview.workflow_runs),
          { label: `${data.trends.bucket === 'week' ? 'Weeks' : 'Days'} with activity`, value: `${activeDays} of ${slots.length}`, type: 'derived' },
        ],
      },
      published: {
        value: this.format(successful), of: `of ${this.format(worked)}`, rate: publishRate,
        rateText: this.format(publishRate, 'percent'),
        published: this.format(successful),
        notPublished: this.format(successful != null && worked != null ? worked - successful : null),
        details: [
          this.kv('Successful projects', overview.successful_projects),
          this.kv('Projects worked on', overview.projects_worked_on),
          { label: 'Publish rate', value: this.format(publishRate, 'percent'), type: 'derived' },
        ],
      },
      people: {
        value: this.format(overview.active_users.value),
        caption: owners.length ? `${namesText} built content this period` : 'Team members who built content this period',
        owners,
        details: [
          this.kv('Active users', overview.active_users),
          ...owners.map((owner) => ({ label: owner.name,
            value: `${owner.sessions} ${owner.sessions === 1 ? 'session' : 'sessions'} · ${this.format(owner.slides)} slides` })),
        ],
      },
      hours: {
        value: `≈ ${this.format(efficiency.estimated_hours_saved.value == null ? null : Math.round(efficiency.estimated_hours_saved.value))}`,
        caption: leverage ? `Roughly ${this.format(leverage, 'leverage')} faster than doing it by hand` : 'Compared with a manual-production baseline',
        confidence, agentShare: Math.max(2, Math.min(100, agentShare)),
        agentShareText: `Agents take about ${Math.round(agentShare)}% of the manual time`,
        details: [
          this.kv('Estimated hours saved', efficiency.estimated_hours_saved, 'hours'),
          this.kv('Time leverage', efficiency.time_leverage, 'leverage'),
          this.kv('Hours saved per dollar', efficiency.hours_saved_per_dollar),
          { label: 'Confidence', value: `${confidence} · baseline ${efficiency.estimate?.baseline_version || 'unknown'}` },
        ],
        note: 'Modelled against a manual-production baseline. Directional only, not audited labor savings.',
      },
      reliability: {
        value: precise(reliability), rate: reliability,
        caption: completed == null ? 'Share of agent runs that completed without a failure'
          : `Agents completed ${this.format(completed)} of ${this.format(failure.of)} runs without a failure`,
        details: [
          this.kv('Agent runs', quality.agent_runs),
          { label: 'Failed runs', value: `${this.format(failure.count)} (${this.format(failure.value, 'percent')})`, type: failure.type },
          { label: 'Fallbacks', value: `${this.format(quality.fallback_rate.count)} (${this.format(quality.fallback_rate.value, 'percent')})`, type: quality.fallback_rate.type },
          { label: 'Reliability', value: precise(reliability), type: 'derived' },
        ],
      },
    };
  }

  private buildAdoption(data: AnalyticsDashboard): AnalyticsView['adoption'] {
    const { adoption } = data;
    const rate = adoption.multi_turn_rate;
    const distribution = new Map<number, number>();
    this.projects.forEach((project) => { if (project.turns != null) distribution.set(project.turns, (distribution.get(project.turns) || 0) + 1); });
    const rows = [...distribution].sort((first, second) => first[0] - second[0]);
    const maxCount = Math.max(1, ...rows.map(([, count]) => count));
    const plural = (count: number, noun: string) => `${this.format(count)} ${noun}${count === 1 ? '' : 's'}`;
    return {
      stats: [
        { label: 'Multi-turn projects', value: this.format(rate.value, 'percent'),
          caption: rate.count != null && rate.of != null ? `${this.format(rate.count)} of ${this.format(rate.of)} projects came back for another turn` : '' },
        { label: 'Median turns per project', value: this.format(adoption.median_turns_per_project.value), caption: 'Typical number of rounds' },
        { label: 'Average turns per project', value: this.format(adoption.avg_turns_per_project.value), caption: 'Mean rounds per project' },
      ],
      multiTurn: [{ label: 'Multi-turn', value: `${this.format(rate.count)} of ${this.format(rate.of)}`, width: this.clampPercent((rate.value ?? 0) * 100) }],
      turns: rows.map(([turns, count]) => ({ label: plural(turns, 'turn'), value: plural(count, 'project'),
        width: this.clampPercent(count / maxCount * 100), tone: turns > 1 ? undefined : 'teal' })),
      turnsLabel: this.projectTotal > this.projects.length ? `Turns per project · current page (${this.projects.length} of ${this.format(this.projectTotal)})` : 'Turns per project',
      details: [
        this.kv('Projects', adoption.projects),
        this.kv('Multi-turn projects', adoption.multi_turn_projects),
        this.kv('Multi-turn rate', rate, 'percent'),
        this.kv('Median turns per project', adoption.median_turns_per_project),
        this.kv('Average turns per project', adoption.avg_turns_per_project),
      ],
      note: rows.length ? 'Turn distribution is counted on the page from the loaded projects.' : '',
    };
  }

  private buildPerformance(data: AnalyticsDashboard): AnalyticsView['performance'] {
    const turnaround = data.performance.ai_turnaround_ms;
    const processing = data.performance.agent_processing_ms;
    const ratio = turnaround.median && turnaround.p95 != null ? turnaround.p95 / turnaround.median : null;
    const parallel = turnaround.total && processing.total != null ? processing.total / turnaround.total : null;
    const maxTotal = Math.max(turnaround.total ?? 0, processing.total ?? 0);
    const share = (value: number | null) => maxTotal > 0 && value != null ? this.clampPercent(value / maxTotal * 100, 1) : 0;
    const exact = (value: number | null) => value == null ? 'No data' : `${new Intl.NumberFormat('en-US').format(value)} ms`;
    return {
      stats: [
        { label: 'Median wait per turn', value: this.format(turnaround.median, 'duration'), caption: 'Typical turn' },
        { label: 'Slowest 5% of turns (p95)', value: this.format(turnaround.p95, 'duration'), caption: ratio ? `${this.format(ratio, 'leverage')} the median` : '' },
      ],
      wait: [
        { label: 'Median', value: this.format(turnaround.median, 'duration'),
          width: turnaround.p95 ? this.clampPercent((turnaround.median ?? 0) / turnaround.p95 * 100, 1) : 0 },
        { label: 'p95', value: this.format(turnaround.p95, 'duration'), width: turnaround.p95 ? 100 : 0, tone: 'teal' },
      ],
      active: [
        { label: 'People waited', value: this.format(turnaround.total, 'duration'), width: share(turnaround.total) },
        { label: 'Agents active', value: this.format(processing.total, 'duration'), width: share(processing.total), tone: 'gold' },
      ],
      parallelNote: parallel && parallel > 1 ? `Agents were active ${this.format(parallel, 'leverage')} longer than people waited, because work ran in parallel.` : '',
      details: [
        { label: 'Median AI turnaround', value: exact(turnaround.median), type: turnaround.type },
        { label: 'p95 AI turnaround', value: exact(turnaround.p95), type: turnaround.type },
        { label: 'Total AI turnaround', value: exact(turnaround.total), type: turnaround.type },
        { label: 'Agent processing (summed)', value: exact(processing.total), type: processing.type },
        { label: 'p95 ÷ median', value: this.format(ratio, 'leverage'), type: 'derived' },
        { label: 'Agent-active ÷ wall-clock', value: this.format(parallel, 'leverage'), type: 'derived' },
      ],
      note: [turnaround.note && `AI turnaround: ${turnaround.note}.`, processing.note && `Agent processing: ${processing.note}.`].filter(Boolean).join(' '),
    };
  }

  private buildQuality(data: AnalyticsDashboard): AnalyticsView['quality'] {
    const { quality } = data;
    const { feedback } = quality;
    const ratio = (metric: AnalyticsMetric, noun: string) => metric.count == null || metric.of == null ? ''
      : `${this.format(metric.count)} of ${this.format(metric.of)} ${noun}`;
    const counted = (metric: AnalyticsMetric) => `${this.format(metric.count)} of ${this.format(metric.of)} (${this.format(metric.value, 'percent')})`;
    return {
      stats: [
        { label: 'Fallback rate', value: this.format(quality.fallback_rate.value, 'percent'), caption: ratio(quality.fallback_rate, 'runs') },
        { label: 'Agent failure rate', value: this.format(quality.agent_failure_rate.value, 'percent'), caption: ratio(quality.agent_failure_rate, 'runs') },
        { label: 'Rework rate', value: this.format(quality.rework_rate.value, 'percent'), caption: ratio(quality.rework_rate, 'published projects') },
      ],
      noRatings: !(feedback.rated_versions.value && feedback.rated_versions.value > 0),
      feedback: [
        { label: 'Thumbs-up rate', value: this.format(feedback.thumbs_up_rate.value, 'percent'), caption: ratio(feedback.thumbs_up_rate, 'rated') },
        { label: 'Comment coverage', value: this.format(feedback.comment_coverage.value, 'percent'), caption: ratio(feedback.comment_coverage, 'rated') },
      ],
      details: [
        this.kv('Agent runs', quality.agent_runs),
        { label: 'Fallbacks', value: counted(quality.fallback_rate), type: quality.fallback_rate.type },
        { label: 'Failures', value: counted(quality.agent_failure_rate), type: quality.agent_failure_rate.type },
        { label: 'Rework', value: counted(quality.rework_rate), type: quality.rework_rate.type },
        this.kv('Rated versions', feedback.rated_versions),
        { label: 'Thumbs up / down', value: `${this.format(feedback.thumbs_up.value)} / ${this.format(feedback.thumbs_down.value)}`, type: feedback.thumbs_up.type },
        this.kv('Thumbs-up rate', feedback.thumbs_up_rate, 'percent'),
        this.kv('Comment coverage', feedback.comment_coverage, 'percent'),
      ],
    };
  }

  private buildTokens(data: AnalyticsDashboard): AnalyticsView['tokens'] {
    const { cost } = data;
    const input = cost.input_tokens.value ?? 0;
    const output = cost.output_tokens.value ?? 0;
    const cached = cost.cached_input_tokens.value ?? 0;
    const total = cost.total_tokens.value ?? input + output + cached;
    const segment = (label: string, color: string, value: number): TokenSegment => {
      const share = total > 0 ? value / total * 100 : 0;
      return { label, color, width: share, value: this.format(value, 'compact'), share: `${this.format(share, 'number')}%` };
    };
    return {
      stats: [
        { label: 'Total tokens', value: this.format(total, 'compact'), caption: `${this.format(cost.llm_calls.value)} AI calls` },
        { label: 'Tokens per slide', value: this.format(cost.tokens_per_slide.value, 'compact'),
          caption: cost.tokens_per_slide.denominator != null ? `across ${this.format(cost.tokens_per_slide.denominator)} slides` : '' },
        { label: 'Cost per call', value: this.format(cost.cost_per_call_usd.value, 'currency'),
          caption: cost.cost_per_call_usd.denominator != null ? `across ${this.format(cost.cost_per_call_usd.denominator)} calls` : '' },
      ],
      split: [
        segment('Input', '--analytics-chart-blue', input),
        segment('Output', '--analytics-chart-teal', output),
        segment('Cached input', '--analytics-chart-gold', cached),
      ],
      details: [
        this.kv('Input tokens', cost.input_tokens),
        this.kv('Output tokens', cost.output_tokens),
        this.kv('Cached input tokens', cost.cached_input_tokens),
        this.kv('Total tokens', cost.total_tokens),
        this.kv('LLM calls', cost.llm_calls),
        this.kv('Tokens per slide', cost.tokens_per_slide),
        this.kv('Tokens per call', cost.tokens_per_call),
        this.kv('Cost per call', cost.cost_per_call_usd, 'currency'),
      ],
    };
  }

  private buildAgentRows(): void {
    const agents = this.dashboard?.agents || [];
    this.agentRows = sortRows(agents, this.agentsSort, (agent, key) => key === 'tokens' ? this.totalTokens(agent) : agent[key]);
    this.agentRunTotal = agents.reduce((sum, agent) => sum + (agent.runs ?? 0), 0);
    this.agentsBelowTarget = agents.filter((agent) => agent.success_rate != null && agent.success_rate < 1).map((agent) => this.agentLabel(agent.agent_name));
  }

  private buildModelRows(): void {
    const models = this.dashboard?.models || [];
    const totalCost = models.reduce((sum, model) => sum + (model.cost_usd ?? 0), 0);
    const ranked = [...models].sort((first, second) => (second.cost_usd ?? 0) - (first.cost_usd ?? 0));
    const rows = ranked.map((model, index): ModelRow => ({ model, color: MODEL_COLORS[Math.min(index, MODEL_COLORS.length - 1)],
      share: totalCost > 0 ? (model.cost_usd ?? 0) / totalCost * 100 : null }));
    this.modelsTotalCost = totalCost;
    this.topModel = rows.length && totalCost > 0 ? rows[0] : null;
    this.modelRows = sortRows(rows, this.modelsSort, (row, key) => key === 'share' ? row.share : key === 'tokens' ? this.totalTokens(row.model) : row.model[key]);
  }

  private buildDataQuality(data: AnalyticsDashboard): void {
    const quality = data.data_quality;
    this.dataQuality = [
      { label: 'unpriced agent runs', count: quality.unpriced_agent_runs, affects: 'Cost totals',
        why: 'Agent runs whose model has no price list entry, so their cost could not be calculated.' },
      { label: 'missing page counts', count: quality.missing_page_counts, affects: 'Slides delivered',
        why: 'Successful runs that did not report how many slides they produced.' },
      { label: 'dropped events', count: quality.dropped_metric_events, affects: 'Every metric on this page',
        why: 'Metric events the pipeline rejected before they were stored.' },
    ];
    this.dataQualityFlagged = this.dataQuality.filter((item) => (item.count ?? 0) > 0);
    this.dataQualityTotal = this.dataQualityFlagged.reduce((sum, item) => sum + (item.count ?? 0), 0);
  }

  private ownerSummary(): Owner[] {
    const owners = new Map<string, Owner>();
    this.projects.forEach((project) => {
      if (!project.owner_name) return;
      const owner = owners.get(project.owner_name) || { name: project.owner_name, initials: this.initials(project.owner_name), sessions: 0, slides: 0 };
      owner.sessions += 1;
      owner.slides += project.slides_delivered ?? 0;
      owners.set(project.owner_name, owner);
    });
    return [...owners.values()].sort((first, second) => second.slides - first.slides || first.name.localeCompare(second.name));
  }

  private initials(name: string): string {
    return name.trim().split(/\s+/).slice(0, 2).map((word) => word[0]).join('').toUpperCase();
  }

  /** Mini bar sparkline: one bar per bucket; buckets without data stay as faint ticks. */
  private sparkBars(values: (number | null)[]): SparkRect[] {
    const gap = 1.5;
    const count = Math.max(1, values.length);
    const width = Math.max(1.5, (SPARK_WIDTH - gap * (count - 1)) / count);
    const max = Math.max(0, ...values.filter((value): value is number => value != null));
    return values.map((value, index) => {
      const x = index * (width + gap);
      if (value == null) return { x, y: SPARK_HEIGHT - 2, width, height: 2, kind: 'empty' };
      const height = value > 0 && max > 0 ? Math.max(3, value / max * (SPARK_HEIGHT - 2)) : 2;
      return { x, y: SPARK_HEIGHT - height, width, height, kind: value > 0 ? 'bar' : 'zero' };
    });
  }

  private clampPercent(value: number, minimum = 0): number {
    return Number.isFinite(value) ? Math.max(value > 0 ? minimum : 0, Math.min(100, value)) : 0;
  }

  private kv(label: string, metric: AnalyticsMetric | undefined, format: MetricFormat = 'number'): KvRow {
    return { label, value: this.format(metric?.value, format), type: metric?.type };
  }
}
