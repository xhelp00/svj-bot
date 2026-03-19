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

client.on("ready", () => {
  console.log("WhatsApp Web client is ready!");
});

client.on("authenticated", () => {
  console.log("WhatsApp Web client authenticated.");
});

client.on("auth_failure", (msg) => {
  console.error("Authentication failed:", msg);
});

client.on("disconnected", (reason) => {
  console.error("WhatsApp Web client disconnected:", reason);
  console.log("Attempting to reconnect in 10 seconds...");
  setTimeout(() => {
    client.initialize();
  }, 10000);
});

// Auto-leave groups that are not whitelisted
client.on("group_join", async (notification) => {
  try {
    const botWid = client.info.wid._serialized;
    const addedParticipants = notification.recipientIds || [];
    const botWasAdded = addedParticipants.some(
      (id) => id === botWid || id.replace("@c.us", "") === botWid.replace("@c.us", "")
    );
    if (!botWasAdded) return;

    const groupId = notification.chatId;
    if (allowedGroups.size > 0 && !allowedGroups.has(groupId)) {
      console.warn(`[SECURITY] Bot added to unauthorized group ${groupId}, leaving...`);
      const chat = await notification.getChat();
      await chat.leave();
      console.log(`[SECURITY] Left unauthorized group ${groupId}`);
    } else {
      console.log(`[GROUP] Bot added to group ${groupId} (authorized)`);
    }
  } catch (err) {
    console.error("Error in group_join handler:", err.message);
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

  // Admin command: !allowgroup — add current group to whitelist
  if (isGroup && text.trim().toLowerCase() === "!allowgroup" && phoneNumber === ADMIN_PHONE) {
    allowedGroups.add(chatId);
    saveAllowedGroups();
    const chat = await msg.getChat();
    await chat.sendMessage(`✅ Skupina přidána na whitelist: ${chatId}`);
    console.log(`[ADMIN] !allowgroup → added ${chatId}`);
    return;
  }

  // Block messages from unauthorized groups and auto-leave
  if (isGroup && allowedGroups.size > 0 && !allowedGroups.has(chatId)) {
    console.warn(`[SECURITY] Message from unauthorized group ${chatId}, leaving...`);
    try {
      const chat = await msg.getChat();
      await chat.leave();
      console.log(`[SECURITY] Left unauthorized group ${chatId}`);
    } catch (e) {
      console.error(`[SECURITY] Failed to leave group ${chatId}: ${e.message}`);
    }
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
      await chat.sendMessage(reply);
      console.log(`[REPLY] → ${reply.substring(0, 80)}...`);
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
    await client.sendMessage(to, text);
    console.log(`[SEND] → ${to}: ${text.substring(0, 80)}...`);
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
