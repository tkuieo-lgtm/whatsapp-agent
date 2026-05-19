require("dotenv").config({ path: "../.env" });

const {
    default: makeWASocket,
    DisconnectReason,
    useMultiFileAuthState,
    fetchLatestBaileysVersion,
    downloadMediaMessage,
    Browsers,
} = require("@whiskeysockets/baileys");
const { Boom } = require("@hapi/boom");
const express = require("express");
const axios   = require("axios");
const fs      = require("fs");
const path    = require("path");
const { Pool } = require("pg");
const QRCode  = require("qrcode");

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------
const OWNER_PHONE = (process.env.OWNER_PHONE || "").replace(/\D/g, "");
const PROXY_URL   = process.env.PROXY_URL || "";
const BOT_NAME    = process.env.BOT_NAME || "מקס";
const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";
const DATABASE_URL = process.env.DATABASE_URL;
const PORT        = process.env.PORT || 3000;
const SESSION_DIR = "./.baileys_auth";

if (!OWNER_PHONE) {
    console.error("[ERROR] OWNER_PHONE is not set in .env");
    process.exit(1);
}
console.log(`[SERVER] Starting on port: ${PORT}`);
console.log(`[CONFIG] Owner: ${OWNER_PHONE} | Bot: ${BOT_NAME}`);

// ---------------------------------------------------------------------------
// Crash protection
// ---------------------------------------------------------------------------
process.on("uncaughtException", (err) => {
    console.error("[CRASH] uncaughtException:", err.message, err.stack);
});
process.on("unhandledRejection", (reason) => {
    console.error("[CRASH] unhandledRejection:", reason);
});

// ---------------------------------------------------------------------------
// PostgreSQL session persistence (same pattern as whatsapp-web.js)
// ---------------------------------------------------------------------------
let pgPool = null;

function getPool() {
    if (!DATABASE_URL) return null;
    if (!pgPool) {
        pgPool = new Pool({ connectionString: DATABASE_URL, ssl: { rejectUnauthorized: false } });
    }
    return pgPool;
}

async function ensureSessionTable(pool) {
    await pool.query(`
        CREATE TABLE IF NOT EXISTS whatsapp_sessions (
            id VARCHAR(50) PRIMARY KEY,
            data TEXT NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )`
    );
}

async function restoreSessionFromDB() {
    const pool = getPool();
    if (!pool) { console.log("[SESSION] No DATABASE_URL — skipping restore"); return false; }
    try {
        await ensureSessionTable(pool);
        const { rows } = await pool.query("SELECT data FROM whatsapp_sessions WHERE id=$1", ["main"]);
        if (!rows.length) { console.log("[SESSION] No saved session — QR scan required"); return false; }
        const files = JSON.parse(rows[0].data);
        fs.mkdirSync(SESSION_DIR, { recursive: true });
        for (const [relPath, b64] of Object.entries(files)) {
            const full = path.join(SESSION_DIR, relPath);
            fs.mkdirSync(path.dirname(full), { recursive: true });
            fs.writeFileSync(full, Buffer.from(b64, "base64"));
        }
        console.log(`[SESSION] Restored ${Object.keys(files).length} files from PostgreSQL`);
        return true;
    } catch (e) {
        console.error("[SESSION] Restore error:", e.message);
        return false;
    }
}

async function saveSessionToDB() {
    const pool = getPool();
    if (!pool) return;
    try {
        if (!fs.existsSync(SESSION_DIR)) return;
        const files = {};
        function readDir(dir) {
            for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
                const full = path.join(dir, entry.name);
                if (entry.isDirectory()) readDir(full);
                else files[path.relative(SESSION_DIR, full)] = fs.readFileSync(full).toString("base64");
            }
        }
        readDir(SESSION_DIR);
        if (Object.keys(files).length === 0) return;
        await ensureSessionTable(pool);
        await pool.query(
            `INSERT INTO whatsapp_sessions (id,data,updated_at) VALUES ('main',$1,NOW())
             ON CONFLICT (id) DO UPDATE SET data=$1, updated_at=NOW()`,
            [JSON.stringify(files)]
        );
        console.log(`[SESSION] Saved ${Object.keys(files).length} files to PostgreSQL`);
    } catch (e) {
        console.error("[SESSION] Save error:", e.message);
    }
}

