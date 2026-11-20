import { CommonModule } from '@angular/common';
import { NgModule } from '@angular/core';
import { FormsModule } from '@angular/forms';

/**
 * Shared module re-exporting the common Angular modules used across feature areas.
 * Kept intentionally lean — the app's reusable UI (modals, toasts) is provided
 * imperatively by UiService rather than as declared components.
 */
@NgModule({
  imports: [CommonModule, FormsModule],
  exports: [CommonModule, FormsModule],
})
export class SharedModule {}
