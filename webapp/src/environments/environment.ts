import { AAD_CLIENT_ID, AAD_TENANT_ID } from '../config';
export const environment = {
  production: false,
  backendUrl: '',
  aad: {
    tenantId: AAD_TENANT_ID,
    clientId: AAD_CLIENT_ID,
    redirectUri: '',
  },
};