// ---------------------------------------------------------------------------
// Phone helpers
// ---------------------------------------------------------------------------
const last9 = (n) => String(n).replace(/\D/g, "").slice(-9);

function isOwner(jid) {
    const phone = jid.replace(/@[^@]+$/, "").replace(/\D/g, "");
    return last9(phone) === last9(OWNER_PHONE);
}

function jidToPhone(jid) {
    return jid.replace(/@[^@]+$/, "").replace(/\D/g, "");
}

function normalizeJid(jid) {
    if (jid.endsWith("@c.us")) return jid.replace("@c.us", "@s.whatsapp.net");  // normalize legacy format
    if (jid.endsWith("@g.us")) return jid;  // groups unchanged
    if (jid.includes("@")) return jid;
    return `${jid.replace(/\D/g, "")}@s.whatsapp.net`;
}

// ---------------------------------------------------------------------------
// @lid resolver — WhatsApp multi-device sends @lid instead of phone JIDs.
// lidMap is built from contacts.upsert + explicit sock.onWhatsApp() lookup.
// Mapping is persisted in DB so it survives restarts.
// ---------------------------------------------------------------------------
const lidMap = new Map();   // "39183020240999@lid" → "972546670073@s.whatsapp.net"

function resolveJid(jid) {
    if (!jid || !jid.endsWith("@lid")) return jid;
    if (lidMap.has(jid)) return lidMap.get(jid);
    console.warn(`[LID] Cannot resolve ${jid} — not yet in contact map`);
    return jid;
}

async function _saveLidToDB(lid, phoneJid) {
    const pool = getPool();
    if (!pool) return;
    try {
        await ensureSessionTable(pool);
        await pool.query(
            `INSERT INTO whatsapp_sessions (id,data,updated_at) VALUES ('owner_lid',$1,NOW())
             ON CONFLICT (id) DO UPDATE SET data=$1, updated_at=NOW()`,
            [JSON.stringify({ lid, phoneJid })]
        );
    } catch (e) {
        console.error("[LID] DB save failed:", e.message);
    }
}

async function _loadLidFromDB() {
    const pool = getPool();
    if (!pool) return;
    try {
        await ensureSessionTable(pool);
        const { rows } = await pool.query(
            "SELECT data FROM whatsapp_sessions WHERE id='owner_lid'"
        );
        if (rows.length) {
            const { lid, phoneJid } = JSON.parse(rows[0].data);
            lidMap.set(lid, phoneJid);
            console.log(`[LID] Loaded from DB: ${lid} → ${phoneJid}`);
        }
    } catch (e) {
        console.error("[LID] DB load failed:", e.message);
    }
}

async function resolveOwnerLid() {
    if (!sock || !isConnected) return;
    try {
        const ownerJid = `${OWNER_PHONE}@s.whatsapp.net`;
        const results  = await sock.onWhatsApp(OWNER_PHONE);
        if (!results || !results.length) {
            console.warn("[LID] sock.onWhatsApp returned empty — owner not on WA?");
            return;
        }
        const info = results[0];
        console.log(`[LID] onWhatsApp result: exists=${info.exists} jid=${info.jid} lid=${info.lid}`);
        if (info.lid) {
            lidMap.set(info.lid, ownerJid);
            await _saveLidToDB(info.lid, ownerJid);
            console.log(`[LID] Owner LID mapped: ${info.lid} → ${ownerJid}`);
        }
    } catch (e) {
        console.warn(`[LID] resolveOwnerLid failed: ${e.message}`);
    }
}

// ---------------------------------------------------------------------------
// Bot JID helper — used for group mention detection
// ---------------------------------------------------------------------------
function getBotJid() {
    if (!sock || !sock.user) return null;
    // sock.user.id format: "972529439686:12@s.whatsapp.net"
    const phone = sock.user.id.split(":")[0];
    return `${phone}@s.whatsapp.net`;
}

// ---------------------------------------------------------------------------
// Session save — debounced to prevent DB flooding on creds.update storms
// ---------------------------------------------------------------------------
let _saveTimer   = null;
let _failedState = false;   // set when max retries reached

