// wa-media.ts — voice/image/pdf decrypt + storage + extraction.
//
// Inbound media (voice notes, images, PDF documents) live encrypted on
// WhatsApp's servers; Baileys' downloadMediaMessage decrypts the bytes
// using the per-message mediaKey. We persist the decrypted bytes locally
// (0600, sha256-keyed filename), surface a stable `media_id` on the
// inbox record, and serve the bytes via GET /media/:id. The agent
// transcribes voice (Whisper) and reads images/PDFs (Claude native).

import pino from "pino";
import { mkdir, writeFile, unlink, chmod } from "node:fs/promises";
import { createHash } from "node:crypto";
import path from "node:path";
import { downloadMediaMessage } from "baileys";
import type { proto } from "baileys";

const logger = pino({ level: process.env.WHATSAPP_LOG_LEVEL ?? "warn" }).child({ mod: "wa-media" });

export type MediaKind = "voice" | "image" | "pdf" | "archive" | "text";

export interface MediaPayload {
  kind: MediaKind;
  mimetype: string;
  seconds: number | null;   // voice only
  caption: string | null;   // image / pdf
  filename: string | null;  // pdf
}

export interface MediaRecord {
  media_id: string;        // sha256 prefix (16 hex chars)
  path: string;
  mimetype: string;
  bytes: number;
  kind: MediaKind;
  ts: number;              // unix ms — for TTL sweep
}

export class MediaTooLongError extends Error {}
export class MediaTooLargeError extends Error {}
export class MediaDecryptError extends Error {}

const EXT_BY_MIME: Record<string, string> = {
  "audio/ogg": "ogg",
  "audio/ogg; codecs=opus": "ogg",
  "audio/mpeg": "mp3",
  "audio/mp4": "m4a",
  "audio/aac": "aac",
  "image/jpeg": "jpg",
  "image/png": "png",
  "image/webp": "webp",
  "application/pdf": "pdf",
  "application/zip": "zip",
  "application/x-zip-compressed": "zip",
  "application/x-zip": "zip",
  "application/json": "json",
  "application/xml": "xml",
  "application/yaml": "yaml",
  "application/x-yaml": "yaml",
  "application/javascript": "js",
  "application/typescript": "ts",
  "application/x-shellscript": "sh",
  "application/x-python": "py",
  "application/sql": "sql",
};

const TEXT_APPLICATION_MIMES = new Set<string>([
  "application/json",
  "application/xml",
  "application/yaml",
  "application/x-yaml",
  "application/javascript",
  "application/typescript",
  "application/x-shellscript",
  "application/x-python",
  "application/sql",
]);

const ARCHIVE_MIMES = new Set<string>([
  "application/zip",
  "application/x-zip-compressed",
  "application/x-zip",
]);

function classifyDocMime(mime: string): MediaKind | null {
  if (mime === "application/pdf") return "pdf";
  if (ARCHIVE_MIMES.has(mime)) return "archive";
  if (mime.startsWith("text/")) return "text";
  if (TEXT_APPLICATION_MIMES.has(mime)) return "text";
  return null;
}

function extFor(mimetype: string): string {
  if (EXT_BY_MIME[mimetype]) return EXT_BY_MIME[mimetype]!;
  const base = mimetype.split(";")[0]!.trim();
  if (EXT_BY_MIME[base]) return EXT_BY_MIME[base]!;
  if (base.startsWith("text/")) return "txt";
  return "bin";
}

export function extractMedia(m: proto.IWebMessageInfo): MediaPayload | null {
  const msg = m.message;
  if (!msg) return null;

  const audio = msg.audioMessage;
  if (audio) {
    return {
      kind: "voice",
      mimetype: audio.mimetype ?? "audio/ogg",
      seconds: typeof audio.seconds === "number" ? audio.seconds : null,
      caption: null,
      filename: null,
    };
  }

  const img = msg.imageMessage;
  if (img) {
    return {
      kind: "image",
      mimetype: img.mimetype ?? "image/jpeg",
      seconds: null,
      caption: img.caption ?? null,
      filename: null,
    };
  }

  // WhatsApp wraps caption-bearing PDFs in `documentWithCaptionMessage`.
  // Plain (no-caption) PDFs arrive directly as `documentMessage`. Accept both.
  const doc =
    msg.documentMessage ??
    msg.documentWithCaptionMessage?.message?.documentMessage ??
    null;
  if (doc) {
    const mime = (doc.mimetype ?? "").split(";")[0]!.trim();
    const kind = classifyDocMime(mime);
    if (kind) {
      return {
        kind,
        mimetype: doc.mimetype ?? mime,
        seconds: null,
        caption: doc.caption ?? null,
        filename: doc.fileName ?? null,
      };
    }
    return null;
  }

  return null;
}

