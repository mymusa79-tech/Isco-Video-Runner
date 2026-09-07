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

function productionCategory(entry, request) {
  const kind = String((request && request.kind) || (entry && entry.kind) || "");
  const scope = String((request && request.approval_scope) || (entry && entry.approval_scope) || "");
  if (kind === "short" || scope === "short_only") return "short";
  if (kind === "long" && scope === "long_only") return "long";
  if (kind === "long" && scope === "long_plus_sibling_shorts") return "bundle";
  return "";
}

function productionTimestamp(entry) {
  for (const key of ["completed_at", "qc_pending_at", "failed_at", "consumed_at", "reserved_at", "requested_at"]) {
    const value = String((entry && entry[key]) || "").trim();
    if (value) return value;
  }
  return "";
}

function productionItems(state, category = "") {
  const queue = Array.isArray(state && state.production_queue) ? state.production_queue : [];
  const requests = state && state.requests && typeof state.requests === "object" ? state.requests : {};
  const seen = new Set();
  const items = [];
  for (let index = queue.length - 1; index >= 0; index -= 1) {
    const entry = queue[index];
    if (!entry || typeof entry !== "object") continue;
    const requestId = String(entry.request_id || "").trim();
    if (!requestId || seen.has(requestId)) continue;
    const request = requests[requestId];
    if (!request || typeof request !== "object") continue;
    const itemCategory = productionCategory(entry, request);
    if (!itemCategory) continue;
    seen.add(requestId);
    if (category && itemCategory !== category) continue;
    items.push({
      requestId,
      entry,
      request,
      category: itemCategory,
      title: String(request.approved_topic || "").trim() || requestId,
      timestamp: productionTimestamp(entry),
    });
  }
  return items.sort((a, b) => `${b.timestamp}|${b.requestId}`.localeCompare(`${a.timestamp}|${a.requestId}`));
}

function productionCategoryMeta(category) {
  if (category === "short") return ["⚡", "شورت مستقل"];
  if (category === "long") return ["🎬", "فيديو"];
  return ["🎬➕⚡", "فيديو + Shorts"];
}

function productionStatus(entry) {
  const status = String((entry && entry.status) || "");
  if (status === "completed") return ["✅", "مكتمل"];
  if (status === "qc_pending") return ["🟠", "ينتظر Gold"];
  if (["pending_dispatch", "dispatch_reserved", "dispatch_consumed"].includes(status)) return ["🔵", "قيد الإنتاج"];
  if (status === "failed") return ["❌", "فشل"];
  return ["⚪", status || "غير معروف"];
}

function formatLabel(kind) {
  return kind === "long" ? ["🎬", "طويل"] : ["⚡", "شورت"];
}

function pageNumber(value, pages) {
  const number = Number.parseInt(String(value || "0"), 10);
  if (!Number.isFinite(number)) return 0;
  return Math.min(Math.max(number, 0), Math.max(0, pages - 1));
}

