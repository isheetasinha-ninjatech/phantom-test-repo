// wa-bind-flow.ts — post-link binding state machine.
//
// Owns the 0.5s grace timer, creating_group execution, last_error capture,
// retry_bind, and pairing-code issuance (consolidated here so server.ts /
// wa-session.ts never call pairing.issue() directly).

import pino from "pino";
import type { WASocket } from "baileys";
import type { BindStore, BindSource } from "./wa-bind.js";
import type { BindMethodConfig, BindMethod } from "./wa-bind-method.js";
import type { PairingCode } from "./wa-pairing.js";
import type { EventCounters, RecentlySent } from "./wa-inbound.js";
import { createSelfGroup } from "./wa-auto-group.js";

const logger = pino({ level: process.env.WHATSAPP_LOG_LEVEL ?? "info" }).child({ mod: "wa-bind-flow" });

export type FlowPhase =
  | "idle"
  | "awaiting_bind_method"
  | "creating_group"
  | "auto_group_failed";

const GRACE_MS = 500;

export interface BindFlowDeps {
  sock: () => WASocket | null;
  selfE164: () => string | null;
  bindStore: BindStore;
  bindMethod: BindMethodConfig;
  pairing: PairingCode;
  counters: EventCounters;
  subject: () => string;
  recentlySent: RecentlySent;
}

export class BindFlow {
  private deps: BindFlowDeps;
  private phase: FlowPhase = "idle";
  private lastActiveMethod: BindMethod | null = null;
  private lastError: string | null = null;
  private timer: NodeJS.Timeout | null = null;
  private timerFiresAt = 0;
  // True after /unbind from an api/install bind — suppresses auto-arm
  // until operator picks via POST /bind_method.
  private requireExplicitChoice = false;

  constructor(deps: BindFlowDeps) {
    this.deps = deps;
  }

  getPhase(): FlowPhase {
    return this.phase;
  }

  getLastActiveMethod(): BindMethod | null {
    return this.lastActiveMethod;
  }

  getLastError(): string | null {
    return this.lastError;
  }

  graceRemainingMs(): number {
    if (!this.timer) return 0;
    return Math.max(0, this.timerFiresAt - Date.now());
  }

  /** Called by wa-session on connection=open. No-op if already bound. */
  onOpen(): void {
    if (this.deps.bindStore.getChatJid()) {
      this.phase = "idle";
      return;
    }
    this.enterAwaiting();
  }

  /** Called by wa-session on connection=close/reconnect. */
  onClose(): void {
    this.cancelTimer();
  }

  /** Called after BindStore.set succeeds (pairing-code, /bind, install seed). */
  onBound(via: BindSource): void {
    this.cancelTimer();
    if (via === "auto_group" || via === "pairing_code") {
      this.lastActiveMethod = via;
    }
    this.phase = "idle";
    this.lastError = null;
    this.deps.pairing.clear();
  }

  /**
   * Called by /unbind after BindStore.clear. Never auto-rebinds — operator
   * must click "Bind" (POST /bind_now). lastActiveMethod is retained only
   * for display ("last picked X").
   */
  onUnbound(prevVia: BindSource | null): void {
    if (prevVia === "auto_group" || prevVia === "pairing_code") {
      this.lastActiveMethod = prevVia;
    } else {
      this.lastActiveMethod = null;
    }
    this.requireExplicitChoice = true;
    this.lastError = null;
    this.deps.pairing.clear();
    this.enterAwaiting();
  }

  /** POST /bind_method. Caller handles 409. */
  setOverride(method: string): BindMethod {
    const active = this.deps.bindMethod.setOverride(method);
    this.deps.counters.bind_method_override_set += 1;
    // Firing is explicit via POST /bind_now; cold-boot timer (if armed) keeps running.
    return active;
  }

