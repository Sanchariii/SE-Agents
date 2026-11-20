import { Injectable } from '@angular/core';
import { BehaviorSubject } from 'rxjs';

@Injectable({ providedIn: 'root' })
export class ThemeService {
  private readonly themeSubject = new BehaviorSubject<'light' | 'dark'>('light');
  readonly theme$ = this.themeSubject.asObservable();

  get isDark(): boolean {
    return this.themeSubject.value === 'dark';
  }

  toggleTheme(): void {
    const isLight = document.documentElement.getAttribute('data-theme') === 'light';
    const next: 'light' | 'dark' = isLight ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', next);
    try {
      localStorage.setItem('se_theme', next);
    } catch {}
    this.themeSubject.next(next);
  }

  /** Match the existing pre-paint script on every shell initialization. */
  applyStoredTheme(): void {
    let theme: 'light' | 'dark' = 'light';
    try {
      const saved = localStorage.getItem('se_theme');
      theme = (saved === 'light' || saved === 'dark') ? saved : 'light';
    } catch {}
    document.documentElement.setAttribute('data-theme', theme);
    this.themeSubject.next(theme);
  }
}
