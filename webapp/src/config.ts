export const AAD_TENANT_ID = '855b093e-7340-45c7-9f0c-96150415893e';
export const AAD_CLIENT_ID = 'd302c3ae-d44a-437c-bd0d-f9fc67c47608';

export class Config {
  static readonly API_PREFIX = '/api';

  static readonly API = {
    AUTH_LOGIN: '/auth/login',
    AUTH_VALIDATE: '/auth/validate',
    AUTH_REFRESH: '/auth/refresh',
    AUTH_ME: '/auth/me',
    AUTH_LOGOUT: '/auth/logout',

    ANALYTICS_DASHBOARD: '/analytics/dashboard',
    ANALYTICS_PROJECTS: '/analytics/projects',
    ANALYTICS_PROJECT: (sessionId: string) => `/analytics/projects/${encodeURIComponent(sessionId)}`,

    DECK_SESSIONS: '/decks/sessions',
    DECK: (sessionId: string) => `/decks/${sessionId}`,
    DECK_TITLE: (sessionId: string) => `/decks/${encodeURIComponent(sessionId)}/title`,
    DECK_MESSAGES: (sessionId: string) => `/decks/${sessionId}/messages`,
    DECK_STREAM: (sessionId: string) => `/decks/${sessionId}/stream`,
    DECK_INVOKE: (sessionId: string) => `/decks/${sessionId}/invoke`,
    DECK_STOP: (sessionId: string) => `/decks/${sessionId}/stop`,
    DECK_RUN_CURRENT: (sessionId: string) => `/decks/${sessionId}/runs/current`,
    DECK_RUN: (sessionId: string, runId: string) => `/decks/${sessionId}/runs/${encodeURIComponent(runId)}`,
    DECK_RUN_EVENTS: (sessionId: string, runId: string, afterEventIndex: number) =>
      `/decks/${sessionId}/runs/${encodeURIComponent(runId)}/events?after_event_index=${afterEventIndex}`,
    DECK_DOCUMENTS: (sessionId: string) => `/decks/${sessionId}/documents`,
    DECK_DOCUMENTS_REGISTER: (sessionId: string) => `/decks/${sessionId}/documents/register`,
    DECK_DOCUMENTS_COMPLETE: (sessionId: string) => `/decks/${sessionId}/documents/complete`,
    DECK_DOCUMENT: (sessionId: string, docId: string) => `/decks/${sessionId}/documents/${docId}`,
    DECK_DOCUMENT_ABORT: (sessionId: string, docId: string) => `/decks/${sessionId}/documents/${docId}/abort`,
    DECK_VERSION: (sessionId: string, version: number) => `/decks/${sessionId}/versions/${version}`,
    DECK_DOWNLOAD_URL: (sessionId: string, version: number) =>
      `/decks/${encodeURIComponent(sessionId)}/versions/${version}/download-url`,
    DECK_FEEDBACK: (sessionId: string, version: number) =>
      `/decks/${encodeURIComponent(sessionId)}/versions/${encodeURIComponent(version)}/feedback`,
  };
}
