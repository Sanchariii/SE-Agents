import { Component, OnDestroy, OnInit } from '@angular/core';
import { ApprovalDecision } from '../models/workspace.models';
import { ThemeService } from '../shared/services/theme.service';
import { WorkspaceService } from '../shared/services/workspace.service';

@Component({
  selector: 'app-main',
  templateUrl: './main.component.html',
  styleUrls: ['./main.component.scss'],
  providers: [WorkspaceService],
})
export class MainComponent implements OnInit, OnDestroy {
  constructor(public vm: WorkspaceService, public theme: ThemeService) {}

  ngOnInit(): void { this.vm.initialize(); }
  ngOnDestroy(): void { this.vm.destroy(); }

  onApprovalsFilterChange(value: string): void {
    this.vm.approvalsFilterValue = value;
    this.vm.loadApprovals();
  }

  onDecision(event: ApprovalDecision): void {
    this.vm.decide(event.sessionId, event.deckVersion, event.action);
  }
}
