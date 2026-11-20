import { Injectable } from '@angular/core';
import { BrowserAuthError, PublicClientApplication } from '@azure/msal-browser';

import { LOGIN_SCOPES, msalConfig } from '../../config/msal-config';

@Injectable({ providedIn: 'root' })
export class SsoService {
  private instance?: PublicClientApplication;
  private initPromise?: Promise<void>;

  // MSAL v3+ requires initialize() to resolve before any other API is called.
  private getInstance(): Promise<PublicClientApplication> {
    if (!this.instance) {
      this.instance = new PublicClientApplication(msalConfig);
      this.initPromise = this.instance.initialize();
    }
    return this.initPromise!.then(() => this.instance!);
  }

  /**
   * Opens the Microsoft sign-in popup (Authorization Code + PKCE, handled by MSAL)
   * and resolves with the Entra `{ accessToken, idToken }` to hand to the backend.
   * Throws an Error with a user-friendly message on cancel / blocked / failure.
   */
  async ssoLogin(): Promise<{ accessToken: string; idToken: string; account: any }> {
    const msal = await this.getInstance();
    let result: any;
    try {
      result = await msal.loginPopup({ scopes: LOGIN_SCOPES });
    } catch (err) {
      throw new Error(this.friendlyAuthError(err));
    }
    if (result?.account) msal.setActiveAccount(result.account);
    if (!result?.accessToken || !result?.idToken) {
      throw new Error('Sign-in did not return the expected tokens. Please try again.');
    }
    return { accessToken: result.accessToken, idToken: result.idToken, account: result.account };
  }

  /** Clears MSAL's cached account/tokens for this app (local sign-out; best-effort). */
  async ssoLogout(): Promise<void> {
    try {
      const msal = await this.getInstance();
      await msal.clearCache();
    } catch { /* never let a cache-clear failure block our own logout */ }
  }

  private friendlyAuthError(err: any): string {
    if (err instanceof BrowserAuthError) {
      if (err.errorCode === 'user_cancelled') return 'Sign-in was cancelled.';
      if (err.errorCode === 'popup_window_error' || err.errorCode === 'empty_window_error') {
        return 'The sign-in popup was blocked. Please allow popups for this site and try again.';
      }
    }
    return err?.message || 'Microsoft sign-in failed. Please try again.';
  }
}
