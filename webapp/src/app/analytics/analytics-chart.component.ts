import { AfterViewInit, Component, ElementRef, Input, NgZone, OnChanges, OnDestroy, ViewChild } from '@angular/core';
import { BarController, BarElement, CategoryScale, Chart, Legend, LinearScale, Tooltip } from 'chart.js';
import { MetricFormat } from '../models/analytics.models';
import { formatAnalyticsValue } from './analytics.helpers';

Chart.register(BarController, BarElement, CategoryScale, LinearScale, Tooltip, Legend);

export interface AnalyticsChartSeries {
  label: string;
  values: (number | null)[];
  color: string;
  outline?: boolean;
}

@Component({
  selector: 'app-analytics-chart',
  standalone: true,
  template: '<canvas #canvas role="img" [attr.aria-label]="label"></canvas>',
  styles: [':host { display: block; position: relative; width: 100%; height: 100%; min-width: 0; } canvas { display: block; }'],
})
export class AnalyticsChartComponent implements AfterViewInit, OnChanges, OnDestroy {
  @Input() labels: string[] = [];
  @Input() series: AnalyticsChartSeries[] = [];
  @Input() label = '';
  @Input() format: MetricFormat = 'number';
  @Input() stacked = false;
  @Input() horizontal = false;
  @Input() showAllLabels = false;
  @Input() showLegend?: boolean;
  @Input() maxBarThickness = 32;
  @Input() tooltipNotes: string[] = [];
  @ViewChild('canvas', { static: true }) canvas!: ElementRef<HTMLCanvasElement>;
  private chart?: Chart<'bar'>;
  private observer?: MutationObserver;
  private ready = false;

  constructor(private zone: NgZone) {}

  ngAfterViewInit(): void {
    this.ready = true;
    this.zone.runOutsideAngular(() => {
      this.observer = new MutationObserver(() => this.render());
      this.observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'data-theme-color', 'style'] });
      this.render();
    });
  }

  ngOnChanges(): void {
    if (this.ready) this.zone.runOutsideAngular(() => this.render());
  }

  ngOnDestroy(): void {
    this.observer?.disconnect();
    this.chart?.destroy();
  }

  private render(): void {
    this.chart?.destroy();
    const theme = getComputedStyle(document.documentElement);
    const color = (token: string) => theme.getPropertyValue(token).trim();
    const numericAxis = {
      beginAtZero: true, stacked: this.stacked, border: { display: false },
      grid: { color: color('--border') },
      ticks: {
        color: color('--muted'), maxTicksLimit: this.horizontal ? 3 : 5,
        callback: (value: string | number) => this.format === 'number'
          ? new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 }).format(Number(value))
          : formatAnalyticsValue(Number(value), this.format),
      },
    };
    const categoryAxis = {
      stacked: this.stacked, border: { display: false }, grid: { display: false },
      ticks: {
        color: color('--muted'), maxRotation: 0, autoSkip: !this.showAllLabels,
        maxTicksLimit: this.showAllLabels ? undefined : this.horizontal ? 8 : 7,
        callback: (value: string | number) => {
          const label = this.labels[Number(value)] || '';
          if (!this.horizontal) return label;
          const maxCharacters = Math.max(10, Math.min(32, Math.floor(this.canvas.nativeElement.clientWidth * .38 / 6.5)));
          return label.split(' / ').map((part) => part.length > maxCharacters
            ? `${part.slice(0, maxCharacters - 3)}...` : part);
        },
      },
    };
    this.chart = new Chart(this.canvas.nativeElement, {
      type: 'bar',
      data: {
        labels: this.labels,
        datasets: this.series.map((series) => ({
          label: series.label, data: series.values,
          backgroundColor: series.outline ? 'transparent' : color(series.color),
          hoverBackgroundColor: series.outline ? 'transparent' : color(series.color),
          borderColor: color(series.color), borderWidth: series.outline ? 1.5 : 0,
          borderSkipped: false, maxBarThickness: this.maxBarThickness,
        })),
      },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        indexAxis: this.horizontal ? 'y' : 'x',
        interaction: { intersect: false, mode: 'index', axis: this.horizontal ? 'y' : 'x' },
        plugins: {
          legend: { display: this.showLegend ?? this.series.length > 1, position: 'bottom',
            labels: { color: color('--text'), boxWidth: 10, boxHeight: 10, padding: 18 } },
          tooltip: {
            position: 'nearest',
            backgroundColor: color('--panel-3'), titleColor: color('--text'), bodyColor: color('--text'),
            borderColor: color('--border'), borderWidth: 1,
            callbacks: {
              title: (items) => (items[0]?.label || '').match(/.{1,32}/g) || [],
              label: (context) => `${context.dataset.label}: ${formatAnalyticsValue(context.raw as number | null, this.format)}`,
              afterBody: (items) => items.length ? this.tooltipNotes[items[0].dataIndex] || '' : '',
            },
          },
        },
        scales: { x: this.horizontal ? numericAxis : categoryAxis, y: this.horizontal ? categoryAxis : numericAxis },
      },
    });
  }
}