import { Injectable } from '@angular/core';
@Injectable({ providedIn: 'root' })
export class UiService {
  askText(message: string, defaultValue = ''): Promise<string | null> {
    return new Promise((resolve) => {
      const overlay = document.createElement('div');
      overlay.className = 'modal-overlay';
      overlay.innerHTML = `
        <div class="modal-card">
          <p class="modal-msg"></p>
          <input class="modal-input" type="text" />
          <div class="modal-actions">
            <button type="button" class="btn btn-secondary" data-act="cancel">Cancel</button>
            <button type="button" class="btn btn-primary" data-act="ok">OK</button>
          </div>
        </div>`;
      (overlay.querySelector('.modal-msg') as HTMLElement).textContent = message;
      const input = overlay.querySelector('.modal-input') as HTMLInputElement;
      input.value = defaultValue;
      document.body.appendChild(overlay);
      input.focus();
      input.select();

      const close = (result: string | null) => { overlay.remove(); resolve(result); };
      const okBtn = overlay.querySelector('[data-act="ok"]') as HTMLButtonElement;
      const syncOk = () => { okBtn.disabled = !input.value.trim(); };
      syncOk();
      input.addEventListener('input', syncOk);
      overlay.querySelector('[data-act="cancel"]')!.addEventListener('click', () => close(null));
      okBtn.addEventListener('click', () => { if (input.value.trim()) close(input.value); });
      overlay.addEventListener('click', (e) => { if (e.target === overlay) close(null); });
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && input.value.trim()) close(input.value);
        if (e.key === 'Escape') close(null);
      });
    });
  }

  /** Yes/no confirmation modal (no text input) — returns a boolean. */
  confirmDialog(message: string, confirmLabel = 'Delete'): Promise<boolean> {
    return new Promise((resolve) => {
      const overlay = document.createElement('div');
      overlay.className = 'modal-overlay';
      overlay.innerHTML = `
        <div class="modal-card">
          <p class="modal-msg"></p>
          <div class="modal-actions">
            <button type="button" class="btn btn-secondary" data-act="cancel">Cancel</button>
            <button type="button" class="btn btn-primary" data-act="ok"></button>
          </div>
        </div>`;
      (overlay.querySelector('.modal-msg') as HTMLElement).textContent = message;
      (overlay.querySelector('[data-act="ok"]') as HTMLElement).textContent = confirmLabel;
      document.body.appendChild(overlay);

      const close = (result: boolean) => { overlay.remove(); resolve(result); };
      overlay.querySelector('[data-act="cancel"]')!.addEventListener('click', () => close(false));
      overlay.querySelector('[data-act="ok"]')!.addEventListener('click', () => close(true));
      overlay.addEventListener('click', (e) => { if (e.target === overlay) close(false); });
      document.addEventListener('keydown', function onKey(e) {
        if (e.key === 'Escape') { close(false); document.removeEventListener('keydown', onKey); }
      });
    });
  }

  toast(message: string, kind = 'info'): void {
    let host = document.getElementById('toast-host');
    if (!host) {
      host = document.createElement('div');
      host.id = 'toast-host';
      document.body.appendChild(host);
    }
    const el = document.createElement('div');
    el.className = `toast toast-${kind}`;
    el.textContent = message;
    host.appendChild(el);
    setTimeout(() => el.classList.add('show'), 10);
    setTimeout(() => { el.classList.remove('show'); setTimeout(() => el.remove(), 250); }, 4000);
  }
}
