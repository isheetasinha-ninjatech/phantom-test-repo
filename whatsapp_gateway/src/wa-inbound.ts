// wa-inbound.ts — in-memory ring inbox + allowlist + echo loop prevention.
// Reference behavior (not source): openclaw/extensions/whatsapp/src/auto-reply/monitor/echo.ts

import pino from "pino";
import type { WASocket, proto } from "baileys";
import { proto as protoRuntime } from "baileys";
import {
  conversationIdForDm,
  conversationIdForGroup,
  isGroupJid,
  jidToPhone,
} from "./normalize-jid.js";

const logger = pino({ level: process.env.WHATSAPP_LOG_LEVEL ?? "warn" }).child({ mod: "wa-inbound" });

export interface InboxRecord {
  seq: number;
  provider: "whatsapp";
  workspace_id: string | null; // self_e164 acts as workspace for POC
  user_id: string | null; // sender e164
  channel_id: string; // conversation_id
  thread_id: string | null;
  text: string;
  from_me: boolean;
  ts: number; // ms
  message_key: string | null; // `${remoteJid}:${messageId}` for dedupe
}

const INBOX_CAP = 1000;

// Baileys 7.x HistorySyncType: 0=INITIAL_BOOTSTRAP, 1=INITIAL_STATUS_V3,
// 2=FULL, 3=RECENT, 4=PUSH_NAME, 5=NON_BLOCKING_DATA, 6=ON_DEMAND.
// Accept every type that carries actual messages; skip metadata-only types
// (INITIAL_STATUS_V3, PUSH_NAME, NON_BLOCKING_DATA).
const HST = protoRuntime.HistorySync.HistorySyncType;
const ACCEPTED_HISTORY_SYNC_TYPES = new Set<number>([
  HST.INITIAL_BOOTSTRAP,
  HST.RECENT,
  HST.FULL,
  HST.ON_DEMAND,
]);

export function shouldIngestAppend(
  messageTimestampSec: number | undefined | null,
  nowMs: number,
  maxAgeMs: number,
): boolean {
  const sec = Number(messageTimestampSec ?? 0);
  if (!sec || sec <= 0) return false;
  const tsMs = sec * 1000;
  return nowMs - tsMs <= maxAgeMs;
}
const RECENTLY_SENT_CAP = 64;
const RECENTLY_SENT_TTL_MS = 60_000;

export class Inbox {
  private buf: InboxRecord[] = [];
  private nextSeq = 1;

  append(rec: Omit<InboxRecord, "seq">): InboxRecord {
    const full: InboxRecord = { ...rec, seq: this.nextSeq++ };
    this.buf.push(full);
    if (this.buf.length > INBOX_CAP) this.buf.splice(0, this.buf.length - INBOX_CAP);
    return full;
  }

  since(seq: number, limit: number): InboxRecord[] {
    const out: InboxRecord[] = [];
    for (const r of this.buf) {
      if (r.seq > seq) {
        out.push(r);
        if (out.length >= limit) break;
      }
    }
    return out;
  }

  latestSeq(): number {
    return this.nextSeq - 1;
  }

  hasMessageKey(key: string): boolean {
    if (!key) return false;
    for (let i = this.buf.length - 1; i >= 0; i--) {
      if (this.buf[i]!.message_key === key) return true;
    }
    return false;
  }
}

interface RecentlySentEntry {
  text: string;
  to: string;
  ts: number;
}

export class RecentlySent {
  private ring: RecentlySentEntry[] = [];

  add(to: string, text: string): void {
    this.ring.push({ to, text, ts: Date.now() });
    if (this.ring.length > RECENTLY_SENT_CAP) this.ring.shift();
  }

  matches(to: string, text: string): boolean {
    const now = Date.now();
    for (let i = this.ring.length - 1; i >= 0; i--) {
      const e = this.ring[i]!;
      if (now - e.ts > RECENTLY_SENT_TTL_MS) continue;
      if (e.to === to && e.text === text) return true;
    }
    return false;
  }
}

export interface InboundConfig {
  echo: boolean;
  allowedTo: Set<string>; // e164 allowlist
  selfE164Getter: () => string | null;
}

export interface EventCounters {
  history_set: number;
  history_set_skipped: number;
  history_set_by_sync_type: Record<string, number>;
  upsert_notify: number;
  upsert_append: number;
  upsert_other: number;
  ingested_total: number;
  ingested_by_source: Record<"notify" | "append" | "history", number>;
  skipped_no_ts: number;
  skipped_stale: number;
  skipped_dup: number;
}

export function makeEventCounters(): EventCounters {
  return {
    history_set: 0,
    history_set_skipped: 0,
    history_set_by_sync_type: {},
    upsert_notify: 0,
    upsert_append: 0,
    upsert_other: 0,
    ingested_total: 0,
    ingested_by_source: { notify: 0, append: 0, history: 0 },
    skipped_no_ts: 0,
    skipped_stale: 0,
    skipped_dup: 0,
  };
}

export interface InboundDeps {
  sock: () => WASocket | null;
  inbox: Inbox;
  recentlySent: RecentlySent;
  cfg: InboundConfig;
  historyMaxAgeMs: number;
  noteHistoryActivity?: () => void;
  counters: EventCounters;
}

type IngestSource = "notify" | "append" | "history";

