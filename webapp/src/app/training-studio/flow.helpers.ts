import { ChatMessage, MessageTimelineItem, RunStatus, StreamEvent, TimelineItem, WorkflowEventTimelineItem } from '../models/models';
import { FlowState, LiveTurn, SlideInfo } from '../models/workspace.models';
import { isActiveRunStatus, normalizeRunStatus } from '../shared/workflow.helpers';

const THOUGHT_STYLE: Record<string, [string, string]> = {
  agent_start: ['▶', 't-start'], agent_step: ['•', 't-step'], agent_done: ['✔', 't-done'],
  agent_error: ['✖', 't-error'], thought: ['…', 't-thought'], deck: ['◆', 't-deck'],
  ingest_step: ['»', 't-ingest'], ingest_done: ['✔', 't-ingest'], ingest_error: ['✖', 't-error'],
  workflow_recovering: ['↻', 't-warn'], workflow_recovered: ['✔', 't-done'], workflow_cancelled: ['■', 't-error'],
  done: ['★', 't-final'],
};

/** Log-row icon/colour per slide stage, so each row matches the colour of its slide chip. */
const SLIDE_ROW_STYLE: Record<string, [string, string]> = {
  progress: ['✎', 't-slide-write'], research: ['⌕', 't-slide-research'], written: ['✓', 't-slide-write'],
  design: ['◇', 't-slide-design'], retry: ['↻', 't-slide-retry'], done: ['✔', 't-slide-done'], failed: ['✖', 't-slide-failed'],
};

export const FLOW_STAGES: { key: string; label: string; status: string }[] = [
  { key: 'write', label: 'Plan & write', status: 'Writing your slides' },
  { key: 'design', label: 'Design', status: 'Designing your slides' },
  { key: 'assemble', label: 'Assemble', status: 'Assembling your deck' },
  { key: 'ready', label: 'Ready', status: 'Finalizing your deck' },
];
const STAGE_WRITE = 0, STAGE_DESIGN = 1, STAGE_ASSEMBLE = 2, STAGE_READY = 3;

/**
 * Approximate share of a deck build's wall time per phase (until the backend reports real values):
 * plan + write ≈ 20%, design/repair ≈ 60-70%, assemble + ready ≈ 10% for one language, up to 20% for more.
 */
const PCT_WRITE = 20;
function assembleShare(f: FlowState): number {
  const langs = Math.max(f.langTotal || 0, f.langs.size);
  return langs <= 1 ? 10 : Math.min(20, 10 + 5 * (langs - 1));
}

const LANG_CODES: Record<string, string> = {
  english: 'EN', french: 'FR', spanish: 'ES', portuguese: 'PT', mandarin: 'ZH',
  chinese: 'ZH', japanese: 'JA', german: 'DE', italian: 'IT', korean: 'KO',
  russian: 'RU', arabic: 'AR', hindi: 'HI', dutch: 'NL',
};
export function langCode(name: string): string {
  return LANG_CODES[name.toLowerCase()] || name.slice(0, 2).toUpperCase();
}

const SLIDE_LABELS: Record<string, string> = {
  progress: 'writing', research: 'researching', written: 'written · waiting for design',
  design: 'designing', retry: 'retrying', done: 'done', failed: 'failed',
};
/** Chip stages that are still being worked on (pulse + get resolved when the run finishes). */
const ACTIVE_SLIDE_STAGES = new Set(['progress', 'research', 'written', 'design', 'retry']);

export function newFlowState(): FlowState {
  return {
    started: false, stageIdx: -1, done: false, error: false, cancelled: false,
    slideTotal: 0, slideTarget: 0, designTarget: -1, pct: 0, langTotal: null,
    slides: new Map(), langs: new Set(),
    mode: 'thinking', thinkingNote: '', activity: [], now: '', nowStage: '',
  };
}