function scheduleSave() {
    if (_saveTimer) return;   // already pending — skip
    _saveTimer = setTimeout(async () => {
        _saveTimer = null;
        await saveSessionToDB();
    }, 30_000);   // 30 s debounce
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let latestQR       = null;
let sock           = null;
let isConnected    = false;
let connectionState = "disconnected";
let deviceReady    = false;
let lastOwnerJid   = null;
let keepAliveInterval     = null;
let _sessionBackupInterval = null;   // module-level — only one interval ever
let reconnectAttempts     = 0;
const MAX_RECONNECT       = 10;
const BACKOFF_MS          = [30_000, 60_000, 120_000];
const MAX_BACKOFF         = 5 * 60_000;
// 401 rate-limit guard: track timestamps of recent loggedOut events
const _401timestamps      = [];
let   _warmingUp          = false;   // true for 60s after connection=open
const seen                = new Set();

function markSeen(id) {
    seen.add(id);
    setTimeout(() => seen.delete(id), 30_000);
}

const silentLogger = {
    level: "warn",
    trace: () => {}, debug: () => {}, info: () => {},
    warn:  (obj, msg) => console.warn("[BAILEYS]", msg ?? obj),
    error: (obj, msg) => console.error("[BAILEYS]", msg ?? obj),
    fatal: (obj, msg) => console.error("[BAILEYS]", msg ?? obj),
    child: () => silentLogger,
};

// ---------------------------------------------------------------------------
// WhatsApp connection
// ---------------------------------------------------------------------------
async function connectToWhatsApp() {
    fs.mkdirSync(SESSION_DIR, { recursive: true });

    const { version } = await fetchLatestBaileysVersion();
    console.log(`[WHATSAPP] Using Baileys version ${version.join(".")}`);

    const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR);

    // Build proxy agent if PROXY_URL is set (socks5:// or http://)
    let proxyAgent;
    if (PROXY_URL) {
        const url = new URL(PROXY_URL);
        if (url.protocol.startsWith("socks")) {
            const { SocksProxyAgent } = await import("socks-proxy-agent");
            proxyAgent = new SocksProxyAgent(PROXY_URL);
        } else {
            const { HttpsProxyAgent } = await import("https-proxy-agent");
            proxyAgent = new HttpsProxyAgent(PROXY_URL);
        }
        console.log(`[PROXY] Using proxy: ${url.protocol}//${url.host}`);
    }

    sock = makeWASocket({
        version,
        auth: state,
        printQRInTerminal: false,
        logger: silentLogger,
        browser: Browsers.appropriate("Desktop"),
        syncFullHistory: false,
        connectTimeoutMs: 60_000,
        defaultQueryTimeoutMs: 60_000,
        keepAliveIntervalMs: 20_000,
        getMessage: async (_key) => ({ conversation: "" }),
        ...(proxyAgent ? { agent: proxyAgent } : {}),
    });
    console.log(`[WHATSAPP] makeWASocket created — version=${version.join(".")}`);


    // Build lid→phone map from contact sync (fires during connection init)
    sock.ev.on("contacts.upsert", (contacts) => {
        let mapped = 0;
        for (const c of contacts) {
            if (c.id && c.lid) {
                lidMap.set(c.lid, c.id);
                mapped++;
            }
        }
        console.log(`[LID] contacts.upsert: ${contacts.length} contacts, mapped ${mapped} — total ${lidMap.size}`);
    });

    // Persist credentials — ensure dir exists before writing (loggedOut deletes it)
    sock.ev.on("creds.update", async () => {
        fs.mkdirSync(SESSION_DIR, { recursive: true });
        await saveCreds();
        scheduleSave();
    });

    console.log(`[CONN] Socket created — waiting for connection.update events`);

    // Connection lifecycle
    sock.ev.on("connection.update", async ({ connection, lastDisconnect, qr, isNewLogin, receivedPendingNotifications }) => {
        if (connection) connectionState = connection;   // "connecting" | "open" | "close"
        const code = lastDisconnect ? new Boom(lastDisconnect?.error)?.output?.statusCode : null;
        console.log(`[CONN] connection=${connection ?? "-"} qr=${!!qr} code=${code ?? "-"} isNewLogin=${isNewLogin ?? "-"} pendingNotif=${receivedPendingNotifications ?? "-"} state=${connectionState} deviceReady=${deviceReady}`);
        if (lastDisconnect?.error) console.log(`[CONN] error: ${lastDisconnect.error.message ?? lastDisconnect.error}`);

        // receivedPendingNotifications=true means WhatsApp has finished provisioning
        // the new device on their servers — only now are outgoing messages reliably delivered
        if (receivedPendingNotifications === true && !deviceReady) {
            deviceReady = true;
            console.log("[CONN] Device provisioned — ready to send (receivedPendingNotifications=true)");
        }

        if (qr) {
            latestQR = qr;
            console.log("[QR] New QR received — open /qr in browser to scan");
        }

        if (connection === "open") {
            latestQR      = null;
            isConnected   = true;
            reconnectAttempts = 0;
            _failedState  = false;
            console.log(`[WHATSAPP] ${BOT_NAME} connected! isNewLogin=${isNewLogin}`);
            await saveSessionToDB();

            // On reconnects (existing session), receivedPendingNotifications never fires.
            // Only fresh pairings (isNewLogin=true) need to wait for it.
            if (!isNewLogin) {
                deviceReady = true;
                console.log("[CONN] Existing session reconnected — deviceReady=true immediately");
            }

            // Warmup: defer message processing for 60s after connect to let
            // WhatsApp finish device provisioning without triggering rate limits
            _warmingUp = true;
            console.log("[WARMUP] Waiting 60s before processing messages");
            setTimeout(() => {
                _warmingUp = false;
                console.log("[WARMUP] Done — now processing incoming messages");
            }, 60_000);

            // Manual keepalive: send presence update every 20 s
            if (keepAliveInterval) clearInterval(keepAliveInterval);
            keepAliveInterval = setInterval(async () => {
                if (!isConnected || !sock) return;
                try { await sock.sendPresenceUpdate("available"); }
                catch (_) { /* ignore — WS ping failures are non-fatal */ }
            }, 20_000);
        }

        if (connection === "close") {
            isConnected  = false;
            deviceReady  = false;
            _warmingUp   = false;
            if (keepAliveInterval) { clearInterval(keepAliveInterval); keepAliveInterval = null; }

            const code = new Boom(lastDisconnect?.error)?.output?.statusCode;
            console.log(`[WHATSAPP] Disconnected — code: ${code}`);

            if (code === DisconnectReason.loggedOut) {
                const now = Date.now();
                _401timestamps.push(now);
                // Keep only events in the last 60 seconds
                while (_401timestamps.length && now - _401timestamps[0] > 60_000) _401timestamps.shift();

                if (_401timestamps.length >= 2) {
                    _failedState = true;
                    console.error("[FATAL] Too many 401s — manual intervention required. Halting all reconnects.");
                    console.error("[FATAL] Wait 24-48h before attempting a new QR scan.");
                    try {
                        await axios.post(`${BACKEND_URL}/webhook/alert`, {
                            source: "whatsapp_bridge",
                            message: "⚠️ [FATAL] WhatsApp חסם את החיבור (2× 401 תוך 60 שניות). נדרשת התערבות ידנית — המתן 24-48 שעות לפני ניסיון חדש.",
                        }, { timeout: 5000 });
                    } catch (_) {}
                    return;
                }

                console.log("[WHATSAPP] Logged out (401) — clearing DB + local session files + LID map");
                lidMap.clear();
                // Clear DB
                const pool = getPool();
                if (pool) {
                    try { await pool.query("DELETE FROM whatsapp_sessions WHERE id IN ('main','owner_lid')"); }
                    catch (e) { console.error("[SESSION] DB clear error:", e.message); }
                }
                // Also clear local files — CRITICAL: without this, stale creds cause another 401 loop
                if (fs.existsSync(SESSION_DIR)) {
                    fs.rmSync(SESSION_DIR, { recursive: true, force: true });
                    console.log("[SESSION] Local session files cleared");
                }
                reconnectAttempts = 0;
                setTimeout(() => connectToWhatsApp(), 3000);

            } else if (reconnectAttempts >= MAX_RECONNECT) {
                _failedState = true;
                console.error(`[WHATSAPP] Max reconnect attempts (${MAX_RECONNECT}) reached — halting.`);
                console.error("[WHATSAPP] Visit /qr to scan a new QR code and restart the session.");
                // Notify backend so it can alert the owner via Telegram
                try {
                    await axios.post(`${BACKEND_URL}/webhook/alert`, {
                        source: "whatsapp_bridge",
                        message: `⚠️ WhatsApp bridge נכשל לאחר ${MAX_RECONNECT} ניסיונות (קוד ${code}). נדרשת סריקת QR חדשה.`,
                    }, { timeout: 5000 });
                } catch (_) {}

            } else {
                const delay = BACKOFF_MS[reconnectAttempts] ?? MAX_BACKOFF;
                reconnectAttempts++;
                console.log(`[WHATSAPP] Reconnect attempt ${reconnectAttempts}/${MAX_RECONNECT} in ${delay / 1000}s… (code ${code})`);
                setTimeout(() => connectToWhatsApp(), delay);
            }
        }
    });

    // ---------------------------------------------------------------------------
    // Outgoing message status updates (PENDING→SERVER_ACK→DELIVERY_ACK→READ)
    // ---------------------------------------------------------------------------
    sock.ev.on("messages.update", (updates) => {
        for (const { key, update } of updates) {
            if (!key.fromMe) continue;   // only track our own sent messages
            const STATUS = { 0: "ERROR", 1: "PENDING", 2: "SERVER_ACK", 3: "DELIVERY_ACK", 4: "READ", 5: "PLAYED" };
            const s = update.status;
            console.log(`[STATUS] id=${key.id?.slice(-8)} → ${STATUS[s] ?? s} (${s}) jid=${key.remoteJid}`);
        }
    });

    // ---------------------------------------------------------------------------
    // Incoming messages
    // ---------------------------------------------------------------------------
    sock.ev.on("messages.upsert", async ({ messages, type }) => {
        if (_warmingUp) {
            console.log(`[WARMUP] Skipping ${messages.length} message(s) — still in warmup period`);
            return;   // don't mark as seen — WhatsApp retries will be processed after warmup
        }
        // Raw diagnostic log — before ANY filtering
        console.log(`[MSG] Raw event received: ${messages.length} message(s), type=${type}`);

        for (const msg of messages) {
            const fromMe  = msg.key?.fromMe;
            const hasBody = !!msg.message;
            const msgType = Object.keys(msg.message || {})[0] || "none";
            console.log(`[MSG] id=${msg.key?.id?.slice(-8)} jid=${msg.key?.remoteJid} fromMe=${fromMe} hasBody=${hasBody} type=${msgType} upsertType=${type}`);

            if (type !== "notify") continue;   // skip history replay (type="append")
            if (fromMe) continue;
            if (!hasBody) continue;

            const msgId = msg.key.id;
            if (seen.has(msgId)) continue;
            markSeen(msgId);

            const rawJid    = msg.key.remoteJid || "";
            const isGroup   = rawJid.endsWith("@g.us");
            // Resolve @lid → @s.whatsapp.net for both DM and group participant JIDs
            const jid       = isGroup ? rawJid : resolveJid(rawJid);
            const rawSender = isGroup ? (msg.key.participant || rawJid) : rawJid;
            const senderJid = resolveJid(rawSender);
            const senderPhone = jidToPhone(senderJid);
            if (rawJid !== jid || rawSender !== senderJid) {
                console.log(`[LID] Resolved: ${rawJid} → ${jid} | sender ${rawSender} → ${senderJid}`);
            }

            // --- GROUP ---
            if (isGroup) {
                const body = msg.message?.conversation
                    || msg.message?.extendedTextMessage?.text
                    || "";

                const mentionedJids = msg.message?.extendedTextMessage?.contextInfo?.mentionedJid || [];
                const textMention   = body.toLowerCase().includes(`@${BOT_NAME.toLowerCase()}`);

                // Check if the bot's own JID is in the mentioned list
                const botJid = getBotJid();
                const jidMention = botJid
                    ? mentionedJids.some(j => j.split(":")[0] === botJid.split(":")[0])
                    : mentionedJids.length > 0;

                console.log(`[GROUP] ${jid} | sender=${senderPhone} | textMention=${textMention} jidMention=${jidMention}`);

                if (!textMention && !jidMention) continue;

                const cleaned = body.replace(new RegExp(`@${BOT_NAME}`, "gi"), "").trim();
                console.log(`[GROUP] mention detected — forwarding: ${cleaned.slice(0, 80)}`);

                try {
                    await axios.post(`${BACKEND_URL}/webhook/message`, {
                        sender: OWNER_PHONE,
                        message: cleaned,
                        timestamp: new Date().toISOString(),
                        is_group: true,
                        group_id: jid,
                        group_sender: senderPhone,
                    }, { timeout: 30000 });
                } catch (err) {
                    console.error("[GROUP] Forward error:", err.message);
                }
                continue;
            }

            // --- DIRECT MESSAGE — must be from owner ---
            if (!isOwner(senderJid)) continue;

            // Remember the exact JID the owner used — may be @lid or @s.whatsapp.net
            if (rawJid !== lastOwnerJid) {
                console.log(`[SEND] lastOwnerJid updated: ${lastOwnerJid ?? "none"} → ${rawJid}`);
                lastOwnerJid = rawJid;
            }

            // Voice note (ptt)
            const audioMsg = msg.message?.audioMessage;
            if (audioMsg?.ptt) {
                console.log("[VOICE] Downloading voice note…");
                try {
                    const buffer = await downloadMediaMessage(
                        msg, "buffer", {},
                        { logger: silentLogger, reuploadRequest: sock.updateMediaMessage }
                    );
                    await axios.post(`${BACKEND_URL}/webhook/message`, {
                        sender: OWNER_PHONE,
                        message: "",
                        timestamp: new Date().toISOString(),
                        message_type: "audio",
                        media_data: buffer.toString("base64"),
                        media_mime: audioMsg.mimetype || "audio/ogg; codecs=opus",
                    }, { timeout: 60000 });
                } catch (err) {
                    console.error("[VOICE] Error:", err.message);
                }
                continue;
            }

            // Text
            const text = msg.message?.conversation
                || msg.message?.extendedTextMessage?.text
                || msg.message?.imageMessage?.caption
                || "";

            if (!text.trim()) continue;

            console.log(`[MSG] From owner: ${text.slice(0, 80)}`);
            try {
                await axios.post(`${BACKEND_URL}/webhook/message`, {
                    sender: OWNER_PHONE,
                    message: text,
                    timestamp: new Date().toISOString(),
                }, { timeout: 30000 });
            } catch (err) {
                console.error("[WHATSAPP] Forward error:", err.message);
            }
        }
    });

    // Session backup interval: start once at module level, not per-reconnect
    if (!_sessionBackupInterval) {
        _sessionBackupInterval = setInterval(() => { if (isConnected) saveSessionToDB(); }, 10 * 60 * 1000);
    }
}

