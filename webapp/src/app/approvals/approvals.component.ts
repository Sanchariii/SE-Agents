import { Component, EventEmitter, Input, Output } from '@angular/core';

export interface ApprovalRow {
  session_id: string;
  project_title?: string | null;
  deck_version: number;
  status: string;
  submitted_by?: string | null;
  decided_by?: string | null;
  note?: string | null;
}

export interface ApprovalDecision {
  sessionId: string;
  deckVersion: number;
  action: 'approve' | 'reject';
}

@Component({
  selector: 'app-approvals',
  standalone: false,
  templateUrl: './approvals.component.html',
  styleUrls: ['./approvals.component.scss'],
})
export class ApprovalsComponent {
  @Input() rows: ApprovalRow[] = [];
  @Input() loadingText: string = '';
  @Input() isAdmin: boolean = false;
  @Input() filterValue: string = '';

  @Output() readonly filterChange = new EventEmitter<string>();
  @Output() readonly refresh = new EventEmitter<void>();
  @Output() readonly decision = new EventEmitter<ApprovalDecision>();

  trackApprovalRow(_i: number, a: ApprovalRow): string {
    return `${a.session_id}|${a.deck_version}`;
  }
}