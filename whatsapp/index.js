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
const OWNER_PHONE  = (process.env.OWNER_PHONE || "").replace(/\D/g, "");
const BOT_NAME     = process.env.BOT_NAME || "מקס";
const BACKEND_URL  = process.env.BACKEND_URL || "http://localhost:8000";
const DATABASE_URL = process.env.DATABASE_URL;
const PORT         = process.env.PORT || 3000;
const SESSION_DIR  = "./.baileys_auth";

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
    // Backend sends @c.us (whatsapp-web.js format) — Baileys uses @s.whatsapp.net
    if (jid.endsWith("@c.us")) return jid.replace("@c.us", "@s.whatsapp.net");
    if (jid.endsWith("@g.us")) return jid;  // groups unchanged
    if (jid.includes("@")) return jid;
    return `${jid.replace(/\D/g, "")}@s.whatsapp.net`;
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let latestQR       = null;
let sock           = null;
let isConnected    = false;
let reconnectAttempts = 0;
const BACKOFF_MS   = [30_000, 60_000, 120_000];
const MAX_BACKOFF  = 5 * 60_000;
const seen         = new Set();

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
        printQRInTerminal: true,
        logger: silentLogger,
        browser: ["Chrome (Linux)", "Chrome", "120.0.0.0"],
        syncFullHistory: false,
    });

    // Persist credentials on every update
    sock.ev.on("creds.update", async () => {
        await saveCreds();
        await saveSessionToDB();
    });

    // Connection lifecycle
    sock.ev.on("connection.update", async ({ connection, lastDisconnect, qr }) => {
        if (qr) {
            latestQR = qr;
            console.log("[QR] New QR received — open /qr in browser to scan");
        }

        if (connection === "open") {
            latestQR = null;
            isConnected = true;
            reconnectAttempts = 0;
            console.log(`[WHATSAPP] ${BOT_NAME} connected!`);
            await saveSessionToDB();
        }

        if (connection === "close") {
            isConnected = false;
            const code = new Boom(lastDisconnect?.error)?.output?.statusCode;
            console.log(`[WHATSAPP] Disconnected — code: ${code}`);

            if (code === DisconnectReason.loggedOut) {
                console.log("[WHATSAPP] Logged out — clearing DB session");
                const pool = getPool();
                if (pool) {
                    try { await pool.query("DELETE FROM whatsapp_sessions WHERE id=$1", ["main"]); }
                    catch (e) { console.error("[SESSION] Clear error:", e.message); }
                }
                // Reconnect from scratch (will show QR)
                setTimeout(() => connectToWhatsApp(), 3000);
            } else {
                const delay = BACKOFF_MS[reconnectAttempts] ?? MAX_BACKOFF;
                reconnectAttempts++;
                console.log(`[WHATSAPP] Reconnect attempt ${reconnectAttempts} in ${delay / 1000}s…`);
                setTimeout(() => connectToWhatsApp(), delay);
            }
        }
    });

    // ---------------------------------------------------------------------------
    // Incoming messages
    // ---------------------------------------------------------------------------
    sock.ev.on("messages.upsert", async ({ messages, type }) => {
        if (type !== "notify") return;

        for (const msg of messages) {
            if (msg.key.fromMe) continue;
            if (!msg.message) continue;

            const msgId = msg.key.id;
            if (seen.has(msgId)) continue;
            markSeen(msgId);

            const jid      = msg.key.remoteJid || "";
            const isGroup  = jid.endsWith("@g.us");
            const senderJid = isGroup ? (msg.key.participant || jid) : jid;
            const senderPhone = jidToPhone(senderJid);

            // --- GROUP ---
            if (isGroup) {
                const body = msg.message?.conversation
                    || msg.message?.extendedTextMessage?.text
                    || "";

                const mentionedJids = msg.message?.extendedTextMessage?.contextInfo?.mentionedJid || [];
                const textMention   = body.toLowerCase().includes(`@${BOT_NAME.toLowerCase()}`);
                if (!textMention && mentionedJids.length === 0) continue;

                const cleaned = body.replace(new RegExp(`@${BOT_NAME}`, "gi"), "").trim();
                console.log(`[GROUP] ${jid} | sender=${senderPhone} | ${cleaned.slice(0, 80)}`);

                try {
                    await axios.post(`${BACKEND_URL}/webhook/message`, {
                        sender: OWNER_PHONE,
                        message: cleaned,
                        timestamp: new Date().toISOString(),
                        is_group: true,
                        group_id: jid,
                        group_sender: senderPhone,
                        group_sender_is_owner: isOwner(senderJid),
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

    // Periodic session backup every 5 minutes
    setInterval(() => saveSessionToDB(), 5 * 60 * 1000);
}

// ---------------------------------------------------------------------------
// HTTP server
// ---------------------------------------------------------------------------
const app = express();
app.use(express.json({ limit: "50mb" }));

app.get("/health", (_req, res) => res.json({ status: "ok" }));

app.get("/status", (_req, res) =>
    res.json({ whatsapp: isConnected ? "connected" : "disconnected", connected: isConnected })
);

app.post("/send", async (req, res) => {
    const { phone, message, chat_id } = req.body;
    if (!message) return res.status(400).json({ error: "message is required" });
    try {
        const jid = chat_id ? normalizeJid(chat_id) : normalizeJid(phone || OWNER_PHONE);
        await sock.sendMessage(jid, { text: message });
        console.log(`[SEND] → ${jid}`);
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
        await sock.sendMessage(jid, {
            audio: buffer,
            mimetype: "audio/ogg; codecs=opus",
            ptt: true,   // voice note with waveform display
        });
        console.log(`[VOICE-OUT] Sent to ${jid}`);
        res.json({ status: "sent" });
    } catch (err) {
        console.error("[VOICE-OUT] Failed:", err.message);
        res.status(500).json({ error: err.message });
    }
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