export function makeUpsertHandler(deps: InboundDeps) {
  return async (ev: { messages: proto.IWebMessageInfo[]; type: string }) => {
    const self = deps.cfg.selfE164Getter();
    let source: IngestSource;
    if (ev.type === "append") {
      source = "append";
      deps.counters.upsert_append += 1;
      if (deps.noteHistoryActivity) deps.noteHistoryActivity();
    } else if (ev.type === "notify") {
      source = "notify";
      deps.counters.upsert_notify += 1;
    } else {
      source = "notify"; // treat unknown as live; rare
      deps.counters.upsert_other += 1;
    }
    for (const m of ev.messages ?? []) {
      try {
        await handleOne(m, deps, self, source);
      } catch (e) {
        logger.error({ err: String(e) }, "inbound handler error");
      }
    }
  };
}

export function makeHistorySetHandler(deps: InboundDeps) {
  return async (h: {
    messages?: proto.IWebMessageInfo[];
    syncType?: number;
    isLatest?: boolean;
  }) => {
    const syncType = Number(h?.syncType ?? -1);
    const count = Array.isArray(h?.messages) ? h!.messages!.length : 0;
    deps.counters.history_set += 1;
    const key = String(syncType);
    deps.counters.history_set_by_sync_type[key] =
      (deps.counters.history_set_by_sync_type[key] ?? 0) + 1;
    logger.info({ event: "history_set_received", syncType, count }, "messaging-history.set");
    if (!ACCEPTED_HISTORY_SYNC_TYPES.has(syncType)) {
      deps.counters.history_set_skipped += 1;
      return;
    }
    if (deps.noteHistoryActivity) deps.noteHistoryActivity();
    const msgs = (h.messages ?? []).slice();
    // Sort ascending by messageTimestamp so the ring keeps newest after eviction.
    msgs.sort((a, b) => Number(a.messageTimestamp ?? 0) - Number(b.messageTimestamp ?? 0));
    const self = deps.cfg.selfE164Getter();
    for (const m of msgs) {
      try {
        await handleOne(m, deps, self, "history");
      } catch (e) {
        logger.error({ err: String(e) }, "history-set handler error");
      }
    }
  };
}

async function handleOne(
  m: proto.IWebMessageInfo,
  deps: InboundDeps,
  self: string | null,
  source: IngestSource,
): Promise<void> {
  const remoteJid = m.key?.remoteJid ?? "";
  if (!remoteJid) return;
  const fromMe = Boolean(m.key?.fromMe);

  const text =
    m.message?.conversation ??
    m.message?.extendedTextMessage?.text ??
    "";

  // Age + timestamp filter for non-live sources.
  if (source !== "notify") {
    const tsSec = Number(m.messageTimestamp ?? 0);
    if (!tsSec || tsSec <= 0) {
      deps.counters.skipped_no_ts += 1;
      logger.info({ event: "history_append_skip_no_ts", source }, "skip");
      return;
    }
    if (!shouldIngestAppend(tsSec, Date.now(), deps.historyMaxAgeMs)) {
      deps.counters.skipped_stale += 1;
      logger.info({ event: "history_append_skip_stale", source }, "skip");
      return;
    }
  }

  // Body intentionally not logged — JID/type/length only.
  logger.info(
    {
      event: "upsert",
      source,
      jid_type: isGroupJid(remoteJid) ? "group" : "dm",
      from_me: fromMe,
      len: text.length,
    },
    "inbound",
  );

  if (!text) return;

  const messageId = m.key?.id ?? "";
  const messageKey = messageId ? `${remoteJid}:${messageId}` : "";
  if (source !== "notify" && messageKey && deps.inbox.hasMessageKey(messageKey)) {
    deps.counters.skipped_dup += 1;
    logger.info({ event: "history_append_skip_dup", source }, "dedupe");
    return;
  }

  const senderJid = isGroupJid(remoteJid) ? m.key?.participant ?? remoteJid : remoteJid;
  const senderE164 = jidToPhone(senderJid) ?? null;

  const channel_id = isGroupJid(remoteJid)
    ? self
      ? conversationIdForGroup(self, remoteJid)
      : remoteJid
    : self && senderE164
      ? conversationIdForDm(self, senderE164)
      : remoteJid;

  deps.inbox.append({
    provider: "whatsapp",
    workspace_id: self,
    user_id: fromMe ? self : senderE164,
    channel_id,
    thread_id: null,
    text,
    from_me: fromMe,
    ts: Number(m.messageTimestamp) ? Number(m.messageTimestamp) * 1000 : Date.now(),
    message_key: messageKey || null,
  });
  deps.counters.ingested_total += 1;
  deps.counters.ingested_by_source[source] += 1;

  if (source !== "notify") return; // never echo from history/append
  if (!deps.cfg.echo) return;
  if (fromMe) return; // primary fromMe guard
  if (deps.recentlySent.matches(remoteJid, text)) {
    logger.info({ event: "echo_skip", reason: "recently_sent" }, "skip echo");
    return;
  }
  if (!senderE164 || !deps.cfg.allowedTo.has(senderE164)) {
    logger.info({ event: "echo_skip", reason: "not_allowlisted" }, "skip echo");
    return;
  }

  const reply = `POC: ${text}`;
  const sock = deps.sock();
  if (!sock) return;
  try {
    deps.recentlySent.add(remoteJid, reply);
    await sock.sendMessage(remoteJid, { text: reply });
    logger.info({ event: "echo_sent" }, "echoed");
  } catch (e) {
    logger.error({ err: String(e) }, "echo send failed");
  }
}
