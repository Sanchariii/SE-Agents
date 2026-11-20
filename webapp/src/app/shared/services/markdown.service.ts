import { Injectable } from '@angular/core';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';

@Injectable({ providedIn: 'root' })
export class MarkdownService {
  private mdCache = new Map<string, SafeHtml>();

  constructor(private sanitizer: DomSanitizer) {}

  /** Preserve the existing chat renderer and its bounded cache. `plan` styles a plan review (tags, meta lines). */
  renderMarkdown(src: string | null | undefined, opts: { plan?: boolean } = {}): SafeHtml {
    const text = src ?? '';
    const key = opts.plan ? `\u0001plan\u0001${text}` : text;
    const cached = this.mdCache.get(key);
    if (cached) return cached;
    const html = opts.plan ? this.planReviewToHtml(text) : this.markdownToHtml(text);
    const safe = this.sanitizer.bypassSecurityTrustHtml(html);
    if (this.mdCache.size > 500) this.mdCache.clear();
    this.mdCache.set(key, safe);
    return safe;
  }

  /**
   * A proposed plan is plain lines ("Proposed plan: …", "Style: …", "Quiz answers: …", "Slides:"),
   * so each non-list line becomes its own paragraph instead of one run-on block, and the slide
   * type tags become badges. Text is translated, so unmatched (non-English) text renders as-is.
   */
  private planReviewToHtml(text: string): string {
    const listLine = /^\s*(\d+[.)]|[-*+])\s+/;
    const spaced = text.split(/\r?\n/).map((line, i, all) => {
      const next = all[i + 1];
      return line.trim() && !listLine.test(line) && next !== undefined && next.trim() ? `${line}\n` : line;
    }).join('\n');
    return this.markdownToHtml(spaced)
      .replace(/\[(Quiz answer|Quiz|FAQ)\]\s?/g, (_m, tag: string) => {
        const kind = tag === 'FAQ' ? 'faq' : tag === 'Quiz' ? 'quiz' : 'answer';
        return `<span class="plan-tag plan-tag-${kind}">${tag}</span> `;
      })
      .replace(/<p>(Style|Quiz answers|Audience|Proposed plan):/g, '<p class="plan-meta"><span class="plan-meta-label">$1:</span>');
  }

  private mdEscape(s: string): string {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  private markdownToHtml(input: string): string {
    // Pull fenced code blocks out first so their content isn't formatted.
    const blocks: string[] = [];
    const text = input.replace(/```[a-zA-Z0-9]*\n?([\s\S]*?)```/g, (_m, code) => {
      const i = blocks.push(`<pre class="md-pre"><code>${this.mdEscape(code.replace(/\n$/, ''))}</code></pre>`) - 1;
      return `\u0000B${i}\u0000`;
    });
    const lines = text.split(/\r?\n/);
    const out: string[] = [];
    type ListType = 'ul' | 'ol' | 'outline';
    const tagOf = (t: ListType) => (t === 'ol' ? 'ol' : 'ul');
    const lists: Array<{ type: ListType; indent: number }> = [];
    let para: string[] = [];
    const flushPara = () => {
      if (para.length) { out.push(`<p>${this.inlineMd(para.join(' '))}</p>`); para = []; }
    };
    const closeList = () => {
      while (lists.length) out.push(`</li></${tagOf(lists.pop()!.type)}>`);
    };
    // Indent-based nesting: deeper items open a list inside the open <li>, so "1." items separated by bullets keep counting.
    const openItem = (type: ListType, indent: number, html: string, start = 1) => {
      while (lists.length && lists[lists.length - 1].indent > indent) out.push(`</li></${tagOf(lists.pop()!.type)}>`);
      const top = lists[lists.length - 1];
      if (top && top.indent === indent && top.type !== type) { out.push(`</li></${tagOf(top.type)}>`); lists.pop(); }
      const cur = lists[lists.length - 1];
      if (!cur || cur.indent < indent) {
        const cls = type === 'outline' ? 'md-list md-outline' : 'md-list';
        out.push(`<${tagOf(type)} class="${cls}"${type === 'ol' && start > 1 ? ` start="${start}"` : ''}>`);
        lists.push({ type, indent });
      } else out.push('</li>');
      out.push(`<li>${html}`);
    };
    const isListLine = (l: string) => /^\s*(\d+(\.\d+)*[.)]?|[-*+])\s+/.test(l);
    const tableSep = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
    const boxBorder = /^\s*(\+[-=+]{3,}\+|[┌├└╔╠╚][─═┬┴┼╦╩╬╤╧╪]{3,}[┐┤┘╗╣╝])\s*$/;
    const boxRow = /^\s*(\|.*\||[│║].*[│║])\s*$/;
    const splitRow = (l: string): string[] => {
      let t = l.trim();
      if (t.startsWith('|')) t = t.slice(1);
      if (t.endsWith('|') && !t.endsWith('\\|')) t = t.slice(0, -1);
      return t.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, '|'));
    };
    for (let idx = 0; idx < lines.length; idx++) {
      const line = lines[idx].replace(/\s+$/, '');
      const cb = line.match(/^\u0000B(\d+)\u0000$/);
      if (cb) { flushPara(); closeList(); out.push(blocks[+cb[1]]); continue; }
      if (!line.trim()) {
        flushPara();
        // A blank line between items belongs to the same list; only close when something else follows.
        let next = idx + 1;
        while (next < lines.length && !lines[next].trim()) next++;
        if (!(lists.length && next < lines.length && isListLine(lines[next]))) closeList();
        continue;
      }
      if (line.includes('|') && idx + 1 < lines.length && lines[idx + 1].includes('|') && tableSep.test(lines[idx + 1])) {
        flushPara(); closeList();
        const head = splitRow(line);
        const aligns = splitRow(lines[idx + 1]).map((c) => (c.startsWith(':') && c.endsWith(':') ? 'center' : c.endsWith(':') ? 'right' : ''));
        const rows: string[][] = [];
        idx += 2;
        while (idx < lines.length && lines[idx].trim() && lines[idx].includes('|')) rows.push(splitRow(lines[idx++]));
        idx--;
        const cell = (tag: 'th' | 'td', c: string, i: number) =>
          `<${tag}${aligns[i] ? ` style="text-align:${aligns[i]}"` : ''}>${this.inlineMd(c || '')}</${tag}>`;
        const body = rows.map((r) => `<tr>${head.map((_h, i) => cell('td', r[i], i)).join('')}</tr>`).join('');
        out.push(`<div class="md-table-wrap"><table class="md-table"><thead><tr>${head.map((c, i) => cell('th', c, i)).join('')}</tr></thead><tbody>${body}</tbody></table></div>`);
        continue;
      }
      // ASCII/box-drawing "cards" must keep their alignment, so they are shown verbatim in a monospace block.
      if (boxBorder.test(line)) {
        flushPara(); closeList();
        const box: string[] = [];
        while (idx < lines.length && (boxBorder.test(lines[idx]) || boxRow.test(lines[idx]))) box.push(lines[idx++].replace(/\s+$/, ''));
        idx--;
        out.push(`<pre class="md-pre"><code>${this.mdEscape(box.join('\n'))}</code></pre>`);
        continue;
      }
      const h = line.match(/^(#{1,6})\s+(.*)$/);
      if (h) {
        flushPara(); closeList();
        out.push(`<${h[1].length <= 2 ? 'h4' : 'h5'} class="md-h">${this.inlineMd(h[2])}</${h[1].length <= 2 ? 'h4' : 'h5'}>`);
        continue;
      }
      if (/^(-{3,}|\*{3,}|_{3,})$/.test(line.trim())) {
        flushPara(); closeList(); out.push('<hr class="md-hr">'); continue;
      }
      const bq = line.match(/^>\s?(.*)$/);
      if (bq) {
        flushPara(); closeList();
        out.push(`<blockquote class="md-quote">${this.inlineMd(bq[1])}</blockquote>`);
        continue;
      }
      const outline = line.match(/^(\s*)(\d+(?:\.\d+)+)\.?\s+(.*)$/);
      if (outline) {
        flushPara();
        // Sub-numbers (1.1, 2.3) always hang under the open numbered item, whatever their indentation.
        const top = lists[lists.length - 1];
        const indent = top ? (top.type === 'outline' ? top.indent : top.indent + 1) : outline[1].length;
        openItem('outline', indent, `<span class="md-outline-num">${outline[2]}</span> ${this.inlineMd(outline[3])}`);
        continue;
      }
      const ol = line.match(/^(\s*)(\d+)[.)]\s+(.*)$/);
      if (ol) {
        flushPara();
        openItem('ol', ol[1].length, this.inlineMd(ol[3]), Number(ol[2]));
        continue;
      }
      const ul = line.match(/^(\s*)[-*+]\s+(.*)$/);
      if (ul) {
        flushPara();
        openItem('ul', ul[1].length, this.inlineMd(ul[2]));
        continue;
      }
      closeList();
      para.push(line.trim());
    }
    flushPara(); closeList();
    return out.join('\n');
  }

  private inlineMd(s: string): string {
    const codes: string[] = [];
    let t = s.replace(/`([^`]+)`/g, (_m, c) => {
      const i = codes.push(`<code class="md-code">${this.mdEscape(c)}</code>`) - 1;
      return `\u0000C${i}\u0000`;
    });
    t = this.mdEscape(t);
    t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      (_m, label, url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`);
    t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
         .replace(/__([^_]+)__/g, '<strong>$1</strong>');
    t = t.replace(/(^|[^*])\*([^*\s][^*]*?)\*/g, '$1<em>$2</em>')
         .replace(/(^|[^_\w])_([^_\s][^_]*?)_/g, '$1<em>$2</em>');
    t = t.replace(/\u0000C(\d+)\u0000/g, (_m, i) => codes[+i]);
    return t;
  }
}