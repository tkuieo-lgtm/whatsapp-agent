require("dotenv").config({ path: "../.env" });
const { Client, LocalAuth, MessageMedia } = require("whatsapp-web.js");
const qrcode = require("qrcode-terminal");
const express = require("express");
const axios = require("axios");

const OWNER_PHONE = (process.env.OWNER_PHONE || "").replace(/\D/g, "");
const BOT_NAME   = process.env.BOT_NAME || "מקס";
const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";
const PORT = process.env.PORT || 3000;

if (!OWNER_PHONE) {
  console.error("[ERROR] OWNER_PHONE is not set in .env");
  process.exit(1);
}

// ---------------------------------------------------------------------------
// WhatsApp client
// ---------------------------------------------------------------------------
const client = new Client({
  authStrategy: new LocalAuth({ dataPath: "./.wwebjs_auth" }),
  puppeteer: {
    headless: true,
    args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-gpu"],
  },
});

let latestQR = null;

client.on("qr", (qr) => {
  latestQR = qr;
  console.log(`\n[QR] New QR received — scan at /qr or via terminal:\n`);
  qrcode.generate(qr, { small: true });
});

client.on("ready", () => {
  console.log(`[WHATSAPP] ${BOT_NAME} ready. Owner: ${OWNER_PHONE}`);
  console.log(`[WHATSAPP] Endpoints: /send /send-voice /health /qr`);
  console.log(`[WHATSAPP] Voice detection: ptt + audio messages enabled`);
});

client.on("auth_failure", (msg) => {
  console.error("[WHATSAPP] Auth failure:", msg);
});

client.on("disconnected", (reason) => {
  console.warn("[WHATSAPP] Disconnected:", reason);
  setTimeout(() => client.initialize(), 5000);
});

// ---------------------------------------------------------------------------
// Phone normalisation helpers
// ---------------------------------------------------------------------------
const last9 = (n) => String(n).slice(-9);

async function resolveNumber(message) {
  let contact = null;
  let contactNumber = null;
  try {
    contact = await client.getContactById(message.from);
    contactNumber = contact.number || null;
  } catch (_) {}

  const candidates = [
    message.from,
    message.author,
    contactNumber,
    contact?.id?.user,
  ]
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

  // --- GROUP message ---
  if (isGroup) {
    const body = message.body || "";
    // Only respond when @BOT_NAME is mentioned
    if (!body.includes(`@${BOT_NAME}`) && !body.toLowerCase().includes(`@${BOT_NAME.toLowerCase()}`)) {
      return;
    }
    // Strip the mention from the message
    const cleaned = body.replace(new RegExp(`@${BOT_NAME}`, "gi"), "").trim();
    console.log(`[GROUP] ${message.from} → ${cleaned.slice(0, 80)}`);

    const { candidates } = await resolveNumber(message);
    const senderIsOwner = isOwner(candidates);

    try {
      await axios.post(
        `${BACKEND_URL}/webhook/message`,
        {
          sender: OWNER_PHONE,      // validated by backend
          message: cleaned,
          timestamp: new Date().toISOString(),
          is_group: true,
          group_id: message.from,
          group_sender: message.author || message.from,
          group_sender_is_owner: senderIsOwner,
        },
        { timeout: 30000 }
      );
    } catch (err) {
      console.error("[GROUP] Failed to forward to backend:", err.message);
    }
    return;
  }

  // --- DIRECT MESSAGE ---
  const { candidates } = await resolveNumber(message);
  if (!isOwner(candidates)) return;

  console.log(`[MSG] type=${message.type} hasMedia=${message.hasMedia} body=${message.body?.slice(0,40)}`);

  // Voice note
  const isVoice = message.hasMedia && (message.type === "ptt" || message.type === "audio");
  if (isVoice) {
    console.log("[VOICE] Downloading voice note…");
    try {
      const media = await message.downloadMedia();
      await axios.post(
        `${BACKEND_URL}/webhook/message`,
        {
          sender: OWNER_PHONE,
          message: "",
          timestamp: new Date().toISOString(),
          message_type: "audio",
          media_data: media.data,
          media_mime: media.mimetype,
        },
        { timeout: 60000 }
      );
    } catch (err) {
      console.error("[VOICE] Error:", err.message);
    }
    return;
  }

  // Text message
  console.log(`[MSG] From owner: ${message.body.slice(0, 80)}`);
  try {
    await axios.post(
      `${BACKEND_URL}/webhook/message`,
      {
        sender: OWNER_PHONE,
        message: message.body,
        timestamp: new Date().toISOString(),
      },
      { timeout: 30000 }
    );
  } catch (err) {
    console.error("[WHATSAPP] Failed to forward message:", err.message);
  }
});

// ---------------------------------------------------------------------------
// HTTP server
// ---------------------------------------------------------------------------
const app = express();
app.use(express.json({ limit: "50mb" }));   // voice notes can be large

app.post("/send", async (req, res) => {
  const { phone, message, chat_id } = req.body;
  if (!message) {
    return res.status(400).json({ error: "message is required" });
  }
  try {
    // chat_id is a full JID (@c.us or @g.us); phone is a plain number
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
  console.log(`[VOICE-OUT] /send-voice called, to=${to}, audio_len=${audio?.length}`);
  const media = new MessageMedia(mime, audio, "voice.mp3");
  await client.sendMessage(to, media, { sendAudioAsVoice: true });
  console.log(`[VOICE-OUT] Sent to ${to}`);
  res.json({ status: "sent" });
});

app.get("/health", (_req, res) => {
  res.json({ status: client.info ? "ready" : "initializing", ready: Boolean(client.info) });
});

// QR page for Railway (no terminal access)
app.get("/qr", (_req, res) => {
  if (client.info) {
    return res.send(`<!DOCTYPE html><html><body style="font-family:sans-serif;text-align:center;padding:60px">
      <h2>✅ ${BOT_NAME} is connected!</h2></body></html>`);
  }
  if (!latestQR) {
    return res.send(`<!DOCTYPE html><html><body style="font-family:sans-serif;text-align:center;padding:60px">
      <h2>⏳ Waiting for QR code…</h2>
      <script>setTimeout(()=>location.reload(),3000)</script></body></html>`);
  }
  res.send(`<!DOCTYPE html>
<html>
<head><title>${BOT_NAME} QR</title></head>
<body style="font-family:sans-serif;text-align:center;padding:40px">
  <h2>Scan with WhatsApp Business</h2>
  <p>Open WhatsApp Business → Linked Devices → Link a device</p>
  <div id="qr" style="display:inline-block;margin:20px"></div>
  <p style="color:#888;font-size:14px">QR expires after 20s — page auto-refreshes every 18s</p>
  <script src="https://cdn.jsdelivr.net/npm/qrcodejs@1.0.0/qrcode.min.js"></script>
  <script>
    new QRCode(document.getElementById("qr"), {
      text: ${JSON.stringify(latestQR)}, width: 280, height: 280
    });
    setTimeout(() => location.reload(), 18000);
  </script>
</body>
</html>`);
});

app.listen(PORT, () => {
  console.log(`[SERVER] ${BOT_NAME} bridge listening on port ${PORT}`);
});

client.initialize();
