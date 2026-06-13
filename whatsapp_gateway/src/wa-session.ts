// wa-session.ts — Baileys socket lifecycle: connect, QR, reconnect, logout.
// Reference behavior (not source): openclaw/extensions/whatsapp/src/connection-controller.ts

import { rm } from "node:fs/promises";
import path from "node:path";
import pino from "pino";
import {
  makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  type WASocket,
} from "baileys";

type BoomLike = { output?: { statusCode?: number } };

export type ConnState = "starting" | "qr" | "connecting" | "open" | "logged_out" | "closed";

export interface SessionEvents {
  onUpsert?: (m: unknown) => void;
  onHistorySet?: (h: unknown) => void;
}

export interface SessionOptions {
  /** Pass `syncFullHistory: true` to Baileys. Default false (personal-phone safe). */
  syncFullHistory?: boolean;
  /**
   * Debounce window for flipping history_sync_active=false after the last
   * append/history activity. Default 5s.
   */
  historySyncQuietMs?: number;
}

const logger = pino({ level: process.env.WHATSAPP_LOG_LEVEL ?? "warn" }).child({ mod: "wa-session" });

const TRANSIENT_CODES = new Set<number>([
  DisconnectReason.connectionClosed,
  DisconnectReason.connectionLost,
  DisconnectReason.restartRequired,
  DisconnectReason.timedOut,
  DisconnectReason.connectionReplaced,
  // 503 service unavailable surfaces via Boom.output.statusCode
  503,
]);

export class WaSession {
  private sock: WASocket | null = null;
  private state: ConnState = "starting";
  private latestQr: string | null = null;
  private selfE164: string | null = null;
  private authDir: string;
  private events: SessionEvents;
  private shuttingDown = false;
  private reconnectTimer: NodeJS.Timeout | null = null;
  private reconnectAttempt = 0;

  // History sync state.
  private syncFullHistory: boolean;
  private historySyncQuietMs: number;
  private inboxEpoch = Date.now();
  private connectedAtMs = 0;
  private historySyncActive = false;
  private historyQuietTimer: NodeJS.Timeout | null = null;

  constructor(authDir: string, options: SessionOptions = {}, events: SessionEvents = {}) {
    this.authDir = path.resolve(authDir);
    this.events = events;
    this.syncFullHistory = Boolean(options.syncFullHistory);
    this.historySyncQuietMs = options.historySyncQuietMs ?? 5000;
  }

  setEvents(events: SessionEvents): void {
    this.events = events;
  }

  getState(): ConnState {
    return this.state;
  }

  getSelfE164(): string | null {
    return this.selfE164;
  }

  getLatestQr(): string | null {
    return this.latestQr;
  }

  getSocket(): WASocket | null {
    return this.sock;
  }

  getInboxEpoch(): number {
    return this.inboxEpoch;
  }

  getHistorySyncActive(): boolean {
    return this.historySyncActive;
  }

  getConnectedAtMs(): number {
    return this.connectedAtMs;
  }

  isSyncFullHistory(): boolean {
    return this.syncFullHistory;
  }

  /**
   * Called by the inbound handler when an append upsert or history-set batch
   * arrives. Re-arms the quiet-window debounce so /status correctly reports
   * history_sync_active=true while we're still receiving history.
   */
  noteHistoryActivity(): void {
    if (!this.syncFullHistory) return;
    this.historySyncActive = true;
    this.armHistoryQuietTimer();
  }

  private armHistoryQuietTimer(): void {
    if (this.historyQuietTimer) clearTimeout(this.historyQuietTimer);
    this.historyQuietTimer = setTimeout(() => {
      this.historySyncActive = false;
      this.historyQuietTimer = null;
      logger.info({ event: "history_sync_idle" }, "history sync quiet");
    }, this.historySyncQuietMs);
  }

  async start(): Promise<void> {
    await this.connect();
  }

