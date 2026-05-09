require("dotenv").config({ path: "../.env" });
const { Client, LocalAuth, MessageMedia } = require("whatsapp-web.js");
const qrcode = require("qrcode-terminal");
const express = require("express");
const axios = require("axios");
const fs = require("fs");
const path = require("path");
const { Pool } = require("pg");

const OWNER_PHONE  = (process.env.OWNER_PHONE || "").replace(/\D/g, "");
const BOT_NAME     = process.env.BOT_NAME || "מקס";
const BACKEND_URL  = process.env.BACKEND_URL || "http://localhost:8000";
const DATABASE_URL = process.env.DATABASE_URL;
const PORT         = process.env.PORT || 3000;
const SESSION_DIR  = "./.wwebjs_auth";

if (!OWNER_PHONE) {
  console.error("[ERROR] OWNER_PHONE is not set in .env");
  process.exit(1);
}

// ---------------------------------------------------------------------------
// PostgreSQL session persistence
// ---------------------------------------------------------------------------

let pgPool = null;

function getPool() {
  if (!DATABASE_URL) return null;
  if (!pgPool) {
    pgPool = new Pool({
      connectionString: DATABASE_URL,
      ssl: { rejectUnauthorized: false },
    });
  }
  return pgPool;
}

async function ensureSessionTable(pool) {
  await pool.query(`
    CREATE TABLE IF NOT EXISTS whatsapp_sessions (
      id      VARCHAR(50)  PRIMARY KEY,
      data    TEXT         NOT NULL,
      updated_at TIMESTAMPTZ DEFAULT NOW()
    )
  `);
}

async function restoreSessionFromDB() {
  const pool = getPool();
  if (!pool) {
    console.log("[SESSION] No DATABASE_URL — skipping DB restore");
    return false;
  }
  try {
    await ensureSessionTable(pool);
    const { rows } = await pool.query(
      "SELECT data FROM whatsapp_sessions WHERE id = $1",
      ["main"]
    );
    if (!rows.length) {
      console.log("[SESSION] No saved session in DB — QR scan required");
      return false;
    }
    const files = JSON.parse(rows[0].data);
    fs.mkdirSync(SESSION_DIR, { recursive: true });
    for (const [relPath, b64] of Object.entries(files)) {
      const fullPath = path.join(SESSION_DIR, relPath);
      fs.mkdirSync(path.dirname(fullPath), { recursive: true });
      fs.writeFileSync(fullPath, Buffer.from(b64, "base64"));
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
        if (entry.isDirectory()) {
          readDir(full);
        } else {
          files[path.relative(SESSION_DIR, full)] =
            fs.readFileSync(full).toString("base64");
        }
      }
    }
    readDir(SESSION_DIR);
    await ensureSessionTable(pool);
    await pool.query(
      `INSERT INTO whatsapp_sessions (id, data, updated_at)
       VALUES ('main', $1, NOW())
       ON CONFLICT (id) DO UPDATE SET data = $1, updated_at = NOW()`,
      [JSON.stringify(files)]
    );
    console.log(`[SESSION] Saved ${Object.keys(files).length} files to PostgreSQL`);
  } catch (e) {
    console.error("[SESSION] Save error:", e.message);
  }
}

// ---------------------------------------------------------------------------
// Bootstrap — restore session before starting client
// ---------------------------------------------------------------------------