  /** Called by /unlink — resets to cold-boot so next open auto-arms. */
  reset(): void {
    this.cancelTimer();
    this.phase = "idle";
    this.lastActiveMethod = null;
    this.lastError = null;
    this.requireExplicitChoice = false;
    this.deps.pairing.clear();
  }

  /** POST /bind_now — fire the currently-resolved method immediately. */
  async triggerNow(): Promise<void> {
    if (this.deps.bindStore.getChatJid()) {
      throw new Error("already_bound");
    }
    if (this.phase === "creating_group") {
      throw new Error("bind_in_progress");
    }
    this.cancelTimer();
    this.requireExplicitChoice = false;
    this.lastError = null;
    await this.executeMethod();
  }

  /** POST /retry_bind from auto_group_failed. Caller validates phase. */
  async retry(): Promise<void> {
    this.deps.counters.retry_bind_requested += 1;
    this.lastError = null;
    const method = this.deps.bindMethod.get();
    if (method === "auto_group") {
      await this.runAutoGroup();
    } else {
      // pairing_code path
      this.phase = "idle";
      this.deps.pairing.issue();
    }
  }

  private enterAwaiting(): void {
    this.phase = "awaiting_bind_method";
    if (this.requireExplicitChoice) {
      this.cancelTimer();
      logger.info({ event: "awaiting_bind_method_no_timer" }, "waiting for operator");
      return;
    }
    this.armTimer();
  }

  private armTimer(): void {
    this.cancelTimer();
    this.timerFiresAt = Date.now() + GRACE_MS;
    this.timer = setTimeout(() => {
      this.timer = null;
      this.timerFiresAt = 0;
      void this.executeMethod();
    }, GRACE_MS);
    logger.info(
      { event: "bind_method_armed", method: this.deps.bindMethod.get(), grace_ms: GRACE_MS },
      "armed bind method",
    );
  }

  private cancelTimer(): void {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
      this.timerFiresAt = 0;
    }
  }

  private async executeMethod(): Promise<void> {
    const method = this.deps.bindMethod.get();
    if (method === "auto_group") {
      await this.runAutoGroup();
    } else {
      // pairing_code: leave bound.json alone and issue a code.
      this.phase = "idle";
      this.deps.pairing.issue();
    }
  }

  private async runAutoGroup(): Promise<void> {
    this.phase = "creating_group";
    this.lastError = null;
    const sock = this.deps.sock();
    const selfE164 = this.deps.selfE164();
    if (!sock || !selfE164) {
      this.phase = "auto_group_failed";
      this.lastError = "socket_or_self_not_ready";
      this.deps.counters.auto_group_create_err += 1;
      logger.warn({ event: "auto_group_failed", err: this.lastError }, "auto-group preflight failed");
      return;
    }
    const selfJid = `${selfE164}@s.whatsapp.net`;
    const subject = this.deps.subject();
    try {
      const { group_jid } = await createSelfGroup(sock, selfJid, subject);
      await this.deps.bindStore.set(group_jid, "auto_group");
      this.lastActiveMethod = "auto_group";
      this.deps.counters.auto_group_create_ok += 1;
      this.phase = "idle";
      logger.info({ event: "auto_group_bound", group_jid }, "auto-group bound");
      // Confirmation message into the newly bound chat. Register in
      // recentlySent so the inbound loopback filter drops the echo —
      // otherwise the monitor would dispatch our own confirmation to Claude.
      try {
        const confirmation = "🥷 Ninja: bound. From now on I only listen here.";
        this.deps.recentlySent.add(group_jid, confirmation);
        await sock.sendMessage(group_jid, { text: confirmation });
      } catch (e) {
        logger.warn({ err: String(e) }, "auto-group confirmation send failed");
      }
    } catch (e) {
      this.lastError = String(e);
      this.phase = "auto_group_failed";
      this.deps.counters.auto_group_create_err += 1;
      logger.warn({ event: "auto_group_failed", err: this.lastError }, "auto-group create failed");
    }
  }
}
