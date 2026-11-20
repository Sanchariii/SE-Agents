import { Injectable } from '@angular/core';

import { Config } from '../../../config';
import { environment } from '../../../environments/environment';

@Injectable({ providedIn: 'root' })
export class ConfigService {
  private readonly defaultBackendUrl = (environment.backendUrl ?? '').replace(/\/$/, '');

  getBackendUrl(): string {
    const fromQuery = new URLSearchParams(location.search).get('backend');
    const stored = localStorage.getItem('se_backend_url');
    return (fromQuery || stored || this.defaultBackendUrl).replace(/\/$/, '');
  }

  setBackendUrl(url: string): void {
    localStorage.setItem('se_backend_url', url);
  }

  getApiBaseUrl(): string {
    return `${this.getBackendUrl()}${Config.API_PREFIX}`;
  }
}