async function main() {
  await restoreSessionFromDB();

  // ---------------------------------------------------------------------------
  // WhatsApp client
  // ---------------------------------------------------------------------------
  const client = new Client({
    authStrategy: new LocalAuth({ dataPath: SESSION_DIR }),
    puppeteer: {
      headless: true,
      args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-gpu"],
    },
  });

  let latestQR = null;

  client.on("qr", (qr) => {
    latestQR = qr;
    console.log(`\n[QR] New QR — scan at /qr or in terminal:\n`);
    qrcode.generate(qr, { small: true });
  });

  client.on("authenticated", async () => {
    console.log("[WHATSAPP] Authenticated — saving session to DB…");
    // Small delay to let LocalAuth write files first
    setTimeout(() => saveSessionToDB(), 3000);
  });

  client.on("ready", async () => {
    latestQR = null;
    console.log(`[WHATSAPP] ${BOT_NAME} ready. Owner: ${OWNER_PHONE}`);
    console.log(`[WHATSAPP] Endpoints: /send /send-voice /health /qr`);
    console.log(`[WHATSAPP] Voice detection: ptt + audio messages enabled`);
    // Save fresh session on every successful startup
    setTimeout(() => saveSessionToDB(), 5000);
  });

  client.on("auth_failure", (msg) => {
    console.error("[WHATSAPP] Auth failure:", msg);
  });

  client.on("disconnected", (reason) => {
    console.warn("[WHATSAPP] Disconnected:", reason);
    setTimeout(() => client.initialize(), 5000);
  });

  // Periodic session backup every 5 minutes
  setInterval(() => saveSessionToDB(), 5 * 60 * 1000);

  // ---------------------------------------------------------------------------
  // Phone helpers
  // ---------------------------------------------------------------------------
  const last9 = (n) => String(n).slice(-9);

  async function resolveNumber(message) {
    let contact = null, contactNumber = null;
    try {
      contact = await client.getContactById(message.from);
      contactNumber = contact.number || null;
    } catch (_) {}
    const candidates = [message.from, message.author, contactNumber, contact?.id?.user]
      .filter(Boolean)
      .map((v) => v.replace(/@\S+/, "").replace(/\D/g, ""));
    return { candidates, contact };
  }

  function isOwner(candidates) {
    return candidates.some((c) => last9(c) === last9(OWNER_PHONE));
  }

  // ---------------------------------------------------------------------------
  // Message handler
  // ---------------------------------------------------------------------------
  client.on("message", async (message) => {
    const isGroup = message.from.endsWith("@g.us");

    // --- GROUP ---
    if (isGroup) {
      const body = message.body || "";
      if (!body.includes(`@${BOT_NAME}`) && !body.toLowerCase().includes(`@${BOT_NAME.toLowerCase()}`)) return;
      const cleaned = body.replace(new RegExp(`@${BOT_NAME}`, "gi"), "").trim();
      console.log(`[GROUP] ${message.from} → ${cleaned.slice(0, 80)}`);
      const { candidates } = await resolveNumber(message);
      try {
        await axios.post(`${BACKEND_URL}/webhook/message`, {
          sender: OWNER_PHONE,
          message: cleaned,
          timestamp: new Date().toISOString(),
          is_group: true,
          group_id: message.from,
          group_sender: message.author || message.from,
          group_sender_is_owner: isOwner(candidates),
        }, { timeout: 30000 });
      } catch (err) {
        console.error("[GROUP] Forward error:", err.message);
      }
      return;
    }

    // --- DIRECT ---
    const { candidates } = await resolveNumber(message);
    if (!isOwner(candidates)) return;

    console.log(`[MSG] type=${message.type} hasMedia=${message.hasMedia} body=${message.body?.slice(0, 40)}`);

    // Voice note
    if (message.hasMedia && (message.type === "ptt" || message.type === "audio")) {
      console.log("[VOICE] Downloading voice note…");
      try {
        const media = await message.downloadMedia();
        await axios.post(`${BACKEND_URL}/webhook/message`, {
          sender: OWNER_PHONE,
          message: "",
          timestamp: new Date().toISOString(),
          message_type: "audio",
          media_data: media.data,
          media_mime: media.mimetype,
        }, { timeout: 60000 });
      } catch (err) {
        console.error("[VOICE] Error:", err.message);
      }
      return;
    }

    // Text
    console.log(`[MSG] From owner: ${message.body.slice(0, 80)}`);
    try {
      await axios.post(`${BACKEND_URL}/webhook/message`, {
        sender: OWNER_PHONE,
        message: message.body,
        timestamp: new Date().toISOString(),
      }, { timeout: 30000 });
    } catch (err) {
      console.error("[WHATSAPP] Forward error:", err.message);
    }
  });

  // ---------------------------------------------------------------------------
  // HTTP server
  // ---------------------------------------------------------------------------
  const app = express();
  app.use(express.json({ limit: "50mb" }));

  app.post("/send", async (req, res) => {
    const { phone, message, chat_id } = req.body;
    if (!message) return res.status(400).json({ error: "message is required" });
    try {
      const target = chat_id || `${(phone || "").replace(/\D/g, "")}@c.us`;
      await client.sendMessage(target, message);
      console.log(`[SEND] → ${target}`);
      res.json({ success: true });
    } catch (err) {
      console.error("[SEND] Failed:", err.message);
      res.status(500).json({ success: false, error: err.message });
    }
  });

  app.post("/send-voice", async (req, res) => {
    const { to, audio, mime } = req.body;
    console.log(`[VOICE-OUT] to=${to} audio_len=${audio?.length}`);
    try {
      const media = new MessageMedia(mime, audio, "voice.mp3");
      await client.sendMessage(to, media, { sendAudioAsVoice: true });
      console.log(`[VOICE-OUT] Sent to ${to}`);
      res.json({ status: "sent" });
    } catch (err) {
      console.error("[VOICE-OUT] Failed:", err.message);
      res.status(500).json({ error: err.message });
    }
  });

  app.get("/health", (_req, res) => {
    res.json({ status: client.info ? "ready" : "initializing", ready: Boolean(client.info) });
  });

  app.get("/qr", (_req, res) => {
    if (client.info) {
      return res.send(`<h2 style="font-family:sans-serif;text-align:center;padding:40px">✅ ${BOT_NAME} is connected!</h2>`);
    }
    if (!latestQR) {
      return res.send(`<h2 style="font-family:sans-serif;text-align:center;padding:40px">⏳ Waiting for QR…</h2><script>setTimeout(()=>location.reload(),3000)</script>`);
    }
    res.send(`<!DOCTYPE html>
<html><head><title>${BOT_NAME} QR</title></head>
<body style="font-family:sans-serif;text-align:center;padding:40px">
  <h2>Scan with WhatsApp Business</h2>
  <p>Linked Devices → Link a device</p>
  <div id="qr" style="display:inline-block;margin:20px"></div>
  <p style="color:#888;font-size:13px">Auto-refreshes every 18s</p>
  <script src="https://cdn.jsdelivr.net/npm/qrcodejs@1.0.0/qrcode.min.js"></script>
  <script>
    new QRCode(document.getElementById("qr"),{text:${JSON.stringify(latestQR)},width:280,height:280});
    setTimeout(()=>location.reload(),18000);
  </script>
</body></html>`);
  });

  app.listen(PORT, () => {
    console.log(`[SERVER] ${BOT_NAME} bridge on port ${PORT}`);
  });

  client.initialize();
}

main().catch((err) => {
  console.error("[FATAL]", err);
  process.exit(1);
});
