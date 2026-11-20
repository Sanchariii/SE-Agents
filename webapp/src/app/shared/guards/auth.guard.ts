import { Injectable } from '@angular/core';
import { CanActivate } from '@angular/router';

import { ApiService } from '../services/api.service';

@Injectable({ providedIn: 'root' })
export class AuthGuard implements CanActivate {
  constructor(private api: ApiService) {}

  canActivate(): boolean {
    return this.api.loggedIn && !this.api.sessionExpired;
  }
}
