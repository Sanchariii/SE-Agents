import { AfterViewChecked, Component, ElementRef, NgZone, OnDestroy, OnInit, QueryList, ViewChild, ViewChildren } from '@angular/core';
import { ChatMessage, Session, SessionDoc, StreamEvent } from '../models/models';
import { DECK_FEEDBACK_COMMENT_MAX, DeckFeedbackState, LiveTurn, SlideInfo, StudioOutputRow, WorkspacePresentationEvent } from '../models/workspace.models';
import { MarkdownService } from '../shared/services/markdown.service';
import { userBubbleText } from '../shared/questions.helpers';
import { UiService } from '../shared/services/ui.service';
import { ACCEPTED_UPLOAD_EXTENSIONS, MAX_UPLOAD_BYTES, WorkspaceService } from '../shared/services/workspace.service';
import { CHAT_SUGGESTIONS } from './chat-suggestions';
import { docStatusClass, docStatusLabel, docStatusText, docTitle, isDocProcessing } from './document.helpers';
import {
  buildTurnFromMessage, FLOW_STAGES, flowMeterTicks, flowPercent, flowStageStatus, flowSummary, formatDuration, langCode,
  langsArr, showLangBuilding, showLangSection,
  slideChipTitle, slidesArr, slidesDoneCount, slidesProgressLabel, stepDot, stepState, thoughtIcon, thoughtIconClass,
  thoughtMsgClass,
} from './flow.helpers';
import { init as initPptxPreview } from 'pptx-preview';

export interface ThemeColorOption {
  id: string;
  name: string;
  primary: string;
  hover: string;
  primaryText: string;
  soft: string;
  accentText: string;
}

export interface ChatStyleOption {
  id: string;
  name: string;
  description: string;
  userBg: string;
  userText: string;
  userBorder: string;
  agentBg: string;
  agentText: string;
  agentBorder: string;
}

export const THEME_COLOR_OPTIONS: ThemeColorOption[] = [
  {
    id: 'lavender',
    name: 'Lilac Lavender',
    primary: '#DFC5FE',
    hover: '#CEAAFD',
    primaryText: '#241442',
    soft: 'rgba(223, 197, 254, 0.28)',
    accentText: '#7c3aed',
  },
  {
    id: 'coral',
    name: 'Sunset Coral',
    primary: '#FF8762',
    hover: '#EA6C46',
    primaryText: '#FFFFFF',
    soft: 'rgba(255, 135, 98, 0.25)',
    accentText: '#EA6C46',
  },
  {
    id: 'sky',
    name: 'Ocean Sky',
    primary: '#7DD3FC',
    hover: '#38BDF8',
    primaryText: '#0C4A6E',
    soft: 'rgba(125, 211, 252, 0.28)',
    accentText: '#0284C7',
  },
  {
    id: 'mint',
    name: 'Emerald Mint',
    primary: '#6EE7B7',
    hover: '#34D399',
    primaryText: '#064E3B',
    soft: 'rgba(110, 231, 183, 0.28)',
    accentText: '#059669',
  },
  {
    id: 'rose',
    name: 'Rose Blossom',
    primary: '#F472B6',
    hover: '#EC4899',
    primaryText: '#831843',
    soft: 'rgba(244, 114, 182, 0.28)',
    accentText: '#DB2777',
  },
  {
    id: 'indigo',
    name: 'Royal Indigo',
    primary: '#A5B4FC',
    hover: '#818CF8',
    primaryText: '#1E1B4B',
    soft: 'rgba(165, 180, 252, 0.28)',
    accentText: '#4F46E5',
  },
  {
    id: 'amber',
    name: 'Warm Amber',
    primary: '#FDE047',
    hover: '#FACC15',
    primaryText: '#713F12',
    soft: 'rgba(253, 224, 71, 0.28)',
    accentText: '#CA8A04',
  },
];

export const DARK_MODE_THEME_COLOR: ThemeColorOption = {
  id: 'dark-indigo',
  name: 'Vibrant Indigo',
  primary: '#6366f1',
  hover: '#818cf8',
  primaryText: '#ffffff',
  soft: 'rgba(99, 102, 241, 0.20)',
  accentText: '#818cf8',
};

export const CHAT_STYLE_OPTIONS: ChatStyleOption[] = [
  {
    id: 'slate',
    name: 'Cool Slate & Pure White',
    description: 'Modern standard with high contrast and readability',
    userBg: '#EEF2F6',
    userText: '#0F172A',
    userBorder: '#D8E0EA',
    agentBg: '#FFFFFF',
    agentText: '#0F172A',
    agentBorder: '#E2E8F0',
  },
  {
    id: 'lavender',
    name: 'Lilac & Soft Pearl',
    description: 'Subtle pastel purple prompt with clean pearl card',
    userBg: '#F3E8FF',
    userText: '#3B0764',
    userBorder: '#E9D5FF',
    agentBg: '#FFFFFF',
    agentText: '#1E1B4B',
    agentBorder: '#E5E7EB',
  },
  {
    id: 'sky',
    name: 'Sky Azure & Cloud',
    description: 'Fresh light blue prompt with crisp white response',
    userBg: '#E0F2FE',
    userText: '#0369A1',
    userBorder: '#BAE6FD',
    agentBg: '#FFFFFF',
    agentText: '#0C4A6E',
    agentBorder: '#E2E8F0',
  },
  {
    id: 'mint',
    name: 'Sage Mint & Pure White',
    description: 'Natural soothing green tone with clean white card',
    userBg: '#ECFDF5',
    userText: '#065F46',
    userBorder: '#A7F3D0',
    agentBg: '#FFFFFF',
    agentText: '#064E3B',
    agentBorder: '#E2E8F0',
  },
  {
    id: 'sand',
    name: 'Warm Sand & Paper',
    description: 'Editorial cozy parchment prompt with crisp response',
    userBg: '#F5F0E8',
    userText: '#3D2D1D',
    userBorder: '#E8DEC8',
    agentBg: '#FFFFFF',
    agentText: '#261C14',
    agentBorder: '#EAE5DB',
  },
  {
    id: 'midnight',
    name: 'Midnight & Pure White',
    description: 'ChatGPT-style dark slate prompt with clean response',
    userBg: '#1E293B',
    userText: '#F8FAFC',
    userBorder: '#334155',
    agentBg: '#FFFFFF',
    agentText: '#0F172A',
    agentBorder: '#E2E8F0',
  },
];

