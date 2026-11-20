import { Injectable } from '@angular/core';

import { ChatMessage, Session, SessionDoc, StreamEvent } from '../../models/models';

@Injectable({ providedIn: 'root' })
export class StoreService {
  private readonly BASE_KEY = 'se_sessions';

  private keyForUser(): string {
    try {
      const user = JSON.parse(localStorage.getItem('se_user') || 'null');
      if (user && user.id != null) return `${this.BASE_KEY}:${user.id}`;
    } catch { /* fall through to shared key */ }
    return this.BASE_KEY;
  }

  private load(): Session[] {
    try { return JSON.parse(localStorage.getItem(this.keyForUser()) || '[]'); } catch { return []; }
  }

  private save(sessions: Session[]): void {
    localStorage.setItem(this.keyForUser(), JSON.stringify(sessions));
  }

  all(): Session[] {
    return this.load();
  }

  get(id: string): Session | null {
    return this.load().find((s) => s.id === id) || null;
  }

  create(title?: string): Session {
    const sessions = this.load();
    const session: Session = {
      id: `web-${Date.now().toString(36)}`,
      title: title || 'Untitled session',
      status: 'new',            // new | running | answered | done | failed
      deckVersion: null,
      verdict: null,
      messages: [],
      docs: [],
      createdAt: new Date().toISOString(),
    };
    sessions.unshift(session);
    this.save(sessions);
    return session;
  }

  update(id: string, patch: Partial<Session>): Session | null {
    const sessions = this.load();
    const idx = sessions.findIndex((s) => s.id === id);
    if (idx === -1) return null;
    sessions[idx] = { ...sessions[idx], ...patch };
    this.save(sessions);
    return sessions[idx];
  }

  remove(id: string): void {
    this.save(this.load().filter((s) => s.id !== id));
  }

  /**
   * Insert a server-restored project if it isn't already tracked locally.
   * Never clobbers an existing local entry (which may hold richer live state).
   */
  upsert(session: Partial<Session> & { id: string }): void {
    const sessions = this.load();
    if (sessions.some((s) => s.id === session.id)) return;
    sessions.push({
      status: 'new', deckVersion: null, verdict: null, messages: [], docs: [],
      createdAt: new Date().toISOString(), ...session,
    } as Session);
    // Newest first, matching create().
    sessions.sort((a, b) => (b.createdAt || '').localeCompare(a.createdAt || ''));
    this.save(sessions);
  }

  /** Add a live message using the same shape as the durable API contract. */
  addMessage(id: string, role: ChatMessage['role'], content: string, result: any = null, extra: Partial<ChatMessage> = {}): void {
    const session = this.get(id);
    if (!session) return;
    const entry: ChatMessage = {
      id: null,
      session_id: id,
      role,
      content,
      result,
      created_at: new Date().toISOString(),
      ...extra,
    };
    session.messages.push(entry);
    this.update(id, { messages: session.messages });
  }

  /** Persist a client-only "thought process" group so its final progress board survives in the chat log. */
  addThought(id: string, events: StreamEvent[], title = 'Thought process', durationMs: number | null = null): void {
    const session = this.get(id);
    if (!session) return;
    const entry: ChatMessage = {
      id: null,
      session_id: id,
      role: 'thought',
      content: '',
      events,
      title,
      durationMs,
      result: null,
      created_at: new Date().toISOString(),
    };
    session.messages.push(entry);
    this.update(id, { messages: session.messages });
  }

  setDocs(id: string, docs: SessionDoc[]): void {
    this.update(id, { docs });
  }
}
