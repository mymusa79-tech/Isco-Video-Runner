import priorWorker from "./observability-worker-v6.js";

const DEFAULT_REPO = "mymusa79-tech/Isco-Video-Runner";
const STATE_TTL_MS = 15_000;
const PAGE_SIZE = 5;
let stateCache = null;
let stateCacheAt = 0;

function actor(update) {
  const callback = update && update.callback_query;
  if (callback && typeof callback === "object") {
    return {
      userId: callback.from && callback.from.id,
      chatId: callback.message && callback.message.chat && callback.message.chat.id,
      messageId: callback.message && callback.message.message_id,
      callbackId: callback.id,
      data: String(callback.data || ""),
    };
  }
  const message = update && update.message;
  return {
    userId: message && message.from && message.from.id,
    chatId: message && message.chat && message.chat.id,
    messageId: message && message.message_id,
    callbackId: "",
    data: "",
  };
}

function authorized(update, env) {
  const target = actor(update);
  const allowedUser = String(env.TELEGRAM_ALLOWED_USER_ID || "").trim();
  const allowedChat = String(env.TELEGRAM_CHAT_ID || "").trim();
  return Boolean(allowedUser) && Boolean(allowedChat)
    && String(target.userId ?? "") === allowedUser
    && String(target.chatId ?? "") === allowedChat;
}

function secretHeaderValid(request, env) {
  const expected = String(env.TELEGRAM_WEBHOOK_SECRET || "").trim();
  const actual = String(request.headers.get("X-Telegram-Bot-Api-Secret-Token") || "").trim();
  return Boolean(expected) && actual === expected;
}

async function telegram(env, method, payload = {}) {
  const token = String(env.TELEGRAM_BOT_TOKEN || "").trim();
  if (!token) throw new Error("TELEGRAM_BOT_TOKEN is missing");
  const response = await fetch(`https://api.telegram.org/bot${token}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok || !body.ok) throw new Error(`Telegram ${method} failed`);
  return body.result;
}

async function ack(env, callbackId, text = "") {
  if (!callbackId) return;
  try {
    await telegram(env, "answerCallbackQuery", {
      callback_query_id: callbackId,
      ...(text ? { text } : {}),
    });
  } catch (_) {
    // Read-only Edge acknowledgement is best effort only.
  }
}

async function updatePanel(env, target, text, rows) {
  const replyMarkup = { inline_keyboard: rows };
  if (target.callbackId && target.messageId) {
    try {
      return await telegram(env, "editMessageText", {
        chat_id: target.chatId,
        message_id: target.messageId,
        text,
        disable_web_page_preview: true,
        reply_markup: replyMarkup,
      });
    } catch (error) {
      if (String((error && error.message) || "").toLowerCase().includes("message is not modified")) return null;
    }
  }
  return telegram(env, "sendMessage", {
    chat_id: target.chatId,
    text,
    disable_web_page_preview: true,
    reply_markup: replyMarkup,
  });
}

async function decryptState(bytes, passphrase) {
  if (new TextDecoder().decode(bytes.slice(0, 8)) !== "Salted__") {
    throw new Error("Unsupported encrypted state envelope");
  }
  const salt = bytes.slice(8, 16);
  const password = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(passphrase),
    "PBKDF2",
    false,
    ["deriveBits"],
  );
  const derived = new Uint8Array(await crypto.subtle.deriveBits({
    name: "PBKDF2",
    hash: "SHA-256",
    salt,
    iterations: 10000,
  }, password, 384));
  const key = await crypto.subtle.importKey(
    "raw",
    derived.slice(0, 32),
    { name: "AES-CBC" },
    false,
    ["decrypt"],
  );
  const plain = await crypto.subtle.decrypt(
    { name: "AES-CBC", iv: derived.slice(32, 48) },
    key,
    bytes.slice(16),
  );
  const state = JSON.parse(new TextDecoder().decode(plain));
  if (!state || typeof state !== "object" || Array.isArray(state)) {
    throw new Error("Invalid Telegram state");
  }
  return state;
}

async function controlState(env) {
  if (stateCache && Date.now() - stateCacheAt <= STATE_TTL_MS) return stateCache;
  const secret = String(env.STATE_ENCRYPTION_KEY || "").trim();
  if (!secret) throw new Error("STATE_ENCRYPTION_KEY is missing at Edge");
  const repo = String(env.GITHUB_REPO || DEFAULT_REPO).trim();
  const response = await fetch(
    `https://raw.githubusercontent.com/${repo}/control-plane-state/state/control-panel.json.enc`,
    { headers: { "user-agent": "isco-telegram-editorial-v7" }, cache: "no-store" },
  );
  if (!response.ok) throw new Error(`Encrypted control state read failed: ${response.status}`);
  stateCache = await decryptState(new Uint8Array(await response.arrayBuffer()), secret);
  stateCacheAt = Date.now();
  return stateCache;
}

