import { HttpEvent, HttpHandler, HttpInterceptor, HttpRequest } from '@angular/common/http';
import { Injectable } from '@angular/core';
import { Observable } from 'rxjs';

import { ApiService } from '../services/api.service';

/**
 * Attaches the opaque session bearer token to outgoing HttpClient requests.
 *
 * NOTE: the app's own data layer ({@link ApiService}) intentionally uses `fetch`/`XHR`
 * directly for SSE streaming and blob-upload progress, so it does NOT flow through this
 * interceptor. It is registered for structural parity with the reference project and to
 * transparently authenticate any future HttpClient-based calls.
 */
@Injectable()
export class AuthInterceptor implements HttpInterceptor {
  constructor(private api: ApiService) {}

  intercept(req: HttpRequest<any>, next: HttpHandler): Observable<HttpEvent<any>> {
    const token = this.api.access;
    if (token) {
      req = req.clone({ setHeaders: { Authorization: `Bearer ${token}` } });
    }
    return next.handle(req);
  }
}
