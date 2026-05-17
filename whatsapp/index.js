require("dotenv").config({ path: "../.env" });

const {
    default: makeWASocket,
    DisconnectReason,
    useMultiFileAuthState,
    fetchLatestBaileysVersion,
    downloadMediaMessage,
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
const OWNER_PHONE      = (process.env.OWNER_PHONE || "").replace(/\D/g, "");  // owner identity (message filtering)
const AGENT_PHONE      = (process.env.AGENT_PHONE || OWNER_PHONE).replace(/\D/g, "");  // WA account running the bot (pairing)
const BOT_NAME         = process.env.BOT_NAME || "מקס";
const BACKEND_URL      = process.env.BACKEND_URL || "http://localhost:8000";
const DATABASE_URL     = process.env.DATABASE_URL;
const PORT             = process.env.PORT || 3000;
const SESSION_DIR      = "./.baileys_auth";
const USE_PAIRING_CODE = process.env.USE_PAIRING_CODE === "true";

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
let latestQR              = null;
let latestPairingCode     = null;
let sock                  = null;
let isConnected           = false;
let keepAliveInterval     = null;
let _sessionBackupInterval = null;   // module-level — only one interval ever
let reconnectAttempts     = 0;
const MAX_RECONNECT       = 10;
const BACKOFF_MS          = [30_000, 60_000, 120_000];
const MAX_BACKOFF         = 5 * 60_000;
const seen                = new Set();

function markSeen(id) {
    seen.add(id);
    setTimeout(() => seen.delete(id), 30_000);
}

// Minimal silent logger for Baileys
const silentLogger = {
    level: "silent",
    trace: () => {}, debug: () => {}, info: () => {},
    warn: () => {}, error: () => {}, fatal: () => {},
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

    sock = makeWASocket({
        version,
        auth: state,
        printQRInTerminal: false,   // never print QR in terminal (Railway has no tty)
        logger: silentLogger,
        browser: ["Ubuntu", "Chrome", "20.0.04"],
        syncFullHistory: false,
        keepAliveIntervalMs: 20_000,
        getMessage: async (_key) => ({ conversation: "" }),
    });

    // Build lid→phone map from contact sync (fires during connection init)
    sock.ev.on("contacts.upsert", (contacts) => {
        console.log(`[LID] contacts.upsert fired: ${contacts.length} contact(s)`);
        for (const c of contacts) {
            // Log every contact so we can see what fields are available
            if (c.lid || c.id?.endsWith("@lid")) {
                console.log(`[LID] Contact with lid: id=${c.id} lid=${c.lid} name=${c.name || c.notify || "?"}`);
            }
            if (c.id && c.lid) {
                lidMap.set(c.lid, c.id);
                console.log(`[LID] Mapped ${c.lid} → ${c.id}`);
            }
        }
        console.log(`[LID] Map now has ${lidMap.size} entries`);
    });

    // Persist credentials on every update — debounced to prevent DB flooding
    sock.ev.on("creds.update", async () => {
        await saveCreds();
        scheduleSave();   // debounced 30 s — NOT immediate
    });

    // Connection lifecycle
    sock.ev.on("connection.update", async ({ connection, lastDisconnect, qr, isNewLogin, receivedPendingNotifications }) => {
        const code = lastDisconnect ? new Boom(lastDisconnect?.error)?.output?.statusCode : null;
        console.log(`[CONN] connection=${connection ?? "-"} qr=${!!qr} code=${code ?? "-"} isNewLogin=${isNewLogin ?? "-"} pendingNotif=${receivedPendingNotifications ?? "-"}`);
        if (lastDisconnect?.error) console.log(`[CONN] error: ${lastDisconnect.error.message ?? lastDisconnect.error}`);

        if (qr) {
            if (USE_PAIRING_CODE && !state.creds.registered) {
                // qr event = socket connected, WhatsApp waiting for auth.
                // This is the correct moment to request a pairing code.
                // A setTimeout is WRONG — the socket isn't connected yet at that point.
                try {
                    const code = await sock.requestPairingCode(AGENT_PHONE);
                    latestPairingCode = code;
                    console.log(`[PAIR] Code for agent ${AGENT_PHONE}: ${code}`);
                    console.log("[PAIR] WhatsApp → Settings → Linked Devices → Link with phone number");
                } catch (e) {
                    console.error("[PAIR] requestPairingCode failed:", e.message);
                }
            } else {
                latestQR = qr;
                console.log("[QR] New QR received — open /qr in browser to scan");
            }
        }

        if (connection === "open") {
            latestQR      = null;
            isConnected   = true;
            reconnectAttempts = 0;
            _failedState  = false;
            console.log(`[WHATSAPP] ${BOT_NAME} connected!`);
            await saveSessionToDB();

            // Resolve owner's @lid immediately after connect
            setTimeout(resolveOwnerLid, 3000);

            // Manual keepalive: send presence update every 20 s
            if (keepAliveInterval) clearInterval(keepAliveInterval);
            keepAliveInterval = setInterval(async () => {
                if (!isConnected || !sock) return;
                try { await sock.sendPresenceUpdate("available"); }
                catch (_) { /* ignore — WS ping failures are non-fatal */ }
            }, 20_000);
        }

        if (connection === "close") {
            isConnected = false;
            if (keepAliveInterval) { clearInterval(keepAliveInterval); keepAliveInterval = null; }

            const code = new Boom(lastDisconnect?.error)?.output?.statusCode;
            console.log(`[WHATSAPP] Disconnected — code: ${code}`);

            if (code === DisconnectReason.loggedOut) {
                console.log("[WHATSAPP] Logged out (401) — clearing DB + local session files + LID map");
                lidMap.clear();
                latestPairingCode = null;
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
        reconnectAttempts,
        maxReconnect: MAX_RECONNECT,
        failed: _failedState,
    })
);

app.post("/send", async (req, res) => {
    const { phone, message, chat_id } = req.body;
    if (!message) return res.status(400).json({ error: "message is required" });
    try {
        const jid  = chat_id ? normalizeJid(chat_id) : normalizeJid(phone || OWNER_PHONE);
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
    console.log(`[VOICE-OUT] Sending to ${to}, audio_len=${audio?.length}`);
    try {
        const jid    = normalizeJid(to);
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

app.get("/pair", (_req, res) => {
    if (isConnected) {
        return res.send(`<h2 style="font-family:sans-serif;text-align:center;padding:40px">✅ ${BOT_NAME} is connected!</h2>`);
    }
    if (!latestPairingCode) {
        return res.send(
            `<h2 style="font-family:sans-serif;text-align:center;padding:40px">⏳ Requesting pairing code…</h2>` +
            `<p style="text-align:center;color:#555">Make sure USE_PAIRING_CODE=true and the server just started.</p>` +
            `<script>setTimeout(()=>location.reload(),3000)</script>`
        );
    }
    const code = latestPairingCode.match(/.{1,4}/g)?.join("-") || latestPairingCode;
    res.send(`<!DOCTYPE html>
<html><head><title>${BOT_NAME} Pairing</title></head>
<body style="font-family:sans-serif;text-align:center;padding:40px;background:#f5f5f5">
  <h2>🔑 Pairing Code</h2>
  <p>Open WhatsApp → Settings → Linked Devices → <b>Link with phone number</b></p>
  <div style="font-size:3em;font-weight:bold;letter-spacing:8px;margin:30px;color:#075e54">${code}</div>
  <p style="color:#888;font-size:13px">Phone: ${OWNER_PHONE} | Code expires in ~60s</p>
  <script>setTimeout(()=>location.reload(),30000)</script>
</body></html>`);
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