export interface QuotedContext {
  quoted_message_key: string | null;
  quoted_text: string | null;
  quoted_sender: string | null;
}

export function extractQuoted(m: proto.IWebMessageInfo): QuotedContext {
  const msg = m.message;
  const empty: QuotedContext = { quoted_message_key: null, quoted_text: null, quoted_sender: null };
  if (!msg) return empty;

  // Baileys puts contextInfo under whichever message type carries the reply.
  // Caption-bearing PDFs are wrapped, so also probe the inner documentMessage.
  const ctx =
    msg.extendedTextMessage?.contextInfo ??
    msg.imageMessage?.contextInfo ??
    msg.documentMessage?.contextInfo ??
    msg.documentWithCaptionMessage?.message?.documentMessage?.contextInfo ??
    msg.audioMessage?.contextInfo ??
    msg.videoMessage?.contextInfo ??
    msg.stickerMessage?.contextInfo ??
    null;
  if (!ctx?.quotedMessage) return empty;

  const remoteJid = m.key?.remoteJid ?? "";
  const stanzaId = ctx.stanzaId ?? "";
  const key = remoteJid && stanzaId ? `${remoteJid}:${stanzaId}` : null;

  const q = ctx.quotedMessage;
  let raw =
    q.conversation ??
    q.extendedTextMessage?.text ??
    q.imageMessage?.caption ??
    q.documentMessage?.caption ??
    "";
  // Override #5: straight truncation at 1000 chars + ellipsis. No pointer.
  if (raw.length > 1000) raw = raw.slice(0, 1000) + "…";

  return {
    quoted_message_key: key,
    quoted_text: raw || null,
    quoted_sender: ctx.participant ?? null,
  };
}

export interface DecryptOpts {
  dir: string;
  maxAudioSeconds: number;
  maxBytes: number;
}

export async function decryptAndPersist(
  m: proto.IWebMessageInfo,
  payload: MediaPayload,
  opts: DecryptOpts,
): Promise<MediaRecord> {
  // Pre-flight duration check avoids decrypting hours-long voice notes.
  if (
    payload.kind === "voice" &&
    payload.seconds !== null &&
    payload.seconds > opts.maxAudioSeconds
  ) {
    throw new MediaTooLongError(`voice ${payload.seconds}s > ${opts.maxAudioSeconds}s`);
  }

  let buf: Buffer;
  try {
    buf = await downloadMediaMessage(m as Parameters<typeof downloadMediaMessage>[0], "buffer", {});
  } catch (e) {
    throw new MediaDecryptError(String(e));
  }

  if (buf.length > opts.maxBytes) {
    throw new MediaTooLargeError(`${buf.length} > ${opts.maxBytes}`);
  }

  const sha = createHash("sha256").update(buf).digest("hex").slice(0, 16);
  const ext = extFor(payload.mimetype);
  const filename = `${payload.kind}_${sha}.${ext}`;
  const fullPath = path.join(opts.dir, filename);

  await mkdir(opts.dir, { recursive: true });
  await writeFile(fullPath, buf, { mode: 0o600 });
  try { await chmod(fullPath, 0o600); } catch {}

  return {
    media_id: sha,
    path: fullPath,
    mimetype: payload.mimetype,
    bytes: buf.length,
    kind: payload.kind,
    ts: Date.now(),
  };
}

export class MediaStore {
  private map = new Map<string, MediaRecord>();

  register(rec: MediaRecord): void {
    this.map.set(rec.media_id, rec);
  }

  get(media_id: string): MediaRecord | undefined {
    return this.map.get(media_id);
  }

  async sweep(ttlMs: number): Promise<void> {
    const cutoff = Date.now() - ttlMs;
    for (const [id, rec] of this.map.entries()) {
      if (rec.ts < cutoff) {
        this.map.delete(id);
        try {
          await unlink(rec.path);
        } catch (e) {
          logger.debug({ err: String(e), path: rec.path }, "sweep unlink failed");
        }
      }
    }
  }
}
