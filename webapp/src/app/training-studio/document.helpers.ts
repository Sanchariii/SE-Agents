import { SessionDoc } from '../models/models';

export function docTitle(d: SessionDoc): string | null {
  const name = d.filename || null;
  if (!d.uploaded_at) return name;
  const uploaded = new Date(d.uploaded_at);
  const when = `Uploaded ${Number.isNaN(uploaded.getTime()) ? d.uploaded_at : uploaded.toLocaleString()}`;
  return name ? `${name}\n${when}` : when;
}
export function docStatusLabel(d: SessionDoc): string {
  if (d.status === 'uploading') return d.pct != null ? `uploading ${d.pct}%` : 'uploading';
  if (d.status === 'upload_in_progress') return 'uploading';
  // Once bytes are in the blob (`uploaded`), backend ingestion is still running.
  // Show "processing" so users don't think the file is done while the spinner keeps spinning.
  if (d.status === 'uploaded') return 'processing';
  return d.status || '';
}
export function docStatusText(d: SessionDoc): string {
  return d.status === 'failed' && d.error ? `${docStatusLabel(d)}: ${d.error}` : docStatusLabel(d);
}
export function docStatusClass(d: SessionDoc): string {
  if (d.status === 'indexed') return 'indexed';
  if (d.status === 'failed' || d.status === 'error') return 'error';
  if (d.status === 'uploaded') return 'processing';
  return '';
}
export function isDocProcessing(d: SessionDoc): boolean {
  const s = d?.status;
  return s !== 'indexed' && s !== 'failed' && s !== 'error';
}

/** A register 400 reporting disabled presigning must use the legacy upload. */
export function isPresignUnavailable(err: any): boolean {
  if (err?.status !== 400) return false;
  const detail = err.detail;
  const msg = (typeof detail === 'string' ? detail : detail?.message || '') || err.message || '';
  return /not available/i.test(msg);
}