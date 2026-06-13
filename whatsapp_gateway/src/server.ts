// server.ts — Fastify HTTP gateway. Bearer auth, 127.0.0.1 bind by default.

import Fastify, { type FastifyReply, type FastifyRequest } from "fastify";
import QRCode from "qrcode";
import pino from "pino";
import { WaSession } from "./wa-session.js";
import {
  Inbox,
  RecentlySent,
  makeUpsertHandler,
  makeHistorySetHandler,
  makeEventCounters,
} from "./wa-inbound.js";
import { sendText, sendTextToJid, groupCreate } from "./wa-send.js";
import { listParticipatingGroups } from "./wa-groups.js";
import { conversationIdForDm, conversationIdForGroup, normalizeGroupJid } from "./normalize-jid.js";

const logger = pino({ level: process.env.WHATSAPP_LOG_LEVEL ?? "info" }).child({ mod: "server" });

function envBool(name: string, def: boolean): boolean {
  const v = process.env[name];
  if (v === undefined) return def;
  return v === "1" || v.toLowerCase() === "true";
}

function envCSV(name: string): string[] {
  const v = process.env[name];
  if (!v) return [];
  return v.split(",").map((s) => s.trim()).filter(Boolean);
}

async function main() {
  const bind = process.env.WHATSAPP_BIND ?? "127.0.0.1";
  const port = Number(process.env.PORT ?? 8090);
  const token = process.env.WHATSAPP_GATEWAY_TOKEN ?? "";
  const authDir = process.env.WHATSAPP_AUTH_DIR ?? "./auth/default";
  const echo = envBool("WHATSAPP_ECHO", false);
  const allowGroupCreate = envBool("WHATSAPP_ALLOW_GROUP_CREATE", false);
  const allowedTo = new Set(envCSV("WHATSAPP_ALLOWED_TO").map((s) => s.replace(/\D/g, "")));
  const forceSingleTo = envBool("WHATSAPP_FORCE_SINGLE_TO", true);
  const defaultTo = (process.env.WHATSAPP_TO ?? "").replace(/\D/g, "");
  const syncFullHistory = envBool("WHATSAPP_SYNC_FULL_HISTORY", false);
  const historyMaxAgeMs = Math.max(
    0,
    Number(process.env.WHATSAPP_HISTORY_SYNC_MAX_AGE_MS ?? 604_800_000) || 604_800_000,
  );

  // Safety: if not localhost-only, require a token.
  if (bind !== "127.0.0.1" && bind !== "localhost" && !token) {
    throw new Error("WHATSAPP_GATEWAY_TOKEN required when binding to non-localhost");
  }

  if (echo && allowedTo.size === 0) {
    throw new Error("WHATSAPP_ECHO=1 requires WHATSAPP_ALLOWED_TO to be set (never empty)");
  }

  const inbox = new Inbox();
  const recentlySent = new RecentlySent();
  const counters = makeEventCounters();
  const session = new WaSession(authDir, { syncFullHistory });

  const inboundDeps = {
    sock: () => session.getSocket(),
    inbox,
    recentlySent,
    cfg: {
      echo,
      allowedTo,
      selfE164Getter: () => session.getSelfE164(),
    },
    historyMaxAgeMs,
    noteHistoryActivity: () => session.noteHistoryActivity(),
    counters,
  };
  const upsertHandler = makeUpsertHandler(inboundDeps);
  const historySetHandler = makeHistorySetHandler(inboundDeps);
  session.setEvents({
    onUpsert: upsertHandler as unknown as (m: unknown) => void,
    onHistorySet: historySetHandler as unknown as (h: unknown) => void,
  });
  await session.start();

  const app = Fastify({ logger: false });

  function requireAuth(req: FastifyRequest, reply: FastifyReply): boolean {
    if (!token) return true; // localhost-only mode without token
    const h = req.headers.authorization ?? "";
    if (h === `Bearer ${token}`) return true;
    reply.code(401).send({ error: "unauthorized" });
    return false;
  }

  app.get("/health", async () => ({ ok: true, state: session.getState() }));

  app.get("/status", async (req, reply) => {
    if (!requireAuth(req, reply)) return;
    return {
      connection: session.getState(),
      linked: session.getState() === "open",
      self_e164: session.getSelfE164(),
      inbox_epoch: session.getInboxEpoch(),
      history_sync_active: session.getHistorySyncActive(),
      sync_full_history: session.isSyncFullHistory(),
      events_seen: counters,
    };
  });

  app.get("/qr", async (req, reply) => {
    if (!requireAuth(req, reply)) return;
    reply.header("Cache-Control", "no-store");
    const qr = session.getLatestQr();
    if (!qr) {
      reply.code(404).send({ error: "no_qr", state: session.getState() });
      return;
    }
    const fmt = (req.query as any)?.format ?? "png";
    if (fmt === "text") {
      reply.header("Content-Type", "text/plain; charset=utf-8");
      return qr;
    }
    const buf = await QRCode.toBuffer(qr, { type: "png", margin: 1, scale: 6 });
    reply.header("Content-Type", "image/png");
    return buf;
  });

  app.post("/send", async (req, reply) => {
    if (!requireAuth(req, reply)) return;
    if (session.getState() !== "open") {
      reply.code(409).send({ error: "not_linked", state: session.getState() });
      return;
    }
    const body = (req.body ?? {}) as { to?: string; group_jid?: string; text?: string };
    const text = (body.text ?? "").toString();
    if (!text) {
      reply.code(400).send({ error: "text required" });
      return;
    }
    const rawGroupJid = (body.group_jid ?? "").toString().trim();
    const rawTo = (body.to ?? "").toString().trim();
    if (rawGroupJid && rawTo) {
      reply.code(400).send({ error: "provide either to or group_jid, not both" });
      return;
    }
    if (rawGroupJid) {
      let groupJid: string;
      try {
        groupJid = normalizeGroupJid(rawGroupJid);
      } catch (e) {
        reply.code(400).send({ error: "invalid group_jid", detail: String(e) });
        return;
      }
      try {
        const res = await sendTextToJid(
          { sock: () => session.getSocket(), recentlySent },
          groupJid,
          text,
        );
        const self = session.getSelfE164();
        const conversation_id = self ? conversationIdForGroup(self, groupJid) : null;
        return { ok: true, group_jid: groupJid, jid: res.jid, id: res.id, conversation_id };
      } catch (e) {
        reply.code(500).send({ error: "send_failed", detail: String(e) });
      }
      return;
    }
    let to = rawTo.replace(/\D/g, "");
    if (forceSingleTo) {
      if (!defaultTo) {
        reply.code(400).send({ error: "WHATSAPP_FORCE_SINGLE_TO=1 but WHATSAPP_TO is not set" });
        return;
      }
      to = defaultTo;
    }
    if (!to) {
      reply.code(400).send({ error: "to or group_jid required" });
      return;
    }
    if (allowedTo.size > 0 && !allowedTo.has(to)) {
      reply.code(403).send({ error: "destination not in WHATSAPP_ALLOWED_TO" });
      return;
    }
    try {
      const res = await sendText({ sock: () => session.getSocket(), recentlySent }, to, text);
      const self = session.getSelfE164();
      const conversation_id = self ? conversationIdForDm(self, to) : null;
      return { ok: true, to, jid: res.jid, id: res.id, conversation_id };
    } catch (e) {
      reply.code(500).send({ error: "send_failed", detail: String(e) });
    }
  });

  app.get("/groups", async (req, reply) => {
    if (!requireAuth(req, reply)) return;
    if (session.getState() !== "open") {
      reply.code(409).send({ error: "not_linked", state: session.getState() });
      return;
    }
    try {
      const groups = await listParticipatingGroups(session.getSocket(), session.getSelfE164());
      return { ok: true, groups };
    } catch (e) {
      reply.code(500).send({ error: "group_list_failed", detail: String(e) });
    }
  });

  app.post("/groups/create", async (req, reply) => {
    if (!requireAuth(req, reply)) return;
    if (!allowGroupCreate) {
      reply.code(403).send({ error: "group_create_disabled", hint: "set WHATSAPP_ALLOW_GROUP_CREATE=1" });
      return;
    }
    if (session.getState() !== "open") {
      reply.code(409).send({ error: "not_linked", state: session.getState() });
      return;
    }
    const body = (req.body ?? {}) as { subject?: string; participants?: string[]; welcome?: string };
    const subject = (body.subject ?? "").toString().trim();
    const participants = Array.isArray(body.participants)
      ? body.participants.map((p) => String(p).replace(/\D/g, "")).filter(Boolean)
      : [];
    if (!subject) {
      reply.code(400).send({ error: "subject required" });
      return;
    }
    if (participants.length === 0) {
      reply.code(400).send({ error: "participants required" });
      return;
    }
    try {
      const res = await groupCreate(
        { sock: () => session.getSocket(), recentlySent },
        subject,
        participants,
        body.welcome,
      );
      const self = session.getSelfE164();
      const conversation_id = self ? conversationIdForGroup(self, res.group_jid) : null;
      return { ok: true, ...res, conversation_id };
    } catch (e) {
      reply.code(500).send({ error: "group_create_failed", detail: String(e) });
    }
  });

  app.get("/messages", async (req, reply) => {
    if (!requireAuth(req, reply)) return;
    const q = (req.query ?? {}) as { since?: string; limit?: string };
    const since = Number(q.since ?? 0) || 0;
    const limit = Math.min(500, Math.max(1, Number(q.limit ?? 100) || 100));
    const items = inbox.since(since, limit);
    return {
      items,
      latest_seq: inbox.latestSeq(),
      inbox_epoch: session.getInboxEpoch(),
    };
  });

  const addr = await app.listen({ host: bind, port });
  logger.info(
    {
      addr,
      bind,
      port,
      echo,
      allow_group_create: allowGroupCreate,
      sync_full_history: syncFullHistory,
      history_max_age_ms: historyMaxAgeMs,
    },
    "gateway listening",
  );

  const shutdown = async (sig: string) => {
    logger.info({ sig }, "shutting down");
    try {
      await app.close();
    } catch {}
    await session.close();
    process.exit(0);
  };
  process.on("SIGINT", () => void shutdown("SIGINT"));
  process.on("SIGTERM", () => void shutdown("SIGTERM"));
}

main().catch((e) => {
  // eslint-disable-next-line no-console
  console.error("fatal:", e);
  process.exit(1);
});
