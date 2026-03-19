const { Client, LocalAuth } = require("whatsapp-web.js");
const qrcode = require("qrcode-terminal");
const axios = require("axios");

const PYTHON_API_URL =
  process.env.PYTHON_API_URL || "http://localhost:8080";

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

console.log("Starting WhatsApp Web client...");
client.initialize();
