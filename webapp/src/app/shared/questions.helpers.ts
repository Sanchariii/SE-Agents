import type { ChatMessage, ClarifyingQuestion, QuestionAnswers, QuestionOption, QuestionType } from '../models/models';
import type { QuestionFormState, QuestionSummary } from '../models/workspace.models';

/** Pure logic for the structured questionnaire; no Angular, DOM or network access. */

/** Ids the backend accepts in `answers` (`QuestionIds.ALL`); anything else would be rejected with a 400. */
export const QUESTION_IDS = [
  'goal', 'audience', 'deck_type', 'requirements', 'creativity_level', 'detailing_level', 'quiz_answer_placement',
  'number_of_slides',
] as const;
/** LOW/MED/HIGH sliders; their answers are sent upper-cased. */
export const SCALE_IDS = ['creativity_level', 'detailing_level'];
export const SCALE_ORDER = ['LOW', 'MED', 'HIGH'];
/** Deck-length slider; always rendered as a scale over slide-count ranges. */
export const SLIDE_COUNT_ID = 'number_of_slides';
export const SLIDE_COUNT_VALUES = ['5-10', '10-15', '15-20', '20+'];
export const MAX_ANSWER_CHARS = 500;
/** Fixed chat command the backend treats as "approve the proposed plan". */
export const APPROVE_COMMAND = '/approve-plan';
export const APPROVE_LABEL = 'Approve plan';

const QUESTION_TYPES: QuestionType[] = ['single_choice', 'free_text', 'scale'];
const SCALE_FALLBACK: QuestionOption[] = [
  { value: 'LOW', label: 'Low', description: '' },
  { value: 'MED', label: 'Medium', description: '' },
  { value: 'HIGH', label: 'High', description: '' },
];
const SLIDE_COUNT_FALLBACK: QuestionOption[] = SLIDE_COUNT_VALUES.map((v) => ({ value: v, label: v, description: '' }));
/** Mirrors the backend's readable answer names (`question_model._ANSWER_NAMES`). */
const ANSWER_NAMES: Record<string, string> = {
  creativity_level: 'Creativity', detailing_level: 'Detailing', quiz_answer_placement: 'Quiz answers',
  goal: 'Goal', audience: 'Audience', deck_type: 'Deck type', requirements: 'Requirements',
  number_of_slides: 'Number of slides',
};
const LEVEL_NAMES: Record<string, string> = { LOW: 'Low', MED: 'Medium', HIGH: 'High' };

function str(value: unknown): string {
  return value == null ? '' : String(value).trim();
}

function normalizeOption(raw: unknown): QuestionOption | null {
  if (typeof raw === 'string') {
    const v = raw.trim();
    return v ? { value: v, label: v, description: '' } : null;
  }
  if (!raw || typeof raw !== 'object') return null;
  const o = raw as Record<string, unknown>;
  const value = str(o['value']) || str(o['label']);
  if (!value) return null;
  return { value, label: str(o['label']) || value, description: str(o['description']) };
}

function scaleRank(value: string): number {
  const i = SCALE_ORDER.indexOf(value.toUpperCase());
  return i === -1 ? SCALE_ORDER.length : i;
}

/** Orders slide-count ranges by their lower bound (`5-10` < `10-15` < `20+`); unparseable ones go last. */
function slideRank(o: QuestionOption): number {
  const m = /\d+/.exec(o.value) || /\d+/.exec(o.label);
  return m ? Number(m[0]) : Number.MAX_SAFE_INTEGER;
}

/**
 * Tolerant parse of `questions` from an event or a stored result. Unknown types degrade to free
 * text, plain strings become free-text questions (`q1`, `q2`, …), malformed items are dropped.
 */