async function showScopeSearch(env, target) {
  const text = [
    "🔎 بحث جديد",
    "",
    "اختر النتيجة التي تريدها قبل البحث:",
    "🎬➕⚡ حلقة + Shorts — الفكرة المختارة تُعتمد مع 2–3 Shorts مختلفة حسب المادة.",
    "🎬 حلقة فقط — الفكرة المختارة تُعتمد كحلقة فقط.",
    "⚡ Short فقط — بحث Short مستقل.",
    "",
    "هذا يحدد نطاق القرار فقط؛ لا يبدأ Production.",
  ].join("\n");
  await updatePanel(env, target, text, [
    [{ text: "🎬➕⚡ حلقة + Shorts", callback_data: "cmd:topic_bundle" }],
    [{ text: "🎬 حلقة فقط", callback_data: "cmd:topic_long" }],
    [{ text: "⚡ Short فقط", callback_data: "cmd:short" }],
    [{ text: "↩️ الرئيسية", callback_data: "cmd:menu" }],
  ]);
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

async function showProductionMenu(env, target, state) {
  const shortCount = productionItems(state, "short").length;
  const longCount = productionItems(state, "long").length;
  const bundleCount = productionItems(state, "bundle").length;
  const pending = productionItems(state).filter((item) => item.entry.status === "qc_pending").length;
  const text = [
    "🎞️ مكتبة الإنتاجات",
    "",
    "اختر نوع الإنتاج أولًا، ثم الفيديو نفسه. كل فيديو يحتفظ بإجراءاته وحالته منفصلة.",
    "",
    `⚡ شورت مستقل — ${shortCount}`,
    `🎬 فيديو — ${longCount}`,
    `🎬➕⚡ فيديو + Shorts — ${bundleCount}`,
    pending ? `\n🟠 ينتظر Gold حاليًا: ${pending}` : "",
    "",
    "🔐 فتح القوائم لا يبدأ Production ولا يتجاوز Gold.",
  ].filter(Boolean).join("\n");
  await updatePanel(env, target, text, [
    [{ text: `⚡ شورت مستقل (${shortCount})`, callback_data: "cmd:productions-short" }],
    [{ text: `🎬 فيديو (${longCount})`, callback_data: "cmd:productions-long" }],
    [{ text: `🎬➕⚡ فيديو + Shorts (${bundleCount})`, callback_data: "cmd:productions-bundle" }],
    [{ text: "🏠 الرئيسية", callback_data: "cmd:menu" }],
  ]);
}

async function showProductionPage(env, target, state, category, requestedPage) {
  const [icon, label] = productionCategoryMeta(category);
  const items = productionItems(state, category);
  if (!items.length) {
    await updatePanel(env, target, `🎞️ ${label}\n\nلا توجد إنتاجات في هذه القائمة حتى الآن.`, [
      [{ text: "↩️ مكتبة الإنتاجات", callback_data: "cmd:productions" }],
      [{ text: "🏠 الرئيسية", callback_data: "cmd:menu" }],
    ]);
    return;
  }
  const pages = Math.max(1, Math.ceil(items.length / PAGE_SIZE));
  const page = pageNumber(requestedPage, pages);
  const current = items.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE);
  const rows = [];
  const lines = [
    `🎞️ ${icon} ${label}`,
    "",
    `${items.length} إنتاجًا — صفحة ${page + 1}/${pages}.`,
    "اضغط على الفيديو لعرض حالته وإجراءاته.",
    "",
  ];
  current.forEach((item) => {
    const [statusIcon, statusLabel] = productionStatus(item.entry);
    const shortTitle = item.title.length <= 36 ? item.title : `${item.title.slice(0, 33).trim()}…`;
    lines.push(`${statusIcon} ${item.title}`);
    rows.push([{
      text: `${statusIcon} ${shortTitle}`,
      callback_data: `cmd:production-item-${item.requestId}`,
    }]);
    if (statusLabel === "ينتظر Gold") lines.push("   تابع Gold متاح داخل الفيديو فقط.");
  });
  const nav = [];
  if (page > 0) nav.push({ text: "⬅️ أحدث", callback_data: `cmd:productions-${category}-page-${page - 1}` });
  if (page + 1 < pages) nav.push({ text: "أقدم ➡️", callback_data: `cmd:productions-${category}-page-${page + 1}` });
  if (nav.length) rows.push(nav);
  rows.push([{ text: "↩️ مكتبة الإنتاجات", callback_data: "cmd:productions" }]);
  await updatePanel(env, target, lines.join("\n"), rows);
}