// ---------------------------------------------------------------------------
// HTTP server
// ---------------------------------------------------------------------------
const app = express();
app.use(express.json({ limit: "50mb" }));

app.get("/health", (_req, res) => res.json({ status: "ok" }));

app.get("/status", (_req, res) =>
    res.json({
        whatsapp: isConnected ? "connected" : (_failedState ? "failed" : "disconnected"),
        connected: isConnected,
        deviceReady,
        connectionState,
        reconnectAttempts,
        maxReconnect: MAX_RECONNECT,
        failed: _failedState,
    })
);

app.post("/send", async (req, res) => {
    const { phone, message, chat_id } = req.body;
    if (!message) return res.status(400).json({ error: "message is required" });
    if (connectionState !== "open" || !deviceReady) {
        console.warn(`[SEND] Blocked — state=${connectionState} deviceReady=${deviceReady}`);
        return res.status(503).json({ success: false, error: `not ready (state=${connectionState} deviceReady=${deviceReady})` });
    }
    try {
        const constructedJid  = chat_id ? normalizeJid(chat_id) : normalizeJid(phone || OWNER_PHONE);
        const isGroupTarget   = constructedJid.endsWith("@g.us");
        // Resolve @lid → @s.whatsapp.net before sending (WA only routes outgoing to @s.whatsapp.net)
        const resolvedOwner   = lastOwnerJid ? resolveJid(lastOwnerJid) : null;
        const ownerJidUsable  = resolvedOwner && !resolvedOwner.endsWith("@lid");
        const jid = (!isGroupTarget && ownerJidUsable) ? resolvedOwner : constructedJid;
        console.log(`[SEND] constructedJid=${constructedJid} lastOwnerJid=${lastOwnerJid ?? "none"} resolved=${resolvedOwner ?? "none"} → using=${jid}`);
        const sent = await sock.sendMessage(jid, { text: message });
        console.log(`[SEND] → ${jid} | status=${sent?.status} id=${sent?.key?.id}`);
        res.json({ success: true });
    } catch (err) {
        console.error("[SEND] Failed:", err.message);
        res.status(500).json({ success: false, error: err.message });
    }
});