function savedItems(state, kind = "") {
  const items = (Array.isArray(state && state.saved_suggestions) ? state.saved_suggestions : [])
    .filter((item) => item && item.status === "available" && item.candidate && String(item.candidate.title || "").trim())
    .filter((item) => !kind || String(item.kind || "") === kind);
  return items.sort((a, b) => {
    const left = `${String(a.last_seen_at || a.saved_at || "")}|${String(a.archive_id || "")}`;
    const right = `${String(b.last_seen_at || b.saved_at || "")}|${String(b.archive_id || "")}`;
    return right.localeCompare(left);
  });
}

function usedItems(state, kind = "") {
  const items = (Array.isArray(state && state.used_topics) ? state.used_topics : [])
    .filter((item) => item && ["long", "short"].includes(String(item.kind || "")) && String(item.topic || "").trim())
    .filter((item) => !kind || String(item.kind || "") === kind);
  return items.sort((a, b) => String(b.used_at || "").localeCompare(String(a.used_at || "")));
}

function formatLabel(kind) {
  return kind === "long" ? ["🎬", "طويل"] : ["⚡", "شورت"];
}

function pageNumber(value, pages) {
  const number = Number.parseInt(String(value || "0"), 10);
  if (!Number.isFinite(number)) return 0;
  return Math.min(Math.max(number, 0), Math.max(0, pages - 1));
}

async function showSavedMenu(env, target, state) {
  const longCount = savedItems(state, "long").length;
  const shortCount = savedItems(state, "short").length;
  const text = [
    "📚 المحفوظة",
    "",
    "اختر النوع. هذه قراءة Edge مباشرة ولا تنتظر GitHub Actions:",
    "",
    `🎬 طويل — ${longCount}`,
    `⚡ شورت — ${shortCount}`,
  ].join("\n");
  await updatePanel(env, target, text, [
    [{ text: `🎬 طويل (${longCount})`, callback_data: "cmd:saved-long" }],
    [{ text: `⚡ شورت (${shortCount})`, callback_data: "cmd:saved-short" }],
    [{ text: "↩️ المواضيع", callback_data: "cmd:library_menu" }],
  ]);
}

async function showUsedMenu(env, target, state) {
  const longCount = usedItems(state, "long").length;
  const shortCount = usedItems(state, "short").length;
  const text = [
    "✅ المستعملة",
    "",
    "اختر النوع. السجل للقراءة فقط ويمنع إعادة الموضوع في البحث:",
    "",
    `🎬 طويل — ${longCount}`,
    `⚡ شورت — ${shortCount}`,
  ].join("\n");
  await updatePanel(env, target, text, [
    [{ text: `🎬 طويل (${longCount})`, callback_data: "cmd:used-long" }],
    [{ text: `⚡ شورت (${shortCount})`, callback_data: "cmd:used-short" }],
    [{ text: "↩️ المواضيع", callback_data: "cmd:library_menu" }],
  ]);
}

async function showSavedPage(env, target, state, kind, requestedPage) {
  const [icon, label] = formatLabel(kind);
  const items = savedItems(state, kind);
  if (!items.length) {
    const searchCallback = kind === "long" ? "cmd:topic" : "cmd:short";
    const searchLabel = kind === "long" ? "🎬 بحث حلقة" : "⚡ بحث شورت";
    await updatePanel(env, target, `📚 المحفوظة — ${icon} ${label}\n\nلا توجد مواضيع ${label} محفوظة حاليًا.`, [
      [{ text: searchLabel, callback_data: searchCallback }],
      [{ text: "↩️ المحفوظة", callback_data: "cmd:saved" }],
    ]);
    return;
  }
  const pages = Math.max(1, Math.ceil(items.length / PAGE_SIZE));
  const page = pageNumber(requestedPage, pages);
  const current = items.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE);
  const lines = [
    `📚 المحفوظة — ${icon} ${label}`,
    "",
    `${items.length} موضوعًا محفوظًا — صفحة ${page + 1}/${pages}.`,
    "اختر الموضوع مباشرة. الاختيار لا يبدأ Production؛ يفتح خطوة الاعتماد المحمية.",
    "",
  ];
  const rows = [];
  for (const item of current) {
    const title = String(item.candidate.title || "").trim();
    const shortTitle = title.length <= 42 ? title : `${title.slice(0, 39).trim()}…`;
    lines.push(`• ${title}`);
    rows.push([{ text: `${icon} ${shortTitle}`, callback_data: `cmd:savedpick-${String(item.archive_id || "")}` }]);
  }
  const nav = [];
  if (page > 0) nav.push({ text: "⬅️ أحدث", callback_data: `cmd:saved-${kind}-page-${page - 1}` });
  if (page + 1 < pages) nav.push({ text: "أقدم ➡️", callback_data: `cmd:saved-${kind}-page-${page + 1}` });
  if (nav.length) rows.push(nav);
  rows.push([{ text: "↩️ المحفوظة", callback_data: "cmd:saved" }]);
  await updatePanel(env, target, lines.join("\n"), rows);
}