async function showProductionItem(env, target, state, requestId) {
  const item = productionItems(state).find((candidate) => candidate.requestId === requestId);
  if (!item) {
    await updatePanel(env, target, "⚠️ لم يعد هذا الإنتاج موجودًا في السجل الحالي.", [
      [{ text: "↩️ مكتبة الإنتاجات", callback_data: "cmd:productions" }],
    ]);
    return;
  }
  const [icon, label] = productionCategoryMeta(item.category);
  const [statusIcon, statusLabel] = productionStatus(item.entry);
  const date = String(item.timestamp || "").slice(0, 10);
  const lines = [
    `${icon} ${item.title}`,
    "",
    `النوع: ${label}`,
    `الحالة: ${statusIcon} ${statusLabel}`,
    `المعرّف: ${item.requestId}`,
  ];
  if (date) lines.push(`التاريخ: ${date}`);
  if (item.entry.status === "qc_pending") {
    const pending = item.entry.qc_pending || {};
    lines.push(
      "",
      "Final Master موجود ومثبت، لكن Gold لم يُقبل بعد بسبب سعة مزود الرؤية.",
      "«تابع Gold» يعيد Gold فقط على نفس البايتات؛ لا تخطيط، لا بحث بصري، لا TTS، ولا رندر جديد.",
    );
    if (pending.source_run_id) lines.push(`Source Run: ${String(pending.source_run_id)}`);
  } else if (item.entry.status === "failed") {
    lines.push("", "هذا فشل عادي وليس QC_PENDING؛ لذلك لا يظهر خيار «تابع Gold».");
  } else if (item.entry.status === "completed") {
    lines.push("", "Gold والحزمة النهائية مكتملان.");
  }

  const rows = [];
  if (item.entry.status === "qc_pending") {
    rows.push([{ text: "▶️ تابع Gold", callback_data: `cmd:goldresume-${item.requestId}` }]);
    const runId = String((item.entry.qc_pending || {}).source_run_id || "");
    if (/^[1-9][0-9]*$/.test(runId)) {
      const repo = String(env.GITHUB_REPO || DEFAULT_REPO).trim();
      rows.push([{ text: "📋 Source Run", url: `https://github.com/${repo}/actions/runs/${runId}` }]);
    }
  }
  if (item.entry.status === "completed") {
    const tag = String(item.entry.completed_release_tag || item.entry.release_tag || "").trim();
    if (tag) {
      const repo = String(env.GITHUB_REPO || DEFAULT_REPO).trim();
      rows.push([{ text: "📦 فتح الحزمة", url: `https://github.com/${repo}/releases/tag/${encodeURIComponent(tag)}` }]);
    }
  }
  rows.push([{ text: `↩️ ${label}`, callback_data: `cmd:productions-${item.category}` }]);
  rows.push([{ text: "🏠 الرئيسية", callback_data: "cmd:menu" }]);
  await updatePanel(env, target, lines.join("\n"), rows);
}

async function showSavedPage(env, target, state, kind, requestedPage) {
  const [icon, label] = formatLabel(kind);
  const items = savedItems(state, kind);
  if (!items.length) {
    const searchCallback = kind === "long" ? "cmd:search_menu" : "cmd:short";
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
  if (value === "cmd:search_menu") return { kind: "scope_search", format: "", page: 0 };
  if (value === "cmd:saved") return { kind: "saved_menu", format: "", page: 0 };
  if (value === "cmd:used") return { kind: "used_menu", format: "", page: 0 };
  if (value === "cmd:productions" || value === "cmd:last_delivery") return { kind: "production_menu", format: "", page: 0 };
  let match = /^cmd:saved-(long|short)(?:-page-(\d+))?$/.exec(value);
  if (match) return { kind: "saved_page", format: match[1], page: Number(match[2] || 0) };
  match = /^cmd:used-(long|short)(?:-page-(\d+))?$/.exec(value);
  if (match) return { kind: "used_page", format: match[1], page: Number(match[2] || 0) };
  match = /^cmd:productions-(short|long|bundle)(?:-page-(\d+))?$/.exec(value);
  if (match) return { kind: "production_page", format: match[1], page: Number(match[2] || 0) };
  match = /^cmd:production-item-([A-Za-z0-9_-]{1,40})$/.exec(value);
  if (match) return { kind: "production_item", requestId: match[1], format: "", page: 0 };
  return null;
}

async function handleLibraryRoute(env, target, route) {
  if (route.kind === "scope_search") return showScopeSearch(env, target);
  const state = await controlState(env);
  if (route.kind === "saved_menu") return showSavedMenu(env, target, state);
  if (route.kind === "used_menu") return showUsedMenu(env, target, state);
  if (route.kind === "production_menu") return showProductionMenu(env, target, state);
  if (route.kind === "production_page") return showProductionPage(env, target, state, route.format, route.page);
  if (route.kind === "production_item") return showProductionItem(env, target, state, route.requestId);
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
      const ackText = route.kind === "scope_search"
        ? "🔎 اختر نطاق البحث…"
        : route.kind.startsWith("production_")
          ? "🎞️ أفتح الإنتاجات…"
          : "⚡ أفتح القائمة مباشرة…";
      await ack(env, target.callbackId, ackText);
      try {
        await handleLibraryRoute(env, target, route);
      } catch (error) {
        console.error("Telegram Edge library/search read failed", String((error && error.message) || error || "unknown"));
        await updatePanel(env, target, "⚠️ تعذر فتح هذه القراءة الآن. لم يتغير أي اختيار أو Production Run.", [
          [{ text: "🎞️ الإنتاجات", callback_data: "cmd:productions" }],
          [{ text: "🏠 الرئيسية", callback_data: "cmd:menu" }],
        ]);
      }
    })());
    return new Response("OK");
  },
};