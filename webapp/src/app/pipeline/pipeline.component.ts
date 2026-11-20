import { Component, EventEmitter, Input, Output } from '@angular/core';

import { Session } from '../models/models';

@Component({
  selector: 'app-pipeline',
  standalone: false,
  templateUrl: './pipeline.component.html',
  styleUrls: ['./pipeline.component.scss'],
})
export class PipelineComponent {
  @Input() sessions: Session[] = [];
  @Input() deletingSessionIds: Set<string> = new Set<string>();

  @Output() readonly openSession = new EventEmitter<string>();
  @Output() readonly download = new EventEmitter<Session>();
  @Output() readonly deleteSession = new EventEmitter<Session>();
  @Output() readonly renameSession = new EventEmitter<Session>();

  trackSessionById(_i: number, s: Session): string { return s.id; }

  pipelineLastMessage(s: Session): string {
    const lastMsg = s.messages[s.messages.length - 1]?.content || '—';
    return (lastMsg || '').slice(0, 60);
  }
}