app.post("/send-voice", async (req, res) => {
    const { to, audio, mime } = req.body;
    if (!to || !audio) return res.status(400).json({ error: "to and audio are required" });
    if (connectionState !== "open" || !deviceReady) {
        console.warn(`[VOICE-OUT] Blocked — state=${connectionState} deviceReady=${deviceReady}`);
        return res.status(503).json({ error: `not ready (state=${connectionState} deviceReady=${deviceReady})` });
    }
    console.log(`[VOICE-OUT] Sending to ${to}, audio_len=${audio?.length}`);
    try {
        const constructedJid  = normalizeJid(to);
        const isGroupTarget   = constructedJid.endsWith("@g.us");
        const resolvedOwner   = lastOwnerJid ? resolveJid(lastOwnerJid) : null;
        const ownerJidUsable  = resolvedOwner && !resolvedOwner.endsWith("@lid");
        const jid = (!isGroupTarget && ownerJidUsable) ? resolvedOwner : constructedJid;
        console.log(`[VOICE-OUT] constructedJid=${constructedJid} lastOwnerJid=${lastOwnerJid ?? "none"} resolved=${resolvedOwner ?? "none"} → using=${jid}`);
        const buffer = Buffer.from(audio, "base64");
        const sent   = await sock.sendMessage(jid, {
            audio: buffer,
            mimetype: "audio/ogg; codecs=opus",
            ptt: true,
        });
        console.log(`[VOICE-OUT] → ${jid} | status=${sent?.status} id=${sent?.key?.id}`);
        res.json({ status: "sent" });
    } catch (err) {
        console.error("[VOICE-OUT] Failed:", err.message);
        res.status(500).json({ error: err.message });
    }
});