export interface FontOption {
  id: string;
  name: string;
  description: string;
  /** Full font-family stack applied to `--app-font` at :root. */
  stack: string;
  /** Short preview text shown inside the option card. */
  sample: string;
}

export const FONT_OPTIONS: FontOption[] = [
  {
    id: 'inter',
    name: 'Inter',
    description: 'Modern neutral sans — crisp UI default',
    stack: '"Inter", "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, Roboto, Helvetica, Arial, sans-serif',
    sample: 'Aa Bb Cc 123',
  },
  {
    id: 'roboto',
    name: 'Roboto',
    description: 'Friendly geometric sans with wide character set',
    stack: '"Roboto", "Segoe UI", system-ui, -apple-system, sans-serif',
    sample: 'Aa Bb Cc 123',
  },
  {
    id: 'poppins',
    name: 'Poppins',
    description: 'Geometric rounded sans with a bold personality',
    stack: '"Poppins", "Segoe UI", system-ui, -apple-system, sans-serif',
    sample: 'Aa Bb Cc 123',
  },
  {
    id: 'nunito',
    name: 'Nunito',
    description: 'Rounded, warm sans — great for long reading',
    stack: '"Nunito", "Segoe UI", system-ui, -apple-system, sans-serif',
    sample: 'Aa Bb Cc 123',
  },
  {
    id: 'source-sans',
    name: 'Source Sans 3',
    description: "Adobe's clean sans — professional and legible",
    stack: '"Source Sans 3", "Segoe UI", system-ui, -apple-system, sans-serif',
    sample: 'Aa Bb Cc 123',
  },
  {
    id: 'system',
    name: 'System UI',
    description: 'Native OS font — Segoe UI / SF Pro / Roboto',
    stack: 'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif',
    sample: 'Aa Bb Cc 123',
  },
];

/** Owns studio rendering only; inherits the shell's WorkspaceService (never provide it here). */
@Component({
  selector: 'app-training-studio',
  templateUrl: './training-studio.component.html',
  styleUrls: ['./training-studio.component.scss'],
})
export class TrainingStudioComponent implements OnInit, AfterViewChecked, OnDestroy {
  readonly chatSuggestions = CHAT_SUGGESTIONS;
  readonly FLOW_STAGES = FLOW_STAGES;
  readonly thoughtIcon = thoughtIcon;
  readonly thoughtIconClass = thoughtIconClass;
  readonly thoughtMsgClass = thoughtMsgClass;
  readonly flowSummary = flowSummary;
  readonly flowPercent = flowPercent;
  readonly flowMeterTicks = flowMeterTicks;
  readonly flowStageStatus = flowStageStatus;
  readonly showLangSection = showLangSection;
  readonly stepState = stepState;
  readonly stepDot = stepDot;
  readonly slidesArr = slidesArr;
  readonly slidesDoneCount = slidesDoneCount;
  readonly slidesProgressLabel = slidesProgressLabel;
  readonly slideChipTitle = slideChipTitle;
  readonly langsArr = langsArr;
  readonly langCode = langCode;
  readonly showLangBuilding = showLangBuilding;
  readonly formatDuration = formatDuration;
  private readonly thoughtTurnCache = new WeakMap<ChatMessage, LiveTurn>();
  readonly docTitle = docTitle;
  readonly docStatusLabel = docStatusLabel;
  readonly docStatusText = docStatusText;
  readonly docStatusClass = docStatusClass;
  readonly isDocProcessing = isDocProcessing;
  readonly themeColorOptions = THEME_COLOR_OPTIONS;
  readonly chatStyleOptions = CHAT_STYLE_OPTIONS;
  readonly fontOptions = FONT_OPTIONS;
  readonly acceptedFileFormats = ACCEPTED_UPLOAD_EXTENSIONS.map((ext) => ext.replace('.', '').toUpperCase());
  readonly maxUploadSizeLabel = `${Math.round(MAX_UPLOAD_BYTES / (1024 * 1024))} MB`;

  isSidebarCollapsed = false;
  isProjectsExpanded = false;
  isStudioCollapsed = false;
  // User-dragged panel widths in px; null falls back to the CSS default for the breakpoint.
  sidebarWidth: number | null = null;
  studioWidth: number | null = null;
  @ViewChild('chatLayoutEl') private chatLayoutRef?: ElementRef<HTMLElement>;
  isOutputsExpanded = false;
  isAppearanceOpen = false;
  selectedThemeId = 'lavender';
  selectedChatStyleId = 'slate';
  selectedFontId = 'inter';
  isDarkMode = false;
  readonly darkModeTheme = DARK_MODE_THEME_COLOR;
  private themeObserver?: MutationObserver;

  get currentTheme(): ThemeColorOption {
    return this.themeColorOptions.find((t) => t.id === this.selectedThemeId) || this.themeColorOptions[0];
  }

  private pendingScrollMessages = true;
  private pendingScrollIngest = true;
  private pendingRestoreScrollHeight: number | null = null;
  private pendingComposerResize = false;
  private pendingComposerFocus = false;
  private composerTimer?: ReturnType<typeof setTimeout>;
  private detachViewPort?: () => void;
  private destroyed = false;
  private composerRef?: ElementRef<HTMLTextAreaElement>;

  @ViewChild('messagesEl') private messagesElRef?: ElementRef<HTMLElement>;
  @ViewChild('ingestListEl') private ingestListRef?: ElementRef<HTMLElement>;
  @ViewChild('fileInputEl') private fileInputRef?: ElementRef<HTMLInputElement>;
  @ViewChildren('logListEl') private logListEls?: QueryList<ElementRef<HTMLElement>>;

