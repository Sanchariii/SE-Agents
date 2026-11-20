import { Component, ElementRef, EventEmitter, HostListener, Input, Output } from '@angular/core';

export type TabName = 'chat' | 'pipeline' | 'approvals' | 'analytics';

@Component({
  selector: 'app-header',
  standalone: false,
  templateUrl: './app-header.component.html',
  styleUrls: ['./app-header.component.scss'],
})
export class AppHeaderComponent {
  @Input() currentTab: TabName = 'chat';
  @Input() connState: string = '';
  @Input() userChipText: string = '—';
  @Input() userChipTitle: string = '';
  @Input() isAdmin: boolean = false;
  @Input() userName: string = 'User';
  @Input() userEmail: string = '';
  @Input() userRoles: string[] = [];
  @Input() userInitial: string = 'U';

  @Output() readonly tabChange = new EventEmitter<TabName>();
  @Output() readonly themeToggle = new EventEmitter<void>();
  @Output() readonly logout = new EventEmitter<void>();

  showRolePopover = false;

  get displayName(): string {
    if (!this.userName) return 'User';
    const first = this.userName.trim().split(/\s+/)[0];
    return first || this.userName;
  }

  get userInitials(): string {
    const parts = (this.userName || '').trim().split(/\s+/).filter(Boolean);
    if (parts.length >= 2) {
      return (parts[0].charAt(0) + parts[parts.length - 1].charAt(0)).toUpperCase();
    }
    return (this.userName.trim().slice(0, 2) || this.userInitial || 'U').toUpperCase();
  }

  constructor(private hostEl: ElementRef) {}

  toggleRolePopover(event: MouseEvent): void {
    event.stopPropagation();
    this.showRolePopover = !this.showRolePopover;
  }

  @HostListener('document:click', ['$event'])
  onDocumentClick(event: MouseEvent): void {
    if (this.showRolePopover && !this.hostEl.nativeElement.contains(event.target)) {
      this.showRolePopover = false;
    }
  }
}