export function normalizeQuestions(raw: unknown): ClarifyingQuestion[] {
  if (!Array.isArray(raw)) return [];
  const out: ClarifyingQuestion[] = [];
  const seen = new Set<string>();
  raw.forEach((item, i) => {
    let q: ClarifyingQuestion | null = null;
    if (typeof item === 'string') {
      const text = item.trim();
      if (text) q = { id: `q${i + 1}`, type: 'free_text', text, options: [], default: '', required: true, allow_free_text: true };
    } else if (item && typeof item === 'object') {
      const r = item as Record<string, unknown>;
      const id = str(r['id']) || `q${i + 1}`;
      const text = str(r['text']);
      if (text) {
        const declared = str(r['type']).toLowerCase() as QuestionType;
        let type: QuestionType = QUESTION_TYPES.includes(declared) ? declared : 'free_text';
        let options = (Array.isArray(r['options']) ? r['options'] : [])
          .map(normalizeOption).filter((o): o is QuestionOption => !!o);
        if (id === SLIDE_COUNT_ID) {
          type = 'scale';
          if (!options.length) options = SLIDE_COUNT_FALLBACK.map((o) => ({ ...o }));
          options = [...options].sort((a, b) => slideRank(a) - slideRank(b));
        } else if (type === 'scale') {
          if (!options.length) options = SCALE_FALLBACK.map((o) => ({ ...o }));
          options = [...options].sort((a, b) => scaleRank(a.value) - scaleRank(b.value));
        }
        if (type === 'single_choice' && !options.length) type = 'free_text';
        q = {
          id, type, text, options,
          default: str(r['default']),
          required: r['required'] !== false,
          allow_free_text: type === 'free_text' ? true : r['allow_free_text'] === true,
        };
      }
    }
    if (q && !seen.has(q.id)) { seen.add(q.id); out.push(q); }
  });
  return out;
}

/** Identity of a question set, so a re-render of the same questions keeps the user's draft. */
export function questionsSignature(questions: ClarifyingQuestion[]): string {
  return questions.map((q) => `${q.id}:${q.type}:${q.text}`).join('|');
}

function scaleDefault(q: ClarifyingQuestion): string {
  const values = q.options.map((o) => o.value);
  const wanted = q.default.toUpperCase();
  if (values.includes(wanted)) return wanted;
  if (values.includes(q.default)) return q.default;
  if (values.includes('MED')) return 'MED';
  return values[Math.floor(values.length / 2)] || '';
}

export function newQuestionForm(questions: ClarifyingQuestion[]): QuestionFormState {
  const choices: QuestionAnswers = {};
  for (const q of questions) {
    if (q.type === 'scale') choices[q.id] = scaleDefault(q);
    else if (q.type === 'single_choice' && q.default && q.options.some((o) => o.value === q.default)) choices[q.id] = q.default;
  }
  return { signature: questionsSignature(questions), questions, choices, text: {}, submitting: false, error: '' };
}

/** The effective answer for one question: typed text wins over a picked chip. */
export function answerFor(form: QuestionFormState, q: ClarifyingQuestion): string {
  if (q.type !== 'scale') {
    const typed = str(form.text[q.id]);
    if (typed) return typed;
  }
  return str(form.choices[q.id]);
}

/** Required questions still unanswered (scales always carry a value). */
export function missingRequired(form: QuestionFormState): ClarifyingQuestion[] {
  return form.questions.filter((q) => q.required && !answerFor(form, q));
}

/**
 * Splits the form into the `/invoke` payload. Known ids go in `answers` (only non-empty ones);
 * answers to ids the backend doesn't know (e.g. `q1` from an older backend) are sent as message
 * text instead, so they never trigger a 400.
 */
export function buildSubmission(form: QuestionFormState): { message: string; answers: QuestionAnswers } {
  const answers: QuestionAnswers = {};
  const typed: string[] = [];
  for (const q of form.questions) {
    let value = answerFor(form, q).slice(0, MAX_ANSWER_CHARS);
    if (!value) continue;
    if (SCALE_IDS.includes(q.id)) value = value.toUpperCase();
    if ((QUESTION_IDS as readonly string[]).includes(q.id)) answers[q.id] = value;
    else typed.push(`${q.text} ${value}`);
  }
  return { message: typed.join('\n'), answers };
}

function displayValue(id: string, value: string, questions: ClarifyingQuestion[]): string {
  const q = questions.find((x) => x.id === id);
  const opt = q?.options.find((o) => o.value === value || o.value.toUpperCase() === value.toUpperCase());
  if (opt?.label) return opt.label;
  return SCALE_IDS.includes(id) ? (LEVEL_NAMES[value.toUpperCase()] || value) : value;
}

/** Readable text for an answers-only turn, matching the backend's synthesized user message. */
export function describeAnswers(answers: QuestionAnswers): string {
  return Object.entries(answers)
    .map(([id, v]) => `${ANSWER_NAMES[id] || id}: ${SCALE_IDS.includes(id) ? (LEVEL_NAMES[v.toUpperCase()] || v) : v}.`)
    .join(' ');
}

export function summarizeAnswers(answers: QuestionAnswers, questions: ClarifyingQuestion[]): QuestionSummary | null {
  const entries = Object.entries(answers || {})
    .filter(([, v]) => str(v))
    .map(([id, v]) => ({
      label: ANSWER_NAMES[id] || questions.find((q) => q.id === id)?.text || id,
      value: displayValue(id, str(v), questions),
    }));
  return entries.length ? { entries } : null;
}