  // In-app slide preview (renders a .pptx into #pptxWrapperEl without downloading).
  @ViewChild('pptxWrapperEl') private pptxWrapperRef?: ElementRef<HTMLDivElement>;
  @ViewChild('previewStageEl') private previewStageRef?: ElementRef<HTMLDivElement>;
  previewOpen = false;
  previewLoading = false;
  previewError = '';
  previewTitle = 'Deck preview';
  previewLanguages: string[] = [];
  previewLanguage: string | null = null;
  previewSlideCount = 0;
  previewCurrent = 1;
  private previewSid: string | null = null;
  private previewVersion: number | null = null;
  private previewBuffer: ArrayBuffer | null = null;
  private previewer: ReturnType<typeof initPptxPreview> | null = null;

  // Outputs-card action menu (Preview / Download per language), positioned at the click point.
  outputMenuKey: string | null = null;
  outputMenuX = 0;
  outputMenuY = 0;
  outputMenuCtx: { sid: string; version: number; lang: string; title: string; languages: string[] } | null = null;

  // Feedback popover opened from a thumb under a deck-ready chat message.
  feedbackPopover: { sid: string; version: number; left: number; top: number | null; bottom: number | null } | null = null;
  copiedMessage: ChatMessage | null = null;
  private copiedTimer: ReturnType<typeof setTimeout> | null = null;

  @ViewChild('feedbackCommentEl')
  set feedbackCommentElement(ref: ElementRef<HTMLTextAreaElement> | undefined) {
    ref?.nativeElement.focus();
  }

  // The same ng-template is re-stamped in welcome/docked locations during hydration/upload.
  // Never capture its element in a timeout; always measure the latest query result.
  @ViewChild('composerEl')
  set composerElement(ref: ElementRef<HTMLTextAreaElement> | undefined) {
    this.composerRef = ref;
    this.scheduleComposerResize();
  }

  constructor(public vm: WorkspaceService, public markdown: MarkdownService, private ui: UiService, private zone: NgZone) {}

  ngOnInit(): void {
    this.initAppearance();
    this.restorePanelWidths();
    this.detachViewPort = this.vm.attachViewPort({ notify: (event) => this.onPresentation(event) });
  }

  ngAfterViewChecked(): void {
    // Hidden panels have no geometry. Keep pending work until Generate is visible again.
    if (this.vm.currentTab !== 'chat') return;
    if (this.pendingRestoreScrollHeight != null && this.messagesElRef) {
      const el = this.messagesElRef.nativeElement;
      if (el.scrollHeight > this.pendingRestoreScrollHeight) {
        // An older page rendered above the viewport: shift down by the added height to stay put.
        el.scrollTop = el.scrollHeight - this.pendingRestoreScrollHeight;
        this.pendingRestoreScrollHeight = null;
        this.pendingScrollMessages = false;
      } else if (!this.vm.loadingOlderMessages) {
        this.pendingRestoreScrollHeight = null;
      }
    }
    if (this.pendingScrollMessages && this.messagesElRef) {
      const el = this.messagesElRef.nativeElement;
      el.scrollTop = el.scrollHeight;
      this.pendingScrollMessages = false;
    }
    const last = this.logListEls?.last?.nativeElement;
    if (last && last.getAttribute('data-live') === '1') last.scrollTop = last.scrollHeight;
    if (this.pendingScrollIngest && this.ingestListRef) {
      const el = this.ingestListRef.nativeElement;
      el.scrollTop = el.scrollHeight;
      this.pendingScrollIngest = false;
    }
    this.scheduleComposerResize();
  }

  ngOnDestroy(): void {
    this.destroyed = true;
    if (this.copiedTimer) clearTimeout(this.copiedTimer);
    this.detachViewPort?.();
    this.detachViewPort = undefined;
    if (this.composerTimer !== undefined) clearTimeout(this.composerTimer);
    this.composerTimer = undefined;
    this.composerRef = undefined;
    this.messagesElRef = undefined;
    this.ingestListRef = undefined;
    this.fileInputRef = undefined;
    this.logListEls = undefined;
    this.previewBuffer = null;
    this.destroyPreviewer();
    this.pptxWrapperRef = undefined;
    this.previewStageRef = undefined;
    if (this.themeObserver) {
      this.themeObserver.disconnect();
      this.themeObserver = undefined;
    }
  }

  /** Lazy history: when the user scrolls near the top, pull the next older page. */
  onMessagesScroll(): void {
    const el = this.messagesElRef?.nativeElement;
    if (!el || this.vm.currentTab !== 'chat') return;
    if (el.scrollTop <= 48 && this.vm.hasMoreOlderMessages && !this.vm.loadingOlderMessages) this.loadOlderMessages();
  }

  /** Records the current height first so the prepended page doesn't move the reading position. */
  loadOlderMessages(): void {
    if (this.vm.loadingOlderMessages || !this.vm.hasMoreOlderMessages) return;
    const el = this.messagesElRef?.nativeElement;
    if (el) this.pendingRestoreScrollHeight = el.scrollHeight;
    this.vm.loadOlderMessages();
  }

  private onPresentation(event: WorkspacePresentationEvent): void {
    switch (event.type) {
      case 'scroll-messages': this.pendingScrollMessages = true; this.pendingRestoreScrollHeight = null; break;
      case 'scroll-ingest': this.pendingScrollIngest = true; break;
      case 'deck-ready': this.isOutputsExpanded = true; break;
      case 'project-created': this.isProjectsExpanded = true; break;
      case 'resize-composer':
        this.pendingComposerResize = true;
        this.pendingComposerFocus ||= event.focus;
        this.scheduleComposerResize();
        break;
    }
  }

  private scheduleComposerResize(): void {
    if (this.destroyed || !this.pendingComposerResize || !this.composerRef
      || this.vm.currentTab !== 'chat' || this.composerTimer !== undefined) return;
    // Run after ngModel and the welcome/docked ng-template have been updated.
    this.composerTimer = setTimeout(() => {
      this.composerTimer = undefined;
      const el = this.composerRef?.nativeElement;
      if (this.destroyed || this.vm.currentTab !== 'chat' || !el?.isConnected) return;
      if (this.pendingComposerFocus) el.focus();
      this.resizeComposer(el);
      this.pendingComposerResize = false;
      this.pendingComposerFocus = false;
    });
  }