const PLANNER_TOOL_STEPS: Record<string, string> = {
  create_deck_plan: 'Drafting the slide outline',
  revise_deck_slides: 'Reworking the selected slides',
  add_deck_slides: 'Adding new slides to the plan',
  remove_deck_slides: 'Removing slides from the plan',
  move_deck_slides: 'Reordering the slides',
  set_slide_layout: 'Choosing slide layouts',
  read_current_plan: 'Reviewing the current plan',
};

/** Plain-language step for a planning-phase event, or null when it has nothing to tell a non-technical user. */
export function friendlyPlanStep(evt: StreamEvent): string | null {
  const msg = `${evt?.message || ''}`.trim();
  if (evt?.type === 'workflow_recovering') return 'Hit a temporary issue, retrying automatically';
  if (evt?.type === 'workflow_recovered') return 'Recovered, carrying on';
  if (evt?.type === 'guardrail') return msg || 'This request is outside the source material';
  if (evt?.agent === 'triage') return /route=(extraction|approve)/.test(msg) ? 'Understanding your request' : null;
  if (evt?.agent === 'planning') {
    if (/interpreting the request/i.test(msg)) return 'Reading your source documents';
    if (/plan approved/i.test(msg)) return 'Plan approved, starting the build';
    if (/^actions=/i.test(msg)) return 'Putting the plan together';
    return null;
  }
  if (evt?.agent === 'planner') {
    const tool = msg.match(/^([a-z_]+)\(/i);
    if (tool) return PLANNER_TOOL_STEPS[tool[1]] || 'Working on the plan';
    return msg || null;
  }
  return null;
}

/** Plain-language summary of a slide event for the "Now" line on the progress board. */
function friendlySlideNow(slide: SlideInfo): string {
  const d = slide.detail.toLowerCase();
  let what: string;
  if (slide.fallback) what = 'placed on a ready-made layout';
  else if (slide.stage === 'failed') what = 'could not be built';
  else if (slide.stage === 'done') what = 'finished';
  else if (slide.stage === 'research') what = 'looking up facts in your sources';
  else if (slide.stage === 'written') what = 'content ready, waiting for design';
  else if (slide.stage === 'retry') {
    if (/visual check/.test(d)) what = 'design check found issues, redesigning';
    else if (/failed; repairing the code|rejected/.test(d)) what = 'slide code failed to run, retrying';
    else if (/template layout|fitting/.test(d)) what = 'retrying on a ready-made layout';
    else if (/quiz options/.test(d)) what = 'fixing the quiz options';
    else what = 'fixing layout issues, retrying';
  } else if (slide.stage === 'design') {
    if (/approved/.test(d)) what = 'design check passed';
    else if (/polishing/.test(d)) what = 'polishing the design';
    else if (/image/.test(d)) what = 'placing the image';
    else if (/answer/.test(d)) what = 'preparing the quiz answer';
    else what = 'coding the slide design';
  } else if (/building (cover|agenda|takeaways|thanks)/.test(d)) what = `creating the ${d.replace(/^.*building\s+/, '')} slide`;
  else what = 'writing the content';
  return `Slide ${slide.index} · ${what}`;
}

/** "Slide 5/23: visual check 1/3: refining the design" -> "visual check 1/3: refining the design" (mirrors the log line). */
function slideDetailFromMessage(msg: string): string {
  return msg
    .replace(/^\s*(?:for\s+)?slide\s+\d+(?:\s*\/\s*\d+)?\s*:?\s*/i, '')
    .replace(/\s+calling the agent\s*$/i, '')
    .trim();
}

/**
 * Stage for a ppt-code (design) event. `design` = the slide is being coded/rendered;
 * `retry` = a visual check or the layout gate rejected the last attempt and it is being redone.
 */
function designStage(phase: string, msg: string, tier: number): string {
  if (phase === 'done' || phase === 'fallback') return 'done';
  if (phase === 'review') return /approved/i.test(msg) ? 'design' : 'retry';
  // "passed; polishing" and image placement are refinements, not failures.
  if (phase === 'repair') return /passed|placing the requested image/i.test(msg) ? 'design' : 'retry';
  // Tier 2+ starts only after the tier-1 designer gave up; answer reveals inherit the question's tier.
  if (phase === 'start') return tier > 1 && !/answer/i.test(msg) ? 'retry' : 'design';
  if (phase) return 'design';
  // Legacy events without a phase.
  if (/rendered|fitted onto/i.test(msg)) return 'done';
  if (/error|fail/i.test(msg) && !/retry|repair|redesign/i.test(msg)) return 'failed';
  return /repair|redesign/i.test(msg) ? 'retry' : 'design';
}

/** Stage for a slide (content-writer) event. Finished content still has to be designed, so it is "written", not done. */
function writeStage(phase: string, verdict: string, msg: string): string | null {
  switch (phase) {
    case 'start': case 'running': case 'write': return 'progress';
    case 'research': return 'research';
    case 'critique': return verdict === 'approve' ? 'written' : 'progress';
    case 'done': return 'written';
    case '': break;
    default: return null;
  }
  const lower = msg.toLowerCase();
  if (/error|fail/.test(lower)) return 'failed';
  if (/complete|done|approve/.test(lower)) return 'written';
  if (/research/.test(lower)) return 'research';
  return 'progress';
}

/**
 * Distils a slide / ppt-code / deck event into status-board data. `prev` is the slide's current state;
 * annotation-only events (image notes, unknown phases) keep it unchanged instead of resetting the chip.
 */
export function parseSlideEvent(evt: StreamEvent, prevOf?: (index: number) => SlideInfo | undefined): SlideInfo | null {
  if (!evt) return null;
  const data = evt.data || {};
  const phase = `${data.phase || ''}`.toLowerCase();
  const isDropped = evt.agent === 'deck' && phase === 'dropped';
  if (evt.agent !== 'slide' && evt.agent !== 'ppt-code' && !isDropped) return null;
  const isDesign = evt.agent === 'ppt-code';
  const msg = evt.message || '';
  let index = Number.parseInt(data.slide_number, 10);
  let total = Number.parseInt(data.total, 10);
  if (!Number.isFinite(index)) {
    const m = msg.match(/slide\s+(\d+)\s*\/\s*(\d+)/i);
    if (!m) return null;
    index = Number.parseInt(m[1], 10);
    total = Number.parseInt(m[2], 10);
  }
  const prev = prevOf?.(index);
  const tier = Number.parseInt(data.tier, 10);
  let detail = slideDetailFromMessage(msg);
  let stage: string | null;
  if (isDropped || phase === 'error' || phase === 'failed') stage = 'failed';
  else if (phase.startsWith('image_')) stage = null;
  else if (isDesign) stage = designStage(phase, msg, tier);
  else stage = writeStage(phase, `${data.verdict || ''}`.toLowerCase(), msg);
  if (stage == null) return prev ? { ...prev } : null;

  const fallback = isDesign && phase === 'fallback';
  if (fallback) detail = `rendered on a template layout after ${data.attempts ?? 'several'} design attempt(s)`;
  else if (isDesign && phase === 'review' && data.score != null) {
    detail += ` · score ${data.score}/10${data.issues ? ` · ${data.issues} issue(s)` : ''}`;
  }
  if (isDesign && Number.isFinite(tier) && tier > 1 && !fallback) detail += ` · tier ${tier}`;
  const attempts = Number.parseInt(data.attempts, 10);
  return {
    index,
    total: Number.isFinite(total) ? total : prev?.total,
    stage,
    detail,
    label: fallback ? 'done · template layout' : SLIDE_LABELS[stage],
    fallback,
    tier: Number.isFinite(tier) ? tier : prev?.tier,
    attempts: Number.isFinite(attempts) ? Math.max(attempts, prev?.attempts || 0) : (isDesign ? prev?.attempts : undefined),
  };
}

export function parseLanguage(evt: StreamEvent): string | null {
  if (!evt || evt.agent !== 'deck') return null;
  const m = (evt.message || '').match(/built\s+(.+?)\s+deck\b/i);
  return m ? m[1].trim() : null;
}

export function stageIndexForEvent(evt: StreamEvent): number {
  const msg = (evt.message || '').toLowerCase();
  if (evt.type === 'done' || evt.type === 'deck') return STAGE_READY;
  if (evt.agent === 'deck') {
    return /design contract|designing\s+\d+\s+slide|layouts were chosen/.test(msg) ? STAGE_DESIGN : STAGE_ASSEMBLE;
  }
  if (evt.agent === 'ppt-code') return STAGE_DESIGN;
  if (evt.agent === 'slide' || evt.agent === 'planner' || evt.agent === 'supervisor') return STAGE_WRITE;
  return -1;
}

/** Folds an event into caller-owned state; no services, DOM or external side effects. */
export function updateFlowState(f: FlowState, evt: StreamEvent): void {
  const msg = `${evt?.message || ''}`;
  const slide = parseSlideEvent(evt, (i) => f.slides.get(i));
  if (slide) {
    f.slides.set(slide.index, slide);
    if (slide.total != null && Number.isFinite(slide.total)) f.slideTotal = Math.max(f.slideTotal, slide.total);
    f.started = true;
    f.now = friendlySlideNow(slide);
    f.nowStage = slide.fallback ? 'retry' : slide.stage;
  }
  if (evt?.agent === 'triage') {
    if (/route=clarify/.test(msg)) f.thinkingNote = 'Preparing a few questions';
    else if (/route=chat/.test(msg)) f.thinkingNote = 'Writing a reply';
  }
  const isBuildEvent = evt?.agent === 'slide' || evt?.agent === 'ppt-code' || evt?.agent === 'deck' || evt?.type === 'deck'
    || (evt?.agent === 'planning' && /plan approved/i.test(msg)) || (evt?.type === 'done' && !!evt.data?.result?.deck_updated);
  if (isBuildEvent) f.mode = 'build';
  else if (f.mode === 'thinking' && (evt?.agent === 'planning' || evt?.agent === 'planner'
    || (evt?.agent === 'triage' && /route=(extraction|approve)/.test(msg)))) f.mode = 'planning';
  const step = friendlyPlanStep(evt);
  if (step && f.activity[f.activity.length - 1] !== step) f.activity.push(step);
  if (evt?.agent === 'deck' && !slide && msg) {
    const deckNow = /designing\s+\d+\s+slide/i.test(msg) ? ['Handing the written slides to the designer', 'design']
      : /designed\s+\d+/i.test(msg) ? ['All slides designed', 'done']
      : /assembled/i.test(msg) ? ['Putting the slides together into one deck', 'design']
      : /built\s+.+\s+deck/i.test(msg) ? [msg.replace(/^built\s+/i, 'Built the ').replace(/\s+v\d+$/i, ''), 'done'] : null;
    if (deckNow) [f.now, f.nowStage] = deckNow;
  }
  const building = msg.match(/building\s+(\d+)\s+of\s+(\d+)\s+slide/i);
  if (building) { f.slideTotal = Math.max(f.slideTotal, Number.parseInt(building[2], 10)); f.started = true; }
  const target = evt?.agent === 'slide'
    ? msg.match(/processing\s+(\d+)\s+changed\s+slide/i)
    : evt?.agent === 'deck' ? msg.match(/designing\s+(\d+)\s+slide/i) : null;
  if (target) f.slideTarget = Number.parseInt(target[1], 10);
  if (target && evt?.agent === 'deck') f.designTarget = Number.parseInt(target[1], 10);
  const lang = parseLanguage(evt);
  if (lang) f.langs.add(lang);
  const langTotal = msg.match(/(\d+)\s+language\(s\)/i);
  if (langTotal) f.langTotal = Number.parseInt(langTotal[1], 10);
  const idx = stageIndexForEvent(evt);
  const isBuildActivity = evt.agent === 'slide' || evt.agent === 'deck' || evt.agent === 'ppt-code'
    || evt.agent === 'planner' || (evt.agent === 'supervisor' && evt.type !== 'done');
  if (idx >= 0 && isBuildActivity) { f.stageIdx = Math.max(f.stageIdx, idx); f.started = true; }
  if (evt.type === 'done' && normalizeRunStatus(evt.data?.status) === 'CANCELLED') {
    f.done = true; f.cancelled = true;
    return;
  }
  if (evt.type === 'done' && (f.started || evt.data?.result?.deck_updated)) {
    f.started = true; f.done = true;
    if (evt.data?.error) f.error = true;
    // A failed run stays on the step it failed at; only a successful finish completes every step.
    if (!evt.data?.error) f.stageIdx = FLOW_STAGES.length - 1;
    // A successful finish means every slide this run touched made it into the deck.
    if (!f.error) {
      for (const s of f.slides.values()) {
        if (ACTIVE_SLIDE_STAGES.has(s.stage)) Object.assign(s, { stage: 'done', label: SLIDE_LABELS['done'] });
      }
    }
  }
  if (evt.type === 'agent_error' && f.started) f.error = true;
  f.pct = Math.max(f.pct, estimatePercent(f));
}

/** Per-slide design progress: coding starts at 15%, each attempt / visual check adds ~18%, capped until it renders. */
function designCredit(s: SlideInfo): number {
  if (s.stage === 'done' || s.stage === 'failed') return 1;
  if (s.stage !== 'design' && s.stage !== 'retry') return 0;
  return Math.min(0.85, 0.15 + 0.18 * (s.attempts || 0));
}

/** Time-weighted estimate: plan + write ≈ 20%, design ≈ 60-70%, assemble + ready ≈ 10-20% (by language count). */
function estimatePercent(f: FlowState): number {
  if (f.done && !f.error && !f.cancelled) return 100;
  if (!f.started) return 0;
  const assemble = assembleShare(f);
  const design = 100 - PCT_WRITE - assemble;

  let write: number;
  if (f.stageIdx >= STAGE_DESIGN) write = 1;
  else if (!f.slides.size) write = Math.min(0.2, 0.05 * Math.max(1, f.activity.length));
  else {
    let credit = 0;
    for (const s of f.slides.values()) credit += s.stage === 'research' ? 0.4 : s.stage === 'progress' ? 0.2 : ACTIVE_SLIDE_STAGES.has(s.stage) || s.stage === 'done' ? 1 : 0;
    write = 0.2 + 0.8 * Math.min(1, credit / Math.max(1, slidesTarget(f)));
  }

  let designed = 0;
  if (f.stageIdx >= STAGE_ASSEMBLE || f.designTarget === 0) designed = 1;
  else if (f.stageIdx >= STAGE_DESIGN) {
    let credit = 0;
    for (const s of f.slides.values()) credit += designCredit(s);
    designed = Math.min(1, credit / Math.max(1, f.designTarget, slidesTarget(f)));
  }

  let assembled = 0;
  if (f.stageIdx >= STAGE_READY) assembled = 0.95;
  else if (f.stageIdx >= STAGE_ASSEMBLE) {
    const langsDone = f.langTotal ? f.langs.size / f.langTotal : f.langs.size ? 0.6 : 0;
    assembled = 0.25 + 0.65 * Math.min(1, langsDone);
  }
  const pct = PCT_WRITE * write + design * designed + assemble * assembled;
  return Math.round(Math.min(99, Math.max(1, pct)));
}

const rowStyleCache = new WeakMap<StreamEvent, [string, string, string]>();
/** [icon, icon class, message class] for a log row; slide events borrow the colour of their chip stage. */
function thoughtRowStyle(evt: StreamEvent): [string, string, string] {
  if (evt && typeof evt === 'object') {
    const hit = rowStyleCache.get(evt);
    if (hit) return hit;
  }
  const [icon, cls] = THOUGHT_STYLE[evt?.type || ''] || ['·', ''];
  let style: [string, string, string] = [icon, cls, cls];
  const slide = evt?.type === 'agent_step' ? parseSlideEvent(evt) : null;
  if (slide?.fallback) style = ['⚠', 't-slide-retry', 't-step'];
  else if (slide && SLIDE_ROW_STYLE[slide.stage]) {
    const [sIcon, sCls] = SLIDE_ROW_STYLE[slide.stage];
    const msgCls = slide.stage === 'done' ? 't-slide-done' : slide.stage === 'failed' ? 't-slide-failed' : 't-step';
    style = [sIcon, sCls, msgCls];
  }
  if (evt && typeof evt === 'object') rowStyleCache.set(evt, style);
  return style;
}
export function thoughtIcon(evt: StreamEvent): string { return thoughtRowStyle(evt)[0]; }
export function thoughtIconClass(evt: StreamEvent): string { return thoughtRowStyle(evt)[1]; }
export function thoughtMsgClass(evt: StreamEvent): string { return thoughtRowStyle(evt)[2]; }
export function flowSummary(f: FlowState): string {
  if (f.cancelled) return 'Stopped';
  if (f.done) return f.error ? 'Completed with issues' : 'Deck ready';
  const cur = f.done ? FLOW_STAGES.length - 1 : Math.max(f.stageIdx, 0);
  const key = FLOW_STAGES[cur]?.key;
  if (key === 'write') return f.slides.size ? `Writing slides ${slidesWrittenCount(f)}/${slidesTarget(f) || '?'}` : 'Planning…';
  if (key === 'design') return `Designing slides ${slidesDoneCount(f)}/${slidesTarget(f) || '?'}`;
  if (key === 'assemble') return f.langs.size
    ? `Assembling deck · ${f.langs.size}${f.langTotal ? `/${f.langTotal}` : ''} language(s)` : 'Assembling deck…';
  if (key === 'ready') return 'Finishing…';
  return 'Planning…';
}
/** Header status copy for a running build ("Planning your deck" until the first slide is being written). */
export function flowStageStatus(f: FlowState): string {
  if (f.stageIdx <= STAGE_WRITE) return f.slides.size ? FLOW_STAGES[STAGE_WRITE].status : 'Planning your deck';
  return FLOW_STAGES[f.stageIdx]?.status || FLOW_STAGES[0].status;
}
export function stepState(f: FlowState, i: number): 'done' | 'active' | 'failed' | 'pending' {
  if (f.cancelled) return i < Math.max(f.stageIdx, 0) ? 'done' : 'pending';
  if (f.done && !f.error) return 'done';
  const cur = Math.max(f.stageIdx, 0);
  if (i < cur) return 'done';
  if (i === cur) return f.error ? 'failed' : 'active';
  return 'pending';
}
export function stepDot(f: FlowState, i: number): string {
  const s = stepState(f, i);
  return s === 'done' ? '✓' : s === 'failed' ? '!' : String(i + 1);
}
export function slidesArr(f: FlowState): SlideInfo[] { return Array.from(f.slides.values()).sort((a, b) => a.index - b.index); }
export function slidesDoneCount(f: FlowState): number {
  let n = 0;
  for (const s of f.slides.values()) if (s.stage === 'done') n++;
  return n;
}
/** Slides whose content is finished (written, being designed/retried, or done). */
export function slidesWrittenCount(f: FlowState): number {
  let n = 0;
  for (const s of f.slides.values()) if (s.stage === 'written' || s.stage === 'design' || s.stage === 'retry' || s.stage === 'done') n++;
  return n;
}
/** Slides this run works on: the backend's changed/designing count, else the deck size. */
export function slidesTarget(f: FlowState): number {
  return Math.max(f.slideTarget || f.slideTotal || 0, f.slides.size);
}
export function slidesRetryCount(f: FlowState): number {
  let n = 0;
  for (const s of f.slides.values()) if (s.stage === 'retry') n++;
  return n;
}
export function slidesProgressLabel(f: FlowState): string {
  const total = slidesTarget(f);
  if (f.done && !f.error && !f.cancelled) return `Slides · ${slidesDoneCount(f)}/${total} ready`;
  if (f.stageIdx <= STAGE_WRITE) return `Slides · ${slidesWrittenCount(f)}/${total} written`;
  const retrying = f.done ? 0 : slidesRetryCount(f);
  return `Slides · ${slidesDoneCount(f)}/${total} designed${retrying ? ` · ${retrying} retrying` : ''}`;
}
export function slideChipTitle(s: SlideInfo): string { return `Slide ${s.index} · ${s.label}${s.detail ? ` — ${s.detail}` : ''}`; }
export function flowPercent(f: FlowState): number {
  return f.done && !f.error && !f.cancelled ? 100 : f.pct;
}
/** Phase boundaries on the progress bar: end of plan & write, end of design. */
export function flowMeterTicks(f: FlowState): number[] { return [PCT_WRITE, 100 - assembleShare(f)]; }
export function langsArr(f: FlowState): string[] { return Array.from(f.langs); }
export function showLangSection(f: FlowState): boolean { return f.langs.size > 0 || f.stageIdx >= STAGE_ASSEMBLE; }
export function showLangBuilding(f: FlowState): boolean {
  return !f.done && f.stageIdx >= STAGE_ASSEMBLE && (!f.langTotal || f.langs.size < f.langTotal);
}
export function buildTurnFromMessage(m: ChatMessage): LiveTurn {
  const flow = newFlowState();
  for (const e of (m.events || [])) updateFlowState(flow, e);
  return { events: m.events || [], title: m.title || 'Thought process', collapsed: true, flow, durationMs: eventsDurationMs(m.events) ?? m.durationMs ?? null };
}

/**
 * Backend-reported wall time (ms) of a finished run, read from the `done` result's
 * `time_taken_minutes`, falling back to `ended_at - started_at`. Null when the backend omitted both.
 */
export function resultDurationMs(result: any): number | null {
  if (!result || typeof result !== 'object') return null;
  if (result.time_taken_minutes != null && result.time_taken_minutes !== '') {
    const mins = Number(result.time_taken_minutes);
    if (Number.isFinite(mins) && mins >= 0) return Math.round(mins * 60_000);
  }
  const start = Date.parse(result.started_at ?? '');
  const end = Date.parse(result.ended_at ?? '');
  if (Number.isFinite(start) && Number.isFinite(end) && end >= start) return end - start;
  return null;
}

/** Backend-reported duration of a run, taken from its last `done` event. */
export function eventsDurationMs(events: StreamEvent[] | null | undefined): number | null {
  if (!events?.length) return null;
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e?.type !== 'done') continue;
    const ms = resultDurationMs(e.data?.result);
    if (ms != null) return ms;
  }
  return null;
}

