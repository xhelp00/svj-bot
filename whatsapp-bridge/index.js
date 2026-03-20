const { Client, LocalAuth } = require("whatsapp-web.js");
const qrcode = require("qrcode-terminal");
const axios = require("axios");
const fs = require("fs");
const path = require("path");

const PYTHON_API_URL =
  process.env.PYTHON_API_URL || "http://localhost:8080";

const ADMIN_PHONE = process.env.ADMIN_PHONE || "420720994342";

// Whitelist of allowed group JIDs.
// Loaded from: (1) env var ALLOWED_GROUP_IDS, (2) persistent file on volume
const ALLOWED_GROUPS_FILE = "/app/.wwebjs_auth/allowed_groups.json";

function loadAllowedGroups() {
  const groups = new Set();
  // From env var
  if (process.env.ALLOWED_GROUP_IDS) {
    process.env.ALLOWED_GROUP_IDS.split(",").map((s) => s.trim()).filter(Boolean)
      .forEach((g) => groups.add(g));
  }
  // From persistent file
  try {
    const data = JSON.parse(fs.readFileSync(ALLOWED_GROUPS_FILE, "utf8"));
    data.forEach((g) => groups.add(g));
  } catch (e) {
    // File doesn't exist yet — that's fine
  }
  return groups;
}

function saveAllowedGroups() {
  try {
    fs.writeFileSync(ALLOWED_GROUPS_FILE, JSON.stringify([...allowedGroups], null, 2));
  } catch (e) {
    console.error("Failed to save allowed groups:", e.message);
  }
}

const allowedGroups = loadAllowedGroups();

// Clean up stale Chromium lock files to prevent restart failures
const AUTH_DIR = "/app/.wwebjs_auth";
function cleanChromiumLocks(dir) {
  const lockFiles = ["SingletonLock", "SingletonCookie", "SingletonSocket"];
  try {
    const entries = fs.readdirSync(dir, { withFileTypes: true, recursive: true });
    for (const entry of entries) {
      if (lockFiles.includes(entry.name)) {
        const fullPath = path.join(entry.parentPath || entry.path, entry.name);
        fs.unlinkSync(fullPath);
        console.log(`[CLEANUP] Removed stale lock: ${fullPath}`);
      }
    }
  } catch (e) {
    // Auth dir may not exist on first run
  }
}
cleanChromiumLocks(AUTH_DIR);

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: "/app/.wwebjs_auth" }),
  puppeteer: {
    headless: true,
    args: [
      "--no-sandbox",
      "--disable-setuid-sandbox",
      "--disable-dev-shm-usage",
      "--disable-gpu",
      "--single-process",
    ],
    executablePath: process.env.PUPPETEER_EXECUTABLE_PATH || undefined,
  },
});

client.on("qr", (qr) => {
  console.log("Scan this QR code with WhatsApp:");
  qrcode.generate(qr, { small: true });
});

let presenceInterval = null;

client.on("ready", () => {
  console.log("WhatsApp Web client is ready!");
  // Clear any previous interval to prevent stacking after reconnects
  if (presenceInterval) clearInterval(presenceInterval);
  // Periodically signal "online" presence to appear human-like
  client.sendPresenceAvailable();
  presenceInterval = setInterval(() => {
    try { client.sendPresenceAvailable(); } catch (e) { /* ignore */ }
  }, 5 * 60 * 1000); // every 5 minutes
  // Reset reconnect counter on successful connection
  if (reconnectAttempt > 0) {
    console.log(`[RECONNECT] Recovered after ${reconnectAttempt} attempt(s)`);
    reconnectAttempt = 0;
  }
});

client.on("authenticated", () => {
  console.log("WhatsApp Web client authenticated.");
});

client.on("auth_failure", async (msg) => {
  console.error("Authentication failed:", msg);
  console.log("[AUTH] Clearing session data and restarting...");
  try {
    // Delete session to force fresh QR on next start
    fs.rmSync(path.join(AUTH_DIR, "session"), { recursive: true, force: true });
  } catch (e) { /* ignore */ }
  // Let Docker restart policy handle the restart
  process.exit(1);
});

let reconnectAttempt = 0;
const MAX_RECONNECT_ATTEMPTS = 5;
const RECONNECT_DELAYS = [60, 120, 300, 600, 1800]; // seconds: 1min, 2min, 5min, 10min, 30min

client.on("disconnected", (reason) => {
  console.error("WhatsApp Web client disconnected:", reason);
  reconnectAttempt++;
  if (reconnectAttempt > MAX_RECONNECT_ATTEMPTS) {
    console.error(`[RECONNECT] Giving up after ${MAX_RECONNECT_ATTEMPTS} attempts. Manual restart required.`);
    return;
  }
  const delayIdx = Math.min(reconnectAttempt - 1, RECONNECT_DELAYS.length - 1);
  const delaySec = RECONNECT_DELAYS[delayIdx];
  console.log(`[RECONNECT] Attempt ${reconnectAttempt}/${MAX_RECONNECT_ATTEMPTS} in ${delaySec}s...`);
  setTimeout(() => {
    client.initialize();
  }, delaySec * 1000);
});