  applySuggestion(prompt: string): void {
    this.vm.chatInputValue = prompt;
    this.onPresentation({ type: 'resize-composer', focus: true });
  }

  onComposerKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      this.vm.onChatSubmit();
    }
  }

  autoGrowComposer(event: Event): void { this.resizeComposer(event.target as HTMLTextAreaElement); }

  private resizeComposer(el: HTMLTextAreaElement): void {
    el.style.height = 'auto';
    const h = el.scrollHeight;
    el.style.height = `${h}px`;
    this.vm.composerHeight = h;
    this.vm.composerExpanded = h > 44;
  }

  toggleSidebar(): void {
    this.isSidebarCollapsed = !this.isSidebarCollapsed;
  }

  toggleStudio(): void {
    this.isStudioCollapsed = !this.isStudioCollapsed;
  }

  // ---- Resizable side panels ------------------------------------------------

  private static readonly PANEL_LIMITS = {
    sidebar: { min: 200, max: 600, key: 'se_sidebar_width', cssVar: '--sidebar-w', dir: 1 },
    studio: { min: 220, max: 600, key: 'se_studio_width', cssVar: '--studio-w', dir: -1 },
  } as const;

  /** Mirrors the CSS `min(var(--*-w), 40vw)` cap so the handle never drifts from the panel edge. */
  private clampPanelWidth(panel: 'sidebar' | 'studio', width: number): number {
    const { min, max } = TrainingStudioComponent.PANEL_LIMITS[panel];
    const cap = Math.max(min, Math.min(max, Math.floor(window.innerWidth * 0.4)));
    return Math.round(Math.max(min, Math.min(cap, width)));
  }

  setPanelWidth(panel: 'sidebar' | 'studio', width: number | null): void {
    const { key } = TrainingStudioComponent.PANEL_LIMITS[panel];
    if (panel === 'sidebar') this.sidebarWidth = width; else this.studioWidth = width;
    try {
      if (width == null) localStorage.removeItem(key); else localStorage.setItem(key, String(width));
    } catch { /* storage unavailable */ }
  }

  private restorePanelWidths(): void {
    for (const panel of ['sidebar', 'studio'] as const) {
      const { min, max, key } = TrainingStudioComponent.PANEL_LIMITS[panel];
      let saved = NaN;
      try { saved = Number(localStorage.getItem(key)); } catch { /* storage unavailable */ }
      if (Number.isFinite(saved) && saved >= min && saved <= max) {
        if (panel === 'sidebar') this.sidebarWidth = saved; else this.studioWidth = saved;
      }
    }
  }

  startPanelResize(ev: PointerEvent, panel: 'sidebar' | 'studio', panelEl: HTMLElement): void {
    const layout = this.chatLayoutRef?.nativeElement;
    if (ev.button !== 0 || !layout) return;
    ev.preventDefault();
    const handle = ev.currentTarget as HTMLElement;
    const { cssVar, dir } = TrainingStudioComponent.PANEL_LIMITS[panel];
    const startX = ev.clientX;
    const startWidth = panelEl.getBoundingClientRect().width;
    let width = startWidth;
    let moved = false;
    handle.setPointerCapture(ev.pointerId);
    handle.classList.add('is-active');
    layout.classList.add('is-resizing');
    // Pointer moves update the CSS variable directly so dragging doesn't run change detection.
    this.zone.runOutsideAngular(() => {
      const onMove = (e: PointerEvent) => {
        moved = true;
        width = this.clampPanelWidth(panel, startWidth + dir * (e.clientX - startX));
        layout.style.setProperty(cssVar, `${width}px`);
      };
      const onEnd = () => {
        handle.removeEventListener('pointermove', onMove);
        handle.removeEventListener('pointerup', onEnd);
        handle.removeEventListener('pointercancel', onEnd);
        handle.classList.remove('is-active');
        layout.classList.remove('is-resizing');
        if (moved) this.zone.run(() => this.setPanelWidth(panel, width));
      };
      handle.addEventListener('pointermove', onMove);
      handle.addEventListener('pointerup', onEnd);
      handle.addEventListener('pointercancel', onEnd);
    });
  }

  onPanelResizeKey(ev: KeyboardEvent, panel: 'sidebar' | 'studio', panelEl: HTMLElement): void {
    if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') return;
    ev.preventDefault();
    const { dir } = TrainingStudioComponent.PANEL_LIMITS[panel];
    const step = (ev.key === 'ArrowRight' ? 16 : -16) * dir;
    this.setPanelWidth(panel, this.clampPanelWidth(panel, panelEl.getBoundingClientRect().width + step));
  }

  toggleProjects(): void {
    this.isProjectsExpanded = !this.isProjectsExpanded;
  }

  toggleOutputs(): void {
    this.isOutputsExpanded = !this.isOutputsExpanded;
  }

  onAddSourceClick(): void {
    if (this.hasNoProject) {
      this.vm.onNewSessionClick();
      return;
    }
    this.fileInputRef?.nativeElement.click();
  }

  onRenameSessionClick(event: MouseEvent, s: Session): void {
    event.stopPropagation();
    void this.vm.renameProject(s.id, s.title);
  }

  onDeleteSessionClick(event: MouseEvent, s: Session): void {
    event.stopPropagation();
    this.vm.deleteProject(s.id, s.title);
  }

  get hasNoProject(): boolean {
    return !this.vm.currentSessionId;
  }

  get userFirstName(): string {
    return this.vm.userFirstName;
  }

  get userName(): string {
    return this.vm.userName;
  }

  get userInitials(): string {
    const parts = (this.vm.userName || '').trim().split(/\s+/).filter(Boolean);
    if (parts.length >= 2) {
      return (parts[0].charAt(0) + parts[parts.length - 1].charAt(0)).toUpperCase();
    }
    return (this.vm.userName.trim().slice(0, 2) || this.vm.userInitial || 'U').toUpperCase();
  }

  get greetingText(): string {
    const hour = new Date().getHours();
    if (hour < 12) return 'Good morning';
    if (hour < 17) return 'Good afternoon';
    return 'Good evening';
  }

  scrollToSuggestions(): void {
    const el = document.querySelector('.suggestions');
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  get statusPillClass(): string {
    const s = (this.vm.statusPillText || '').toLowerCase();
    if (s === 'done') return 'pill done';
    if (s === 'running') return 'pill running';
    if (s === 'answered') return 'pill answered';
    if (s === 'failed') return 'pill failed';
    if (s === 'stopped') return 'pill stopped';
    if (s === 'recovering' || s === 'stopping') return `pill ${s}`;
    if (s === 'reconnecting') return 'pill reconnecting';
    return 'pill';
  }

  get isChatEmpty(): boolean {
    return !this.hasNoProject && !this.vm.messagesLoading && !this.vm.messagesErrorText
      && !this.vm.messagesForCurrent.length && !this.vm.liveTurn && !this.vm.resumingRun
      && !this.vm.pendingMessagesForCurrent.length;
  }
  get latestIngestEvent(): StreamEvent | null {
    const log = this.vm.ingestLogForCurrent;
    return log.length ? log[log.length - 1] : null;
  }
  get activeUploads(): SessionDoc[] { return this.vm.studioInputsRows.filter(isDocProcessing); }
  formatStamp(iso: string | null | undefined): string {
    const at = iso ? new Date(iso) : null;
    if (!at || Number.isNaN(at.getTime())) return '';
    return `${at.toLocaleDateString([], { day: 'numeric', month: 'short' })} ${at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })}`;
  }
  sessionMeta(s: Session): string {
    if (this.vm.deletingSessionIds.has(s.id)) return 'deleting…';
    if (this.vm.sidebarStatusFailed.has(s.id)) return '';
    const when = this.formatStamp(s.createdAt);
    // Status of older, unopened projects isn't fetched on load — show the date only, not a misleading "new".
    if (s.status === 'new' && this.vm.sidebarStatusDeferred.has(s.id)) return when;
    const stamp = when ? ` · ${when}` : '';
    return `${s.status}${s.deckVersion ? ` · v${s.deckVersion}` : ''}${stamp}`;
  }
  trackByIndex(i: number): number { return i; }
  readonly userBubbleText = userBubbleText;
  trackByString(_i: number, v: string): string { return v; }
  trackSessionById(_i: number, s: Session): string { return s.id; }
  trackDocByKey(_i: number, d: SessionDoc): string { return d._pid || d.doc_id || d.filename; }
  trackVersionByDeckVersion(_i: number, v: StudioOutputRow): number { return v.deck_version; }
  readonly feedbackMax = DECK_FEEDBACK_COMMENT_MAX;
  trackSlideByIndex(_i: number, s: SlideInfo): number { return s.index; }
  /** Live chat/clarify/planning turns get a lightweight view; deck builds, errors and history get the full log. */
  isCompactTurn(turn: LiveTurn, live: boolean): boolean {
    const f = turn?.flow;
    return !!live && !!f && f.mode !== 'build' && !f.error && !f.cancelled && !this.showResumeCard(turn, live);
  }
  /** A re-attached run still catching up shows "Resuming" instead of a misleading empty "Thinking" state. */
  showResumeCard(turn: LiveTurn, live: boolean): boolean {
    const f = turn?.flow;
    return !!live && !!turn?.resuming && !!f && f.mode !== 'build' && !f.error && !f.cancelled;
  }

  /** Cache persisted turns so successful builds start collapsed and manual toggles are preserved. */
  thoughtTurnFor(m: ChatMessage): LiveTurn {
    let turn = this.thoughtTurnCache.get(m);
    if (!turn) {
      turn = buildTurnFromMessage(m);
      turn.collapsed = turn.flow.done && !turn.flow.error;
      this.thoughtTurnCache.set(m, turn);
    }
    // Pick up a build duration that was stamped after this turn was first cached.
    if (turn.durationMs == null && m.durationMs != null) turn.durationMs = m.durationMs;
    return turn;
  }

  // ---- In-app slide preview -------------------------------------------------

  /** False once the version is gone from the loaded Outputs list (e.g. deleted). */
  private isVersionAvailable(version: number): boolean {
    const outputs = this.vm.studioOutputs;
    if (outputs === null || this.vm.studioOutputsLoading) return true;
    return outputs.some((v) => Number(v.deck_version) === Number(version));
  }

  /** True when this assistant message announces a freshly built/updated deck we can preview. */
  canPreviewMessage(m: ChatMessage): boolean {
    return !!(m && (m.role === 'assistant' || m.role === 'system') && m.result?.deck_updated && m.result?.deck_version != null
      && this.isVersionAvailable(Number(m.result.deck_version)));
  }

  /** Full language set + title for a version, preferring the live Outputs list over the (often English-only) done-event snapshot. */
  private previewInfoForVersion(version: number, fallbackFiles: Record<string, string> | undefined, fallbackTitle?: string): { languages: string[]; title: string } {
    const match = (this.vm.studioOutputs || []).find((v) => Number(v.deck_version) === Number(version));
    const languages = (match?._languages?.length ? match._languages : Object.keys(fallbackFiles || {})).filter(Boolean);
    return { languages, title: match?.plan_title || fallbackTitle || 'Deck preview' };
  }

  /** Open the slide preview for the deck announced by an assistant message. */
  previewDeckFromMessage(m: ChatMessage): void {
    const r = m.result;
    if (!r || r.deck_version == null) return;
    const sid = m.session_id || this.vm.currentSessionId;
    if (!sid) return;
    const { languages, title } = this.previewInfoForVersion(Number(r.deck_version), r.files, r.plan_title);
    this.openDeckPreview(sid, Number(r.deck_version), languages, title);
  }

  /** True when a finished thought board carries a deck we can preview. */
  canPreviewTurn(turn: LiveTurn): boolean {
    return this.previewDetailsForTurn(turn) != null;
  }

  previewDetailsForTurn(turn: LiveTurn): { version: number; languages: string[]; title: string } | null {
    if (!turn?.flow?.done) return null;
    const result = turn.events.find((event) => event.type === 'done' && event.data?.result?.deck_updated)?.data?.result;
    if (result?.deck_version == null) return null;
    const version = Number(result.deck_version);
    if (!this.isVersionAvailable(version)) return null;
    return { version, ...this.previewInfoForVersion(version, result.files, result.plan_title) };
  }

  /** Open the slide preview for the deck built in this thought board. */
  previewDeckFromTurn(turn: LiveTurn): void {
    const preview = this.previewDetailsForTurn(turn);
    const sid = this.vm.currentSessionId;
    if (!preview || !sid) return;
    this.openDeckPreview(sid, preview.version, preview.languages, preview.title);
  }

  outputMenuKeyFor(v: StudioOutputRow, lang: string): string {
    return `${v._sid}|${v.deck_version}|${lang}`;
  }

  // ---- Chat message copy + deck feedback -----------------------------------

  async copyMessage(m: ChatMessage, body: HTMLElement): Promise<void> {
    try {
      await navigator.clipboard.writeText((body.innerText || m.content || '').trim());
    } catch {
      this.ui.toast('Could not copy to clipboard.', 'error');
      return;
    }
    this.copiedMessage = m;
    if (this.copiedTimer) clearTimeout(this.copiedTimer);
    this.copiedTimer = setTimeout(() => { this.copiedMessage = null; this.copiedTimer = null; }, 2000);
  }

  private feedbackRef(m: ChatMessage): { sid: string; version: number } | null {
    const sid = m.session_id || this.vm.currentSessionId;
    const version = m.result?.deck_version;
    return sid && version != null ? { sid, version: Number(version) } : null;
  }

  messageFeedback(m: ChatMessage): DeckFeedbackState | undefined {
    const ref = this.feedbackRef(m);
    return ref ? this.vm.feedbackFor(ref.sid, ref.version) : undefined;
  }

  isFeedbackPopoverFor(m: ChatMessage): boolean {
    const ref = this.feedbackRef(m);
    const p = this.feedbackPopover;
    return !!(ref && p && p.sid === ref.sid && p.version === ref.version);
  }

  get popoverFeedback(): DeckFeedbackState | undefined {
    const p = this.feedbackPopover;
    const fb = p ? this.vm.feedbackFor(p.sid, p.version) : undefined;
    return fb && (fb.status === 'open' || fb.status === 'submitting') && fb.thumbsUp !== null ? fb : undefined;
  }

  feedbackThumbTitle(fb: DeckFeedbackState, up: boolean): string {
    if (fb.status === 'loading') return 'Loading feedback…';
    if (fb.status === 'load-error') return "Couldn't load feedback — click to retry";
    if (fb.status === 'saved') {
      const rated = fb.thumbsUp ? 'helpful' : 'not helpful';
      return `You rated this ${rated}${fb.comment ? `: “${fb.comment}”` : ''}`;
    }
    return up ? 'Helpful' : 'Not helpful';
  }

  rateMessage(ev: MouseEvent, m: ChatMessage, thumbsUp: boolean): void {
    const ref = this.feedbackRef(m);
    const fb = ref ? this.vm.feedbackFor(ref.sid, ref.version) : undefined;
    if (!ref || !fb) return;
    if (fb.status === 'load-error') { this.vm.retryDeckFeedback(ref.sid, ref.version); return; }
    if (fb.status !== 'open') return;
    this.vm.setFeedbackRating(ref.sid, ref.version, thumbsUp);

    const rect = (ev.currentTarget as HTMLElement).getBoundingClientRect();
    const width = 320;
    const estHeight = 240;
    const left = Math.max(8, Math.min(rect.left - 12, window.innerWidth - width - 8));
    const openAbove = rect.top > estHeight + 16;
    this.feedbackPopover = {
      ...ref, left,
      top: openAbove ? null : Math.round(rect.bottom + 8),
      bottom: openAbove ? Math.round(window.innerHeight - rect.top + 8) : null,
    };
  }

  cancelFeedback(): void {
    const p = this.feedbackPopover;
    if (!p) return;
    if (this.vm.feedbackFor(p.sid, p.version)?.status === 'submitting') return;
    this.vm.clearFeedbackDraft(p.sid, p.version);
    this.feedbackPopover = null;
  }

  async submitFeedback(): Promise<void> {
    const p = this.feedbackPopover;
    if (!p) return;
    await this.vm.submitDeckFeedback(p.sid, p.version);
    if (this.vm.feedbackFor(p.sid, p.version)?.status === 'saved' && this.feedbackPopover === p) this.feedbackPopover = null;
  }

  /** Toggle the Preview/Download menu anchored to a language chip. */
  toggleOutputMenu(ev: MouseEvent, v: StudioOutputRow, lang: string): void {
    ev.stopPropagation();
    const key = this.outputMenuKeyFor(v, lang);
    if (this.outputMenuKey === key) { this.closeOutputMenu(); return; }
    const rect = (ev.currentTarget as HTMLElement).getBoundingClientRect();
    this.outputMenuX = Math.round(rect.left);
    this.outputMenuY = Math.round(rect.bottom + 6);
    this.outputMenuKey = key;
    this.outputMenuCtx = {
      sid: v._sid, version: v.deck_version, lang,
      title: v.plan_title || 'Deck preview', languages: v._languages || [],
    };
  }

  closeOutputMenu(): void {
    this.outputMenuKey = null;
    this.outputMenuCtx = null;
  }

  outputMenuPreview(): void {
    const c = this.outputMenuCtx;
    if (!c) return;
    this.closeOutputMenu();
    this.openDeckPreview(c.sid, c.version, c.languages.length ? c.languages : [c.lang], c.title, c.lang);
  }

  outputMenuDownload(): void {
    const c = this.outputMenuCtx;
    if (!c) return;
    this.closeOutputMenu();
    void this.vm.downloadDeckVersion(c.sid, c.version, c.lang);
  }

  openDeckPreview(sid: string, version: number, languages: string[] | null | undefined, title?: string, initialLanguage?: string): void {
    this.previewSid = sid;
    this.previewVersion = version;
    this.previewLanguages = (languages || []).filter(Boolean);
    this.previewLanguage = (initialLanguage && this.previewLanguages.includes(initialLanguage))
      ? initialLanguage
      : (this.previewLanguages[0] || null);
    this.previewTitle = title || 'Deck preview';
    this.previewSlideCount = 0;
    this.previewCurrent = 1;
    this.previewError = '';
    this.previewOpen = true;
    void this.loadPreview();
  }

  switchPreviewLanguage(lang: string): void {
    if (!lang || lang === this.previewLanguage) return;
    this.previewLanguage = lang;
    void this.loadPreview();
  }

  closeDeckPreview(): void {
    this.previewOpen = false;
    this.previewLoading = false;
    this.previewError = '';
    this.previewBuffer = null;
    this.destroyPreviewer();
    const host = this.pptxWrapperRef?.nativeElement;
    if (host) host.innerHTML = '';
  }

  /** Save-to-disk fallback for anything that doesn't render perfectly in the preview. */
  downloadPreview(): void {
    if (this.previewSid && this.previewVersion != null) {
      void this.vm.downloadDeckVersion(this.previewSid, this.previewVersion, this.previewLanguage || undefined);
    }
  }

  previewNav(dir: number): void {
    const stage = this.previewStageRef?.nativeElement;
    const slides = this.previewSlideEls();
    if (!stage || !slides.length) return;
    const target = Math.min(slides.length, Math.max(1, this.previewCurrent + dir));
    this.previewCurrent = target;
    const top = this.slideOffset(stage, slides[target - 1]) - 8;
    stage.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
  }

  onPreviewScroll(): void {
    const stage = this.previewStageRef?.nativeElement;
    const slides = this.previewSlideEls();
    if (!stage || !slides.length) return;
    const mid = stage.scrollTop + stage.clientHeight / 2;
    let idx = 0;
    for (let i = 0; i < slides.length; i++) {
      if (this.slideOffset(stage, slides[i]) <= mid) idx = i;
    }
    this.previewCurrent = idx + 1;
  }

  private async loadPreview(): Promise<void> {
    if (!this.previewSid || this.previewVersion == null) return;
    this.previewLoading = true;
    this.previewError = '';
    this.previewSlideCount = 0;
    this.previewCurrent = 1;
    try {
      const blob = await this.vm.api.fetchDeckBlob(this.previewSid, this.previewVersion, this.previewLanguage || undefined);
      this.previewBuffer = await blob.arrayBuffer();
      await this.renderPreview();
    } catch (e: any) {
      this.previewError = e?.message || 'Unable to load the preview.';
    } finally {
      this.previewLoading = false;
    }
  }

  private async renderPreview(): Promise<void> {
    // Wait a tick so the host div exists in the DOM (it lives inside the *ngIf overlay).
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    const host = this.pptxWrapperRef?.nativeElement;
    if (!host || !this.previewBuffer || !this.previewOpen) return;
    this.destroyPreviewer();
    host.innerHTML = '';
    try {
      this.previewer = initPptxPreview(host, { width: 960, height: 540, mode: 'list' });
      await this.previewer.preview(this.previewBuffer);
    } catch {
      this.destroyPreviewer();
      this.previewError = 'This deck could not be rendered here. Use Download to open it in PowerPoint.';
      return;
    }
    if (!this.previewOpen) { this.destroyPreviewer(); return; }
    // Collapse the library's own fixed-height/internal scroll so the modal has a single scroller.
    const wrap = this.previewer.wrapper as HTMLElement | undefined;
    if (wrap) { wrap.style.height = 'auto'; wrap.style.maxHeight = 'none'; wrap.style.overflow = 'visible'; }
    this.previewSlideCount = this.previewer.slideCount || this.previewSlideEls().length;
    this.previewCurrent = 1;
    this.hidePlaceholderPrompts();
    const stage = this.previewStageRef?.nativeElement;
    if (stage) stage.scrollTop = 0;
  }

  /** pptx-preview paints the layout/master placeholder prompts ("Click to edit", "Second level")
      that PowerPoint keeps hidden; strip those ghost frames so they stop overlapping real content. */
  private hidePlaceholderPrompts(): void {
    const host = (this.previewer?.wrapper as HTMLElement | undefined) || this.pptxWrapperRef?.nativeElement;
    if (!host) return;
    const norm = (s: string) => s.replace(/\s+/g, ' ').trim().toLowerCase();
    const LEVELS = new Set(['second level', 'third level', 'fourth level', 'fifth level']);
    const isPromptLine = (l: string): boolean =>
      !!l && (l.startsWith('click to edit') || l.startsWith('click to add') || l.startsWith('click icon to add') || LEVELS.has(l));
    try {
      host.querySelectorAll<HTMLElement>('*').forEach((el) => {
        if (el.childElementCount > 6) return;
        const raw = el.textContent || '';
        if (!raw || raw.length > 200) return;
        const flat = norm(raw);
        if (!flat.includes('click to') && !/(second|third|fourth|fifth) level/.test(flat)) return;
        const lines = (el.innerText || raw).split(/\r?\n/).map(norm).filter(Boolean);
        if (lines.length && lines.every(isPromptLine)) el.style.display = 'none';
      });
    } catch { /* best-effort cleanup */ }
  }

  private destroyPreviewer(): void {
    try { this.previewer?.destroy(); } catch { /* ignore teardown errors */ }
    this.previewer = null;
  }

  /** Best-effort list of the rendered slide elements inside the preview host. */
  private previewSlideEls(): HTMLElement[] {
    const host = (this.previewer?.wrapper as HTMLElement | undefined) || this.pptxWrapperRef?.nativeElement;
    if (!host) return [];
    let els = Array.from(host.children) as HTMLElement[];
    // pptx-preview may nest slides one level down; drill in when there's a single wrapper child.
    if (els.length === 1 && els[0].children.length > 1) els = Array.from(els[0].children) as HTMLElement[];
    return els.filter((el) => el.offsetHeight > 40);
  }

  private slideOffset(stage: HTMLElement, el: HTMLElement): number {
    return el.getBoundingClientRect().top - stage.getBoundingClientRect().top + stage.scrollTop;
  }

  toggleAppearanceModal(): void {
    this.isAppearanceOpen = !this.isAppearanceOpen;
  }

  closeAppearanceModal(): void {
    this.isAppearanceOpen = false;
  }

  onSelectSession(id: string): void {
    this.isAppearanceOpen = false;
    this.isOutputsExpanded = false;
    this.vm.selectSession(id);
  }

  onNewProjectClick(): void {
    this.isAppearanceOpen = false;
    this.isOutputsExpanded = false;
    this.vm.onNewSessionClick();
  }

  selectThemeColor(id: string): void {
    if (this.isDarkMode) {
      return;
    }
    this.selectedThemeId = id;
    try {
      localStorage.setItem('se_theme_color', id);
    } catch {}
    this.applyCurrentAppearance();
  }

  selectChatStyle(id: string): void {
    if (this.isDarkMode) {
      return;
    }
    this.selectedChatStyleId = id;
    try {
      localStorage.setItem('se_chat_style', id);
    } catch {}
    this.applyCurrentAppearance();
  }

  /** Font selection is theme-agnostic — takes effect in both light and dark mode. */
  selectFont(id: string): void {
    if (!this.fontOptions.some((f) => f.id === id)) return;
    this.selectedFontId = id;
    try {
      localStorage.setItem('se_font_family', id);
    } catch {}
    this.applyFont();
  }

  private applyFont(): void {
    const font = this.fontOptions.find((f) => f.id === this.selectedFontId) || this.fontOptions[0];
    document.documentElement.style.setProperty('--app-font', font.stack);
    document.documentElement.setAttribute('data-font', font.id);
  }

  private updateDarkModeState(): void {
    const root = document.documentElement;
    this.isDarkMode = root.getAttribute('data-theme') === 'dark';
  }

  private applyCurrentAppearance(): void {
    this.updateDarkModeState();
    const root = document.documentElement;
    if (this.isDarkMode) {
      // In dark mode: overall theme color is permanently fixed to Vibrant Indigo (#6366f1)
      // Custom theme palettes and chat bubble styles only take effect in light mode.
      root.setAttribute('data-theme-color', this.darkModeTheme.id);
      root.style.setProperty('--primary', this.darkModeTheme.primary);
      root.style.setProperty('--primary-hover', this.darkModeTheme.hover);
      root.style.setProperty('--primary-text', this.darkModeTheme.primaryText);
      root.style.setProperty('--primary-soft', this.darkModeTheme.soft);
      root.style.setProperty('--primary-accent-text', this.darkModeTheme.accentText);
      root.style.setProperty('--crail', this.darkModeTheme.primary);
      root.style.setProperty('--crail-hover', this.darkModeTheme.hover);
      // Do NOT override --blue / --blue-soft from the theme accent — they are
      // semantic status colors (thought-process, running pills, progress chips)
      // and must stay high-contrast/readable independent of the chosen accent.
      root.style.removeProperty('--blue');
      root.style.removeProperty('--blue-soft');
      root.removeAttribute('data-chat-style');
      root.style.removeProperty('--chat-user-bg');
      root.style.removeProperty('--chat-user-text');
      root.style.removeProperty('--chat-user-border');
      root.style.removeProperty('--chat-agent-bg');
      root.style.removeProperty('--chat-agent-text');
      root.style.removeProperty('--chat-agent-border');
    } else {
      // Light Mode: Apply user's selected theme color and chat style
      this.applyThemeColor(this.currentTheme);
      const style = this.chatStyleOptions.find((c) => c.id === this.selectedChatStyleId) || this.chatStyleOptions[0];
      this.applyChatStyle(style);
    }
  }

  private applyThemeColor(theme: ThemeColorOption): void {
    const root = document.documentElement;
    root.setAttribute('data-theme-color', theme.id);
    root.style.setProperty('--primary', theme.primary);
    root.style.setProperty('--primary-hover', theme.hover);
    root.style.setProperty('--primary-text', theme.primaryText);
    root.style.setProperty('--primary-soft', theme.soft);
    root.style.setProperty('--primary-accent-text', theme.accentText);
    root.style.setProperty('--crail', theme.primary);
    root.style.setProperty('--crail-hover', theme.hover);
    // --blue / --blue-soft are semantic status colors; keep them stable across theme swaps.
    root.style.removeProperty('--blue');
    root.style.removeProperty('--blue-soft');
  }

  private applyChatStyle(style: ChatStyleOption): void {
    const root = document.documentElement;
    root.setAttribute('data-chat-style', style.id);
    root.style.setProperty('--chat-user-bg', style.userBg);
    root.style.setProperty('--chat-user-text', style.userText);
    root.style.setProperty('--chat-user-border', style.userBorder);
    root.style.setProperty('--chat-agent-bg', style.agentBg);
    root.style.setProperty('--chat-agent-text', style.agentText);
    root.style.setProperty('--chat-agent-border', style.agentBorder);
  }

  private initAppearance(): void {
    try {
      const savedTheme = localStorage.getItem('se_theme_color');
      if (savedTheme && this.themeColorOptions.some((t) => t.id === savedTheme)) {
        this.selectedThemeId = savedTheme;
      }
      const savedChat = localStorage.getItem('se_chat_style');
      if (savedChat && this.chatStyleOptions.some((c) => c.id === savedChat)) {
        this.selectedChatStyleId = savedChat;
      }
      const savedFont = localStorage.getItem('se_font_family');
      if (savedFont && this.fontOptions.some((f) => f.id === savedFont)) {
        this.selectedFontId = savedFont;
      }
    } catch {}
    this.applyCurrentAppearance();
    this.applyFont();

    if (typeof MutationObserver !== 'undefined') {
      this.themeObserver = new MutationObserver(() => {
        const wasDark = this.isDarkMode;
        this.updateDarkModeState();
        if (wasDark !== this.isDarkMode) {
          this.applyCurrentAppearance();
        }
      });
      this.themeObserver.observe(document.documentElement, {
        attributes: true,
        attributeFilter: ['data-theme'],
      });
    }
  }
}