/** Questions carried by a stored assistant message that is (or was) waiting for answers. */
export function questionsOf(m: ChatMessage | null | undefined): ClarifyingQuestion[] {
  if (!m || (m.role !== 'assistant' && m.role !== 'system') || !m.result?.awaiting_answers) return [];
  return normalizeQuestions(m.result.questions);
}

/** The reply minus its numbered question list (the form renders those); null when nothing is left. */
export function stripQuestionList(reply: string | null | undefined): string | null {
  const lines = String(reply || '').split(/\r?\n/);
  const first = lines.findIndex((l) => /^\s*1[.)]\s+\S/.test(l));
  if (first === -1) return String(reply || '');
  const intro = lines.slice(0, first).join('\n').trim();
  return intro || null;
}

/** Autopilot's reply to any open-ended question. */
export const AUTOPILOT_ASSUME_TEXT = 'Assume the best based on the provided input documents.';
export const AUTOPILOT_SLIDES_TEXT = '15-20 slides';
/** Reply when the assistant asks in plain text without a structured form. */
export const AUTOPILOT_PLAIN_REPLY = `${AUTOPILOT_ASSUME_TEXT} Use medium creativity and target ${AUTOPILOT_SLIDES_TEXT}.`;

const SLIDE_COUNT_RE = /how many slides|number of slides|slide count|no\.? of slides|deck length|\blength\b|how long/i;

/**
 * Answers autopilot gives a questionnaire: creativity → Medium, other sliders → their default,
 * slide-count questions → 15-20 slides, everything else → the backend default or "assume the best".
 */
export function autopilotSubmission(questions: ClarifyingQuestion[]): { message: string; answers: QuestionAnswers; picked: QuestionAnswers } {
  const form = newQuestionForm(questions);
  for (const q of questions) {
    const values = q.options.map((o) => o.value);
    if (q.type === 'scale') {
      const slideOpt = q.id === SLIDE_COUNT_ID
        ? q.options.find((o) => /\b15\s*(?:-|–|to)\s*20\b/i.test(`${o.value} ${o.label}`))
        : undefined;
      form.choices[q.id] = slideOpt ? slideOpt.value
        : q.id === 'creativity_level' && values.includes('MED') ? 'MED' : scaleDefault(q);
      continue;
    }
    const aboutSlides = SLIDE_COUNT_RE.test(`${q.id} ${q.text}`);
    if (q.type === 'free_text') {
      form.text[q.id] = aboutSlides ? AUTOPILOT_SLIDES_TEXT : AUTOPILOT_ASSUME_TEXT;
      continue;
    }
    // single_choice
    if (aboutSlides && q.allow_free_text) { form.text[q.id] = AUTOPILOT_SLIDES_TEXT; continue; }
    const slideOpt = aboutSlides
      ? (q.options.find((o) => /\b15\s*(?:-|–|to)\s*20\b/i.test(`${o.value} ${o.label}`))
        || q.options.find((o) => /\bmedium\b/i.test(`${o.value} ${o.label}`)))
      : undefined;
    if (slideOpt) form.choices[q.id] = slideOpt.value;
    else if (q.default && values.includes(q.default)) form.choices[q.id] = q.default;
    else if (q.allow_free_text) form.text[q.id] = AUTOPILOT_ASSUME_TEXT;
    else if (values.length) form.choices[q.id] = values[0];
  }
  // No question asked about length: carry the slide target in the free-text requirements answer.
  const asked = questions.some((q) => q.id === SLIDE_COUNT_ID || (q.type !== 'scale' && SLIDE_COUNT_RE.test(`${q.id} ${q.text}`)));
  const req = questions.find((q) => q.id === 'requirements' && q.type !== 'scale');
  if (!asked && req && (req.type === 'free_text' || req.allow_free_text) && !form.choices[req.id]) {
    form.text[req.id] = `${AUTOPILOT_ASSUME_TEXT} Target ${AUTOPILOT_SLIDES_TEXT}.`;
  }
  const picked: QuestionAnswers = {};
  for (const q of questions) {
    const v = answerFor(form, q);
    if (v) picked[q.id] = v;
  }
  return { ...buildSubmission(form), picked };
}

/** Shows the fixed approve command as the button label it came from. */
export function userBubbleText(content: string | null | undefined): string {
  const text = content || '';
  return text.trim() === APPROVE_COMMAND ? APPROVE_LABEL : text;
}
