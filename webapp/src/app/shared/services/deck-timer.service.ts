import { Injectable } from '@angular/core';

/** One durable timing record for a deck build. Persisted in IndexedDB so it survives logout/login. */
export interface DeckTimerRecord {
  key: string;               // `${sessionId}:pending` while running, `${sessionId}:${deckVersion}` once finalized
  sessionId: string;
  projectName: string;
  userName: string;
  deckVersion: number | null;
  startedAt: number;         // epoch ms
  endedAt: number | null;    // epoch ms
  durationMs: number | null;
}

/**
 * Persists deck-build durations in IndexedDB.
 *
 * IndexedDB is used on purpose: unlike localStorage/sessionStorage (both wiped on logout by
 * `clearBrowserData`), it survives sign-out/sign-in so an in-flight timer isn't lost. It is only
 * gone when the user clears browser data — in which case the start time is unrecoverable and the
 * caller simply shows no duration. Every method fails soft (never throws) so timing is best-effort
 * and never blocks the chat flow.
 */
@Injectable({ providedIn: 'root' })
export class DeckTimerService {
  private readonly DB_NAME = 'se_deck_timers';
  private readonly STORE = 'timers';
  private dbPromise: Promise<IDBDatabase | null> | null = null;

  private open(): Promise<IDBDatabase | null> {
    if (this.dbPromise) return this.dbPromise;
    this.dbPromise = new Promise((resolve) => {
      try {
        if (typeof indexedDB === 'undefined' || !indexedDB) { resolve(null); return; }
        const req = indexedDB.open(this.DB_NAME, 1);
        req.onupgradeneeded = () => {
          const db = req.result;
          if (!db.objectStoreNames.contains(this.STORE)) {
            const os = db.createObjectStore(this.STORE, { keyPath: 'key' });
            os.createIndex('sessionId', 'sessionId', { unique: false });
          }
        };
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => resolve(null);
      } catch { resolve(null); }
    });
    return this.dbPromise;
  }

  /** Create tx + request synchronously inside the executor so the transaction never goes inactive. */
  private run<T>(mode: IDBTransactionMode, make: (os: IDBObjectStore) => IDBRequest): Promise<T | null> {
    return this.open().then((db) => {
      if (!db) return null;
      return new Promise<T | null>((resolve) => {
        try {
          const os = db.transaction(this.STORE, mode).objectStore(this.STORE);
          const req = make(os);
          req.onsuccess = () => resolve(req.result as T);
          req.onerror = () => resolve(null);
        } catch { resolve(null); }
      });
    });
  }

  private pendingKey(sid: string): string { return `${sid}:pending`; }
  private finalKey(sid: string, v: number): string { return `${sid}:${v}`; }

  /** Record the moment a build turn begins. Overwrites any prior pending for this session. */
  async startPending(sessionId: string, projectName: string, userName: string, startedAt = Date.now()): Promise<void> {
    const rec: DeckTimerRecord = {
      key: this.pendingKey(sessionId), sessionId, projectName, userName,
      deckVersion: null, startedAt, endedAt: null, durationMs: null,
    };
    await this.run('readwrite', (os) => os.put(rec));
  }

  /** Drop a pending start for a turn that ended without producing a deck (questions / failure). */
  async discardPending(sessionId: string): Promise<void> {
    await this.run('readwrite', (os) => os.delete(this.pendingKey(sessionId)));
  }

  /** Finalize with an already-known start time (fast path when the timer is still in memory). */
  async finalizeWith(
    sessionId: string, deckVersion: number, projectName: string, userName: string,
    startedAt: number, endedAt = Date.now(),
  ): Promise<DeckTimerRecord> {
    const rec: DeckTimerRecord = {
      key: this.finalKey(sessionId, deckVersion), sessionId, projectName, userName,
      deckVersion, startedAt, endedAt, durationMs: Math.max(0, endedAt - startedAt),
    };
    await this.run('readwrite', (os) => os.put(rec));
    await this.discardPending(sessionId);
    return rec;
  }

  /** Finalize using the persisted pending start (recovery path after reload/login). Null if no start survived. */
  async finalizePending(sessionId: string, deckVersion: number, endedAt = Date.now()): Promise<DeckTimerRecord | null> {
    const finalK = this.finalKey(sessionId, deckVersion);
    const existing = await this.run<DeckTimerRecord>('readonly', (os) => os.get(finalK));
    if (existing && existing.durationMs != null) {
      await this.discardPending(sessionId);
      return existing;
    }
    const pending = await this.run<DeckTimerRecord>('readonly', (os) => os.get(this.pendingKey(sessionId)));
    if (!pending) return null;
    const rec: DeckTimerRecord = {
      key: finalK, sessionId, projectName: pending.projectName, userName: pending.userName,
      deckVersion, startedAt: pending.startedAt, endedAt, durationMs: Math.max(0, endedAt - pending.startedAt),
    };
    await this.run('readwrite', (os) => os.put(rec));
    await this.discardPending(sessionId);
    return rec;
  }

  /** All finalized durations for a session, keyed by deck version. */
  async listForSession(sessionId: string): Promise<Map<number, DeckTimerRecord>> {
    const out = new Map<number, DeckTimerRecord>();
    const db = await this.open();
    if (!db) return out;
    return new Promise((resolve) => {
      try {
        const os = db.transaction(this.STORE, 'readonly').objectStore(this.STORE);
        const req = os.index('sessionId').openCursor(IDBKeyRange.only(sessionId));
        req.onsuccess = () => {
          const cursor = req.result;
          if (cursor) {
            const rec = cursor.value as DeckTimerRecord;
            if (rec.deckVersion != null && rec.durationMs != null) out.set(rec.deckVersion, rec);
            cursor.continue();
          } else resolve(out);
        };
        req.onerror = () => resolve(out);
      } catch { resolve(out); }
    });
  }

  /** Remove every record for a session (called when a project is deleted). */
  async clearForSession(sessionId: string): Promise<void> {
    const db = await this.open();
    if (!db) return;
    return new Promise((resolve) => {
      try {
        const os = db.transaction(this.STORE, 'readwrite').objectStore(this.STORE);
        const req = os.index('sessionId').openCursor(IDBKeyRange.only(sessionId));
        req.onsuccess = () => {
          const cursor = req.result;
          if (cursor) { cursor.delete(); cursor.continue(); } else resolve();
        };
        req.onerror = () => resolve();
      } catch { resolve(); }
    });
  }
}