// Log when bot is added to a group (no auto-leave to avoid WhatsApp restrictions)
client.on("group_join", async (notification) => {
  try {
    const groupId = notification.chatId;
    const isAllowed = allowedGroups.size === 0 || allowedGroups.has(groupId);
    console.log(`[GROUP] Bot added to group ${groupId} (${isAllowed ? "authorized" : "NOT authorized — ignoring messages"})`);
  } catch (err) {
    // ignore
  }
});

client.on("message", async (msg) => {
  // Ignore own messages
  if (msg.fromMe) return;

  // Ignore non-text messages (images, stickers, etc.)
  if (msg.type !== "chat") return;

  const isGroup = msg.from.endsWith("@g.us");
  const sender = isGroup ? msg.author : msg.from;
  const senderNumber = sender
    ? sender.replace("@c.us", "").replace("@lid", "")
    : "";
  const chatId = msg.from;

  // Get sender name and phone number
  let senderName = "";
  let phoneNumber = senderNumber;
  try {
    const contact = await msg.getContact();
    senderName = contact.pushname || contact.name || "";
    // Resolve LID to actual phone number
    if (contact.number) {
      phoneNumber = contact.number;
    }
  } catch (e) {
    // ignore, name is optional
  }

  const text = msg.body;
  if (!text || text.trim().length === 0) return;

  // Mark all messages as read (humans read everything, not just what they reply to)
  try {
    const chat = await msg.getChat();
    // Small random delay before reading (0.5-2s) — humans don't read instantly
    await new Promise((r) => setTimeout(r, 500 + Math.random() * 1500));
    await chat.sendSeen();
  } catch (e) { /* ignore */ }

  // Admin command: !allowgroup — add current group to whitelist
  if (isGroup && text.trim().toLowerCase() === "!allowgroup" && phoneNumber === ADMIN_PHONE) {
    allowedGroups.add(chatId);
    saveAllowedGroups();
    const chat = await msg.getChat();
    await chat.sendMessage(`✅ Skupina přidána na whitelist: ${chatId}`);
    console.log(`[ADMIN] !allowgroup → added ${chatId}`);
    return;
  }

  // Don't respond in groups at night (23:00-06:00 CET) — humans sleep
  // DMs are always answered; groups only during daytime
  if (isGroup) {
    const hour = new Date().toLocaleString("en-US", { timeZone: "Europe/Prague", hour12: false, hour: "numeric" });
    if (parseInt(hour) >= 23 || parseInt(hour) < 6) {
      return;
    }
  }

  // Silently ignore messages from unauthorized groups (no auto-leave)
  if (isGroup && allowedGroups.size > 0 && !allowedGroups.has(chatId)) {
    console.log(`[SECURITY] Ignoring message from unauthorized group ${chatId}`);
    return;
  }

  console.log(
    `[${isGroup ? "GROUP" : "DM"}] ${senderName} (${phoneNumber}): ${text}`
  );

  try {
    const response = await axios.post(
      `${PYTHON_API_URL}/message`,
      {
        text: text,
        sender: phoneNumber,
        sender_name: senderName,
        is_group: isGroup,
        chat_id: chatId,
      },
      { timeout: 60000 }
    );

    const reply = response.data.reply;
    if (reply) {
      const chat = await msg.getChat();
      // Simulate reading (1-3s), then typing indicator (sendSeen already called above)
      const readDelay = 1000 + Math.random() * 2000;
      await new Promise((r) => setTimeout(r, readDelay));
      await chat.sendStateTyping();
      // Typing duration based on reply length (50-80ms per char, min 2s, max 10s)
      const typeDelay = Math.min(10000, Math.max(2000, reply.length * (50 + Math.random() * 30)));
      await new Promise((r) => setTimeout(r, typeDelay));
      await msg.reply(reply);
      const totalDelay = (readDelay + typeDelay) / 1000;
      console.log(`[REPLY] (${totalDelay.toFixed(1)}s delay) → ${reply.substring(0, 80)}...`);
    }
  } catch (error) {
    console.error(
      "Error communicating with Python API:",
      error.message
    );
  }
});

// --- HTTP server for proactive messaging (used by Python API) ---
const express = require("express");
const expressApp = express();
expressApp.use(express.json());

expressApp.post("/send", async (req, res) => {
  const { to, text } = req.body;
  if (!to || !text) {
    return res.status(400).json({ error: "Missing 'to' or 'text'" });
  }
  try {
    // Simulate typing before proactive message
    const chat = await client.getChatById(to);
    await chat.sendStateTyping();
    const delay = 2000 + Math.random() * 3000;
    await new Promise((r) => setTimeout(r, delay));
    await client.sendMessage(to, text);
    console.log(`[SEND] (${(delay/1000).toFixed(1)}s delay) → ${to}: ${text.substring(0, 80)}...`);
    res.json({ status: "sent" });
  } catch (error) {
    console.error("Error sending message:", error.message);
    res.status(500).json({ error: error.message });
  }
});

const BRIDGE_PORT = process.env.BRIDGE_PORT || 3000;
expressApp.listen(BRIDGE_PORT, () => {
  console.log(`Bridge HTTP server listening on port ${BRIDGE_PORT}`);
});

console.log("Starting WhatsApp Web client...");
client.initialize();
