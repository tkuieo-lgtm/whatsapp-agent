require("dotenv").config({ path: "../.env" });
const { Client, LocalAuth } = require("whatsapp-web.js");
const qrcode = require("qrcode-terminal");
const express = require("express");
const axios = require("axios");

const OWNER_PHONE = (process.env.OWNER_PHONE || "").replace(/\D/g, ""); // digits only
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
  console.log("\n[QR] New QR received — scan at /qr or via terminal:\n");
  qrcode.generate(qr, { small: true });
});

client.on("ready", () => {
  console.log("[WHATSAPP] Client ready. Listening for messages from", OWNER_PHONE);
});

client.on("auth_failure", (msg) => {
  console.error("[WHATSAPP] Auth failure:", msg);
});

client.on("disconnected", (reason) => {
  console.warn("[WHATSAPP] Disconnected:", reason);
  // Re-initialize after a short delay
  setTimeout(() => client.initialize(), 5000);
});

client.on("message", async (message) => {
  // Try to resolve the real phone number via the contact
  let contact = null;
  let contactNumber = null;
  try {
    contact = await client.getContactById(message.from);
    contactNumber = contact.number || null;
  } catch (e) {
    console.error("[ERROR] getContactById failed:", e.message);
  }

  // Build candidate identifiers to check against OWNER_PHONE
  const candidates = [
    message.from,
    message.author,
    contactNumber,
    message._data?.notifyName,
    contact?.id?.user,
  ]
    .filter(Boolean)
    .map((v) => v.replace(/@\S+/, "").replace(/\D/g, ""));

  const last9 = (n) => String(n).slice(-9);
  const isOwner = candidates.some((c) => last9(c) === last9(OWNER_PHONE));

  // Only process messages from the owner's number
  if (!isOwner) {
    return;
  }

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
    console.error("[WHATSAPP] Failed to forward message to backend:", err.message);
  }
});

// ---------------------------------------------------------------------------
// HTTP server — used by the Python backend to send messages
// ---------------------------------------------------------------------------
const app = express();
app.use(express.json());

app.post("/send", async (req, res) => {
  const { phone, message } = req.body;
  if (!phone || !message) {
    return res.status(400).json({ error: "phone and message are required" });
  }

  try {
    const chatId = `${phone.replace(/\D/g, "")}@c.us`;
    await client.sendMessage(chatId, message);
    console.log(`[SEND] Message sent to ${chatId}`);
    res.json({ success: true });
  } catch (err) {
    console.error("[SEND] Failed:", err.message);
    res.status(500).json({ success: false, error: err.message });
  }
});

app.get("/health", (_req, res) => {
  const ready = Boolean(client.info);
  res.json({ status: ready ? "ready" : "initializing", ready });
});

// Browser-based QR page — useful for Railway where there is no terminal
app.get("/qr", (_req, res) => {
  if (client.info) {
    return res.send(`<!DOCTYPE html><html><body style="font-family:sans-serif;text-align:center;padding:60px">
      <h2>✅ WhatsApp is connected!</h2></body></html>`);
  }
  if (!latestQR) {
    return res.send(`<!DOCTYPE html><html><body style="font-family:sans-serif;text-align:center;padding:60px">
      <h2>⏳ Waiting for QR code…</h2>
      <script>setTimeout(()=>location.reload(),3000)</script></body></html>`);
  }
  res.send(`<!DOCTYPE html>
<html>
<head><title>WhatsApp QR</title></head>
<body style="font-family:sans-serif;text-align:center;padding:40px">
  <h2>Scan with WhatsApp Business</h2>
  <p>Open WhatsApp Business → Linked Devices → Link a device</p>
  <div id="qr" style="display:inline-block;margin:20px"></div>
  <p style="color:#888;font-size:14px">QR expires after 20s — page auto-refreshes every 18s</p>
  <script src="https://cdn.jsdelivr.net/npm/qrcodejs@1.0.0/qrcode.min.js"></script>
  <script>
    new QRCode(document.getElementById("qr"), {
      text: ${JSON.stringify(latestQR)},
      width: 280, height: 280
    });
    setTimeout(() => location.reload(), 18000);
  </script>
</body>
</html>`);
});

app.listen(PORT, () => {
  console.log(`[SERVER] WhatsApp bridge listening on port ${PORT}`);
});

client.initialize();