/** Map a persisted workflow-event timeline row to the live SSE event shape the flow board consumes. */
export function timelineEventToStreamEvent(item: WorkflowEventTimelineItem): StreamEvent {
  return {
    type: item.type,
    agent: item.agent,
    message: item.message,
    data: item.data || {},
    run_id: item.run_id,
    event_index: item.event_index,
    ts: item.ts,
  };
}

/** A run's activity is worth surfacing when it built something concrete or ended in failure. */
export function thoughtWorthShowing(events: StreamEvent[]): boolean {
  if (!events?.length) return false;
  const flow = newFlowState();
  for (const e of events) updateFlowState(flow, e);
  if (flow.slides.size > 0 || flow.langs.size > 0) return true;
  return events.some((e) =>
    e.type === 'agent_error'
    || (e.type === 'done' && (e.data?.result?.deck_updated || e.data?.error)));
}

/** run_id of the in-progress run (RUNNING / RECOVERING / CANCELLING) with no reply after it. */
export function findRunningRunId(timeline: TimelineItem[] | null | undefined): string | null {
  if (!timeline?.length) return null;
  for (let i = timeline.length - 1; i >= 0; i--) {
    const item = timeline[i];
    if (item.kind === 'message') {
      if (item.role !== 'user') return null;
      if (item.run_id && isActiveRunStatus(item.run_status)) return item.run_id;
      continue;
    }
    return isActiveRunStatus(item.run_status) ? item.run_id : null;
  }
  return null;
}