app.post("/reset-session", async (_req, res) => {
    console.log("[SESSION] Reset requested — clearing session and reconnecting");
    // 1. Delete from DB
    const pool = getPool();
    if (pool) {
        try { await pool.query("DELETE FROM whatsapp_sessions WHERE id IN ('main','owner_lid')"); }
        catch (e) { console.error("[SESSION] DB delete error:", e.message); }
    }
    // 2. Delete local session files
    if (fs.existsSync(SESSION_DIR)) {
        fs.rmSync(SESSION_DIR, { recursive: true, force: true });
        console.log("[SESSION] Local session files deleted");
    }
    // 3. Clear LID map
    lidMap.clear();
    // 4. Close existing socket and reconnect
    if (keepAliveInterval) { clearInterval(keepAliveInterval); keepAliveInterval = null; }
    isConnected = false;
    reconnectAttempts = 0;
    _failedState = false;
    if (sock) {
        try { sock.ev.removeAllListeners(); await sock.ws?.close(); } catch (_) {}
        sock = null;
    }
    res.json({ success: true, message: "Session cleared — scan QR at /qr" });
    setTimeout(() => connectToWhatsApp(), 1000);
});

app.get("/qr", async (_req, res) => {
    if (isConnected) {
        return res.send(
            `<h2 style="font-family:sans-serif;text-align:center;padding:40px">✅ ${BOT_NAME} is connected!</h2>`
        );
    }
    if (!latestQR) {
        return res.send(
            `<h2 style="font-family:sans-serif;text-align:center;padding:40px">⏳ Waiting for QR…</h2>` +
            `<script>setTimeout(()=>location.reload(),3000)</script>`
        );
    }
    try {
        const dataUrl = await QRCode.toDataURL(latestQR);
        res.send(`<!DOCTYPE html>
<html><head><title>${BOT_NAME} QR</title></head>
<body style="font-family:sans-serif;text-align:center;padding:40px">
  <h2>Scan with WhatsApp Business</h2>
  <p>Linked Devices → Link a device</p>
  <img src="${dataUrl}" style="width:280px;margin:20px" />
  <p style="color:#888;font-size:13px">Auto-refreshes every 18s</p>
  <script>setTimeout(()=>location.reload(),18000)</script>
</body></html>`);
    } catch (e) {
        res.status(500).send(`QR error: ${e.message}`);
    }
});

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------
async function main() {
    await restoreSessionFromDB();
    await _loadLidFromDB();   // restore owner LID mapping if previously saved

    await new Promise((resolve) => {
        app.listen(PORT, "0.0.0.0", () => {
            console.log(`[SERVER] ${BOT_NAME} bridge on 0.0.0.0:${PORT}`);
            resolve();
        });
    });

    await connectToWhatsApp();
}

main().catch((err) => {
    console.error("[FATAL]", err);
    process.exit(1);
});
