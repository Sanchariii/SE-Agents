import { BrowserCacheLocation, Configuration } from '@azure/msal-browser';

import { environment } from '../../environments/environment';

const TENANT_ID = environment.aad.tenantId;
const CLIENT_ID = environment.aad.clientId;
const REDIRECT_URI = environment.aad.redirectUri || window.location.origin;

export const msalConfig: Configuration = {
  auth: {
    clientId: CLIENT_ID,
    authority: `https://login.microsoftonline.com/${TENANT_ID}`,
    redirectUri: REDIRECT_URI,
    postLogoutRedirectUri: REDIRECT_URI,
    navigateToLoginRequestUrl: false,
  },
  cache: {
    cacheLocation: BrowserCacheLocation.SessionStorage,
    storeAuthStateInCookie: false,
  },
};

export const LOGIN_SCOPES = ['openid', 'profile', 'offline_access', 'User.Read'];