async function showUsedPage(env, target, state, kind, requestedPage) {
  const [icon, label] = formatLabel(kind);
  const items = usedItems(state, kind);
  if (!items.length) {
    await updatePanel(env, target, `✅ المستعملة — ${icon} ${label}\n\nلا توجد مواضيع ${label} مكتملة الإنتاج في السجل حتى الآن.`, [
      [{ text: "↩️ المستعملة", callback_data: "cmd:used" }],
    ]);
    return;
  }
  const pages = Math.max(1, Math.ceil(items.length / PAGE_SIZE));
  const page = pageNumber(requestedPage, pages);
  const current = items.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE);
  const lines = [
    `✅ المستعملة — ${icon} ${label}`,
    "",
    `${items.length} موضوعًا مكتمل الإنتاج — صفحة ${page + 1}/${pages}.`,
    "هذه القائمة للقراءة فقط وتمنع إعادة الموضوع في أي بحث جديد.",
    "",
  ];
  current.forEach((item, index) => {
    lines.push(`${page * PAGE_SIZE + index + 1}) ${icon} ${String(item.topic || "")}`);
    const date = String(item.used_at || "").slice(0, 10);
    if (date) lines.push(`   ${date}`);
  });
  const rows = [];
  const nav = [];
  if (page > 0) nav.push({ text: "⬅️ أحدث", callback_data: `cmd:used-${kind}-page-${page - 1}` });
  if (page + 1 < pages) nav.push({ text: "أقدم ➡️", callback_data: `cmd:used-${kind}-page-${page + 1}` });
  if (nav.length) rows.push(nav);
  rows.push([{ text: "↩️ المستعملة", callback_data: "cmd:used" }]);
  await updatePanel(env, target, lines.join("\n"), rows);
}

function libraryRoute(data) {
  const value = String(data || "");
  if (value === "cmd:saved") return { kind: "saved_menu", format: "", page: 0 };
  if (value === "cmd:used") return { kind: "used_menu", format: "", page: 0 };
  let match = /^cmd:saved-(long|short)(?:-page-(\d+))?$/.exec(value);
  if (match) return { kind: "saved_page", format: match[1], page: Number(match[2] || 0) };
  match = /^cmd:used-(long|short)(?:-page-(\d+))?$/.exec(value);
  if (match) return { kind: "used_page", format: match[1], page: Number(match[2] || 0) };
  return null;
}

async function handleLibraryRoute(env, target, route) {
  const state = await controlState(env);
  if (route.kind === "saved_menu") return showSavedMenu(env, target, state);
  if (route.kind === "used_menu") return showUsedMenu(env, target, state);
  if (route.kind === "saved_page") return showSavedPage(env, target, state, route.format, route.page);
  return showUsedPage(env, target, state, route.format, route.page);
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method !== "POST" || url.pathname !== "/telegram" || !secretHeaderValid(request, env)) {
      return priorWorker.fetch(request, env, ctx);
    }

    let update;
    try {
      update = await request.clone().json();
    } catch (_) {
      return priorWorker.fetch(request, env, ctx);
    }
    if (!update || !Number.isInteger(update.update_id) || !authorized(update, env)) {
      return priorWorker.fetch(request, env, ctx);
    }

    const target = actor(update);
    const route = update.callback_query ? libraryRoute(target.data) : null;
    if (!route) return priorWorker.fetch(request, env, ctx);

    ctx.waitUntil((async () => {
      await ack(env, target.callbackId, "⚡ أفتح القائمة مباشرة…");
      try {
        await handleLibraryRoute(env, target, route);
      } catch (error) {
        console.error("Telegram Edge library read failed", String((error && error.message) || error || "unknown"));
        await updatePanel(env, target, "⚠️ تعذر فتح مكتبة المواضيع الآن. لم يتغير أي اختيار أو Production Run.", [
          [{ text: "↩️ المواضيع", callback_data: "cmd:library_menu" }],
          [{ text: "🏠 الرئيسية", callback_data: "cmd:menu" }],
        ]);
      }
    })());
    return new Response("OK");
  },
};