/** Backend status of the newest run referenced by the timeline, or null for legacy rows. */
export function latestTimelineRunStatus(timeline: TimelineItem[] | null | undefined): RunStatus | null {
  if (!timeline?.length) return null;
  for (let i = timeline.length - 1; i >= 0; i--) {
    const item = timeline[i];
    if (item.run_id) return normalizeRunStatus(item.run_status);
  }
  return null;
}

/**
 * Rebuild the ordered chat list (messages + agent-activity boards) from the persisted
 * `timeline`. Workflow events are grouped by `run_id` in backend array order — `event_index`
 * is authoritative, so we never re-sort by timestamp. Each run's activity board is inserted
 * immediately before that run's assistant/system reply; event-only runs (e.g. failures with no
 * reply) flush their board before the next user turn or at the end.
 */
export function buildMessagesFromTimeline(timeline: TimelineItem[], sessionId: string, excludeRunId: string | null = null): ChatMessage[] {
  const out: ChatMessage[] = [];
  let buf: StreamEvent[] = [];
  let bufRun: string | null = null;
  const flush = (reply?: MessageTimelineItem) => {
    if (buf.length && thoughtWorthShowing(buf)) {
      // The run's own reply carries the same result as its `done` event (time taken lives there too).
      const sameRun = reply && reply.role !== 'user' && (!reply.run_id || !bufRun || reply.run_id === bufRun);
      out.push({
        id: null,
        session_id: sessionId,
        role: 'thought',
        content: '',
        events: buf,
        title: 'Thought process',
        created_at: buf[0]?.ts ?? null,
        durationMs: eventsDurationMs(buf) ?? (sameRun ? resultDurationMs(reply!.result) : null),
      });
    }
    buf = [];
    bufRun = null;
  };
  for (const item of timeline) {
    if (item.kind === 'workflow_event') {
      if (excludeRunId && item.run_id === excludeRunId) continue;
      // Questions render as a form under the reply, not as a step on the activity board.
      if (item.type === 'questions') continue;
      if (bufRun !== null && item.run_id !== bufRun) flush();
      bufRun = item.run_id;
      buf.push(timelineEventToStreamEvent(item));
      continue;
    }
    if (item.kind !== 'message') continue;
    // Assistant/system reply closes its run's activity; a new user turn flushes any leftover run.
    flush(item);
    out.push({
      id: item.id ?? null,
      session_id: sessionId,
      role: item.role,
      content: item.content || '',
      result: (item.result as any) ?? null,
      created_at: item.ts ?? null,
      run_id: item.run_id ?? null,
      run_status: normalizeRunStatus(item.run_status),
    });
  }
  flush();
  return out;
}

/** Human-friendly build duration, e.g. "45s", "1m 20s", "2h 5m". */
export function formatDuration(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return '';
  const totalSec = Math.round(ms / 1000);
  if (totalSec < 60) return `${totalSec}s`;
  const mins = Math.floor(totalSec / 60);
  const secs = totalSec % 60;
  if (mins < 60) return secs ? `${mins}m ${secs}s` : `${mins}m`;
  const hrs = Math.floor(mins / 60);
  const remMins = mins % 60;
  return remMins ? `${hrs}h ${remMins}m` : `${hrs}h`;
}