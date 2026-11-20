import { Component, EventEmitter, Input, Output } from '@angular/core';
import { ClarifyingQuestion, QuestionOption } from '../../models/models';
import { QuestionFormState } from '../../models/workspace.models';
import { MAX_ANSWER_CHARS } from '../../shared/questions.helpers';

/** Renders the assistant's structured questions: sliders for `scale`, chips for `single_choice`, inputs for `free_text`. */
@Component({
  selector: 'app-question-form',
  templateUrl: './question-form.component.html',
})
export class QuestionFormComponent {
  @Input() form!: QuestionFormState;
  /** The run that asked is still finishing; answers can be picked but not yet submitted. */
  @Input() live = false;
  /** A run or upload is in flight for this project. */
  @Input() busy = false;
  @Output() submitted = new EventEmitter<void>();

  readonly maxChars = MAX_ANSWER_CHARS;
  private static seq = 0;
  readonly uid = `qf${++QuestionFormComponent.seq}`;

  trackById(_i: number, q: ClarifyingQuestion): string { return q.id; }
  trackByValue(_i: number, o: QuestionOption): string { return o.value; }

  inputId(q: ClarifyingQuestion): string { return `${this.uid}-${q.id}`; }

  scaleIndex(q: ClarifyingQuestion): number {
    const i = q.options.findIndex((o) => o.value === this.form.choices[q.id]);
    return i === -1 ? 0 : i;
  }

  scaleOption(q: ClarifyingQuestion): QuestionOption | null {
    return q.options[this.scaleIndex(q)] || null;
  }

  onScaleInput(q: ClarifyingQuestion, event: Event): void {
    const i = Number((event.target as HTMLInputElement).value);
    const option = q.options[i];
    if (option) this.pick(q, option.value);
  }

  isPicked(q: ClarifyingQuestion, o: QuestionOption): boolean {
    return !(this.form.text[q.id] || '').trim() && this.form.choices[q.id] === o.value;
  }

  pick(q: ClarifyingQuestion, value: string): void {
    this.form.choices[q.id] = value;
    this.form.error = '';
  }

  toggleChip(q: ClarifyingQuestion, o: QuestionOption): void {
    // A picked chip replaces any typed answer; clicking it again clears the choice.
    const picked = this.isPicked(q, o);
    this.form.text[q.id] = '';
    this.form.choices[q.id] = picked ? '' : o.value;
    this.form.error = '';
  }

  onText(q: ClarifyingQuestion, event: Event): void {
    this.form.text[q.id] = (event.target as HTMLInputElement).value;
    this.form.error = '';
  }

  get submitDisabled(): boolean {
    return this.live || this.busy || this.form.submitting;
  }
}