  async close(): Promise<void> {
    this.shuttingDown = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.historyQuietTimer) {
      clearTimeout(this.historyQuietTimer);
      this.historyQuietTimer = null;
    }
    try {
      this.sock?.end(undefined);
    } catch {
      // ignore
    }
    this.state = "closed";
  }

  private async connect(): Promise<void> {
    const { state, saveCreds } = await useMultiFileAuthState(this.authDir);
    const { version } = await fetchLatestBaileysVersion().catch(() => ({ version: undefined as any }));

    this.state = "connecting";
    this.sock = makeWASocket({
      auth: state,
      logger: logger as any,
      printQRInTerminal: false,
      version,
      browser: ["Phantom POC", "Chrome", "1.0"],
      syncFullHistory: this.syncFullHistory,
      markOnlineOnConnect: false,
    });

    this.sock.ev.on("creds.update", saveCreds);

    this.sock.ev.on("connection.update", (u) => {
      const { connection, lastDisconnect, qr } = u;
      if (qr) {
        this.latestQr = qr;
        this.state = "qr";
        logger.info({ event: "qr" }, "QR available");
      }
      if (connection === "open") {
        this.latestQr = null;
        this.state = "open";
        this.reconnectAttempt = 0;
        const id = this.sock?.user?.id ?? null;
        this.selfE164 = parseSelfE164(id);
        // Bump epoch on every successful connect so CLI cursors invalidate
        // when the in-memory inbox starts fresh.
        this.inboxEpoch = Date.now();
        this.connectedAtMs = this.inboxEpoch;
        if (this.syncFullHistory) {
          this.historySyncActive = true;
          this.armHistoryQuietTimer();
        } else {
          this.historySyncActive = false;
        }
        logger.info(
          {
            event: "open",
            selfE164: this.selfE164,
            inbox_epoch: this.inboxEpoch,
            sync_full_history: this.syncFullHistory,
          },
          "linked",
        );
      } else if (connection === "close") {
        const err = (lastDisconnect?.error as BoomLike | undefined)?.output?.statusCode;
        logger.warn({ event: "close", code: err }, "disconnected");
        if (err === DisconnectReason.loggedOut) {
          this.state = "logged_out";
          this.latestQr = null;
          this.selfE164 = null;
          void this.handleLoggedOut();
          return;
        }
        if (!this.shuttingDown && (err === undefined || TRANSIENT_CODES.has(err))) {
          this.scheduleReconnect();
        } else if (!this.shuttingDown) {
          // Unknown non-transient code: still try once after a longer delay.
          this.scheduleReconnect(true);
        }
      }
    });

    if (this.events.onUpsert) {
      this.sock.ev.on("messages.upsert", this.events.onUpsert);
    }
    if (this.events.onHistorySet) {
      this.sock.ev.on("messaging-history.set", this.events.onHistorySet as any);
    }
  }

  private async handleLoggedOut(): Promise<void> {
    try {
      await rm(this.authDir, { recursive: true, force: true });
      logger.warn({ authDir: this.authDir }, "auth dir cleared after logout; relink required");
    } catch (e) {
      logger.error({ err: String(e) }, "failed to clear auth dir");
    }
  }

  private scheduleReconnect(longDelay = false): void {
    if (this.reconnectTimer) return;
    this.reconnectAttempt += 1;
    const base = longDelay ? 5000 : 1000;
    const delay = Math.min(30_000, base * Math.pow(2, Math.min(this.reconnectAttempt - 1, 5)));
    logger.info({ attempt: this.reconnectAttempt, delay }, "scheduling reconnect");
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect().catch((e) => {
        logger.error({ err: String(e) }, "reconnect failed");
        if (!this.shuttingDown) this.scheduleReconnect(true);
      });
    }, delay);
  }
}

function parseSelfE164(id: string | null): string | null {
  if (!id) return null;
  // Baileys user.id like "15551234567:42@s.whatsapp.net" or "15551234567@s.whatsapp.net"
  const head = id.split("@")[0] ?? "";
  const digits = head.split(":")[0] ?? "";
  return /^\d+$/.test(digits) ? digits : null;
}
