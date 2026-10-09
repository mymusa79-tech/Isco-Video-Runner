const DEFAULT_REPO = "mymusa79-tech/Isco-Video-Runner";
const CONTROL_WORKFLOW = "telegram-clean-v2-control.yml";
const CONFIRM_TEXT = "تأكيد الإنتاج";
const STATE_REF = "clean-v2-telegram-state";
const STATE_PATH = "state/telegram-clean-v2.json";
const DELIVERY_TAG_PREFIX = "clean-v2-final-";
const LIBRARY_ORDER = ["long", "short", "podcast"];
const LIBRARY_LABELS = {
  long: ["🎬", "طويل"],
  short: ["⚡", "شورت"],
  podcast: ["🎙️", "بودكاست"],
};
const LIBRARY_CACHE_MS = 5_000;

let libraryCache = { repo: "", expiresAt: 0, value: null, pending: null };

async function telegram(env, method, payload) {
  const token = String(env.TELEGRAM_BOT_TOKEN || "").trim();
  if (!token) throw new Error("TELEGRAM_BOT_TOKEN missing");
  const response = await fetch(`https://api.telegram.org/bot${token}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok || !body.ok) throw new Error(`Telegram ${method} failed`);
  return body.result;
}

function target(update) {
  const callback = update && update.callback_query;
  if (callback) {
    return {
      actor: String((callback.from && callback.from.id) || ""),
      chat: String((callback.message && callback.message.chat && callback.message.chat.id) || ""),
      callbackId: String(callback.id || ""),
      data: String(callback.data || ""),
      messageId: String((callback.message && callback.message.message_id) || ""),
    };
  }
  const message = (update && update.message) || {};
  return {
    actor: String((message.from && message.from.id) || ""),
    chat: String((message.chat && message.chat.id) || ""),
    callbackId: "",
    data: "",
    messageId: "",
  };
}

function authorized(update, env) {
  const expected = String(env.TELEGRAM_CHAT_ID || "").trim();
  const current = target(update);
  return Boolean(expected && current.actor === expected && current.chat === expected);
}

function webhookSecretValid(request, env) {
  const expected = String(env.TELEGRAM_WEBHOOK_SECRET || "").trim();
  const actual = String(request.headers.get("X-Telegram-Bot-Api-Secret-Token") || "");
  return Boolean(expected && actual === expected);
}

function scopeKeyboard() {
  return {
    inline_keyboard: [
      [{ text: "🎬 فيديو طويل", callback_data: "scope:long" }],
      [{ text: "⚡ شورت", callback_data: "scope:short" }],
      [{ text: "🎙️ خارج النص", callback_data: "scope:podcast" }],
      [{ text: "↩️ الرئيسية", callback_data: "main:home" }],
    ],
  };
}

function mainMenuKeyboard() {
  return {
    inline_keyboard: [
      [{ text: "🔎 بحث جديد", callback_data: "main:research" }],
      [
        { text: "📚 المحفوظات", callback_data: "main:saved" },
        { text: "✅ المستعملة", callback_data: "main:used" },
      ],
      [
        { text: "📊 الإحصائيات", callback_data: "main:stats" },
        { text: "🟢 حالة الإنتاج", callback_data: "main:status" },
      ],
      [{ text: "🎥 آخر إنتاج", callback_data: "main:last" }],
      [{ text: "❌ إلغاء الاختيار", callback_data: "main:cancel" }],
    ],
  };
}

function arabicMainKeyboard() {
  return {
    keyboard: [[{ text: "🏠 الرئيسية" }]],
    resize_keyboard: true,
    is_persistent: true,
    input_field_placeholder: "افتح الرئيسية",
  };
}

async function sendWelcome(env, chatId) {
  await telegram(env, "sendMessage", {
    chat_id: chatId,
    text:
      "👋 مرحبًا بك في مساعد نداء اليقظة\n\n" +
      "كل الأدوات أصبحت داخل «🏠 الرئيسية».\n" +
      "افتحها واختر البحث أو المحفوظات أو المستعملة أو المتابعة.\n\n" +
      "الاختيار وحده لا يبدأ أي إنتاج؛ التشغيل يحتاج «تأكيد الإنتاج».",
    reply_markup: arabicMainKeyboard(),
  });

  await telegram(env, "sendMessage", {
    chat_id: chatId,
    text: "🏠 الرئيسية\n\nاختر ما تريد من هنا:",
    reply_markup: mainMenuKeyboard(),
  });
}

async function sendScopeMenu(env, chatId) {
  await telegram(env, "sendMessage", {
    chat_id: chatId,
    text: "🔎 اختر نوع المحتوى الذي تريد البحث له. لن يبدأ الإنتاج قبل تأكيدك النهائي.",
    reply_markup: scopeKeyboard(),
  });
}

async function sendMainMenu(env, chatId) {
  await telegram(env, "sendMessage", {
    chat_id: chatId,
    text: "🏠 الرئيسية\n\nكل الأدوات هنا داخل قائمة واحدة. اختر ما تريد؛ ولا يبدأ الإنتاج إلا بعد «تأكيد الإنتاج».",
    reply_markup: mainMenuKeyboard(),
  });
}

async function answerCallback(env, callbackId, text = "") {
  if (!callbackId) return;
  try {
    await telegram(env, "answerCallbackQuery", {
      callback_query_id: callbackId,
      text,
    });
  } catch (_) {
    // UI acknowledgement is best-effort and carries no authority.
  }
}

function base64Utf8(value) {
  const bytes = new TextEncoder().encode(JSON.stringify(value));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function decodeBase64Utf8(value) {
  const binary = atob(String(value || "").replace(/\s+/g, ""));
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

function githubHeaders(token) {
  return {
    "authorization": `Bearer ${token}`,
    "accept": "application/vnd.github+json",
    "x-github-api-version": "2022-11-28",
    "user-agent": "isco-clean-v2-telegram-lite",
  };
}

async function githubJson(env, path) {
  const token = String(env.GITHUB_CONTROL_TOKEN || "").trim();
  const repo = String(env.GITHUB_REPO || DEFAULT_REPO).trim();
  if (!token) throw new Error("GITHUB_CONTROL_TOKEN missing");
  const response = await fetch(`https://api.github.com/repos/${repo}/${path}`, {
    headers: githubHeaders(token),
  });
  if (!response.ok) throw new Error(`GitHub read failed: ${response.status}`);
  return response.json();
}

function normalizeTitle(value) {
  return String(value || "")
    .toLocaleLowerCase("ar")
    .trim()
    .replace(/[أإآٱ]/g, "ا")
    .replace(/ى/g, "ي")
    .replace(/ؤ/g, "و")
    .replace(/ئ/g, "ي")
    .replace(/[^\p{L}\p{N}_]+/gu, " ")
    .trim()
    .replace(/\s+/g, " ");
}

function topicKey(token) {
  let value = normalizeTitle(token);
  if (value.startsWith("ال") && value.length > 4) value = value.slice(2);
  for (const suffix of ["كما", "هما", "كم", "كن", "هم", "هن", "ها", "نا", "ك", "ه", "ي"]) {
    if (value.endsWith(suffix) && value.length - suffix.length >= 3) {
      value = value.slice(0, -suffix.length);
      break;
    }
  }
  if (["ن", "ي", "ت"].includes(value.slice(0, 1)) && value.length > 4) value = value.slice(1);
  const skeleton = [...value].filter((character) => !["ا", "و", "ي"].includes(character)).join("");
  return skeleton.length >= 2 ? skeleton : value;
}

function sameTopic(left, right) {
  const a = normalizeTitle(left);
  const b = normalizeTitle(right);
  if (!a || !b) return false;
  if (a === b) return true;
  const ta = new Set(a.split(" ").map(topicKey).filter(Boolean));
  const tb = new Set(b.split(" ").map(topicKey).filter(Boolean));
  if (Math.min(ta.size, tb.size) < 3) return false;
  const common = [...ta].filter((token) => tb.has(token)).length;
  const union = new Set([...ta, ...tb]).size;
  return common >= 3 && common / Math.min(ta.size, tb.size) >= 0.75 && common / union >= 0.55;
}

function libraryKindForScope(scope) {
  if (["long", "bundle"].includes(String(scope || ""))) return "long";
  return ["short", "podcast"].includes(String(scope || "")) ? String(scope) : "";
}

function releaseLibraryRecords(payload) {
  const records = [];
  const seen = new Set();
  for (const release of Array.isArray(payload) ? payload : []) {
    if (!release || typeof release !== "object" || release.draft) continue;
    const tag = String(release.tag_name || "");
    if (!tag.startsWith(DELIVERY_TAG_PREFIX)) continue;
    const kind = tag.startsWith(`${DELIVERY_TAG_PREFIX}short-`)
      ? "short"
      : (tag.startsWith(`${DELIVERY_TAG_PREFIX}podcast-`) ? "podcast" : "long");
    const name = String(release.name || "").trim();
    const separator = name.indexOf(" — ");
    const topic = separator >= 0 ? name.slice(separator + 3).trim() : "";
    const key = `${kind}|${normalizeTitle(topic)}`;
    if (!topic || seen.has(key)) continue;
    seen.add(key);
    records.push({
      kind,
      topic,
      used_at: String(release.published_at || release.created_at || ""),
    });
  }
  return records;
}

function savedLibraryItems(state, kind, usedRecords = []) {
  const usedTitles = usedRecords
    .filter((item) => String(item && item.kind || "") === kind)
    .map((item) => String(item.topic || ""));
  const ideas = new Map(
    (Array.isArray(state && state.ideas) ? state.ideas : [])
      .filter((idea) => idea && typeof idea === "object")
      .map((idea) => [String(idea.idea_id || ""), idea]),
  );
  const sessions = Object.values(state && state.sessions && typeof state.sessions === "object" ? state.sessions : {})
    .filter((session) => session && typeof session === "object")
    .filter((session) => String(session.source || "") !== "saved_library")
    .filter((session) => libraryKindForScope(session.scope) === kind)
    .sort((left, right) => String(right.created_at || "").localeCompare(String(left.created_at || "")));
  const result = [];
  const seen = new Set();
  for (const session of sessions) {
    const ids = Array.isArray(session.idea_ids) ? session.idea_ids : [];
    ids.forEach((ideaId, index) => {
      const idea = ideas.get(String(ideaId));
      if (!idea) return;
      const title = String(idea.title || "").trim();
      const normalized = normalizeTitle(title);
      if (!title || !normalized || seen.has(normalized)) return;
      if (usedTitles.some((usedTitle) => sameTopic(title, usedTitle))) return;
      seen.add(normalized);
      result.push({
        idea_id: String(idea.idea_id || ""),
        title,
        scope: String(session.scope || ""),
        rank: index + 1,
      });
    });
  }
  return result;
}

async function loadLibrarySnapshot(env) {
  const repo = String(env.GITHUB_REPO || DEFAULT_REPO).trim();
  const now = Date.now();
  if (libraryCache.repo === repo && libraryCache.value && libraryCache.expiresAt > now) {
    return libraryCache.value;
  }
  if (libraryCache.repo === repo && libraryCache.pending) return libraryCache.pending;
  const encodedPath = STATE_PATH.split("/").map(encodeURIComponent).join("/");
  const pending = Promise.all([
    githubJson(env, `contents/${encodedPath}?ref=${encodeURIComponent(STATE_REF)}`),
    githubJson(env, "releases?per_page=100"),
  ]).then(([stateFile, releases]) => {
    if (!stateFile || typeof stateFile.content !== "string") throw new Error("Telegram state content missing");
    const state = JSON.parse(decodeBase64Utf8(stateFile.content));
    if (!state || state.schema_version !== 1 || !Array.isArray(state.ideas) || !state.sessions) {
      throw new Error("Telegram state malformed");
    }
    return { state, used: releaseLibraryRecords(releases) };
  });
  libraryCache = { repo, expiresAt: 0, value: null, pending };
  try {
    const value = await pending;
    libraryCache = { repo, expiresAt: Date.now() + LIBRARY_CACHE_MS, value, pending: null };
    return value;
  } catch (error) {
    libraryCache = { repo, expiresAt: 0, value: null, pending: null };
    throw error;
  }
}

function localLibraryRoute(data) {
  // main:saved belongs to durable production history. library:saved remains
  // the local, read-only research browser linked from that history screen.
  if (data === "main:used") return { bucket: "used", kind: "" };
  const match = /^library:(saved|used)(?::(long|short|podcast))?$/.exec(String(data || ""));
  return match ? { bucket: match[1], kind: match[2] || "" } : null;
}

function renderLibraryMenu(state, used, bucket) {
  const counts = Object.fromEntries(LIBRARY_ORDER.map((kind) => [
    kind,
    bucket === "saved"
      ? savedLibraryItems(state, kind, used).length
      : used.filter((item) => item.kind === kind).length,
  ]));
  const lines = bucket === "saved"
    ? ["📚 المحفوظات", "", "من نتائج البحث التي عُرضت لك فعليًا؛ الأحدث أولًا."]
    : ["✅ المستعملة", "", "المواضيع التي خرج لها إنتاج ناجح فعليًا."];
  const keyboard = [];
  for (const kind of LIBRARY_ORDER) {
    const [icon, label] = LIBRARY_LABELS[kind];
    lines.push(`${icon} ${label} — ${counts[kind]}`);
    keyboard.push([{ text: `${icon} ${label} (${counts[kind]})`, callback_data: `library:${bucket}:${kind}` }]);
  }
  keyboard.push([{ text: "↩️ الرئيسية", callback_data: "main:home" }]);
  return { text: lines.join("\n"), keyboard };
}

function renderSavedLibraryView(state, used, kind) {
  const [icon, label] = LIBRARY_LABELS[kind];
  const items = savedLibraryItems(state, kind, used);
  const lines = [`📚 المحفوظات — ${icon} ${label}`, ""];
  const keyboard = [];
  if (!items.length) {
    lines.push("لا توجد أفكار محفوظة من البحث لهذا النوع حتى الآن.");
  } else {
    lines.push("الأحدث أولًا، وداخل كل بحث يبقى ترتيب 1 ثم 2 ثم 3.");
    for (const item of items.slice(0, 30)) {
      const prefix = item.rank === 1 ? "1️⃣" : (item.rank === 2 ? "2️⃣" : (item.rank === 3 ? "3️⃣" : "•"));
      const shortTitle = item.title.length <= 42 ? item.title : `${item.title.slice(0, 39).trimEnd()}…`;
      keyboard.push([{
        text: `${prefix} ${shortTitle}`,
        callback_data: `savedpick:${item.scope}:${item.idea_id}`,
      }]);
    }
    if (items.length > 30) lines.push(`\n+ ${items.length - 30} أقدم محفوظة غير معروضة هنا.`);
  }
  keyboard.push([{ text: "↩️ المحفوظات", callback_data: "library:saved" }]);
  return { text: lines.join("\n"), keyboard };
}

function renderUsedLibraryView(used, kind) {
  const [icon, label] = LIBRARY_LABELS[kind];
  const items = used.filter((item) => item.kind === kind);
  const lines = [`✅ المستعملة — ${icon} ${label}`, ""];
  if (!items.length) {
    lines.push("لا يوجد إنتاج ناجح لهذا النوع حتى الآن.");
  } else {
    items.slice(0, 30).forEach((item, index) => {
      const date = String(item.used_at || "").slice(0, 10);
      lines.push(`${index + 1}) ${item.topic}${date ? ` — ${date}` : ""}`);
    });
    if (items.length > 30) lines.push(`\n+ ${items.length - 30} أقدم.`);
  }
  return {
    text: lines.join("\n"),
    keyboard: [[{ text: "↩️ المستعملة", callback_data: "library:used" }]],
  };
}

async function sendLocalLibrary(env, chatId, route) {
  const { state, used } = await loadLibrarySnapshot(env);
  const view = !route.kind
    ? renderLibraryMenu(state, used, route.bucket)
    : (route.bucket === "saved"
      ? renderSavedLibraryView(state, used, route.kind)
      : renderUsedLibraryView(used, route.kind));
  await telegram(env, "sendMessage", {
    chat_id: chatId,
    text: view.text,
    reply_markup: { inline_keyboard: view.keyboard },
  });
}

async function dispatchControl(env, update) {
  const token = String(env.GITHUB_CONTROL_TOKEN || "").trim();
  const repo = String(env.GITHUB_REPO || DEFAULT_REPO).trim();
  if (!token) throw new Error("GITHUB_CONTROL_TOKEN missing");
  const response = await fetch(
    `https://api.github.com/repos/${repo}/actions/workflows/${CONTROL_WORKFLOW}/dispatches`,
    {
      method: "POST",
      headers: {
        "authorization": `Bearer ${token}`,
        "accept": "application/vnd.github+json",
        "x-github-api-version": "2022-11-28",
        "content-type": "application/json",
        "user-agent": "isco-clean-v2-telegram-lite",
      },
      body: JSON.stringify({
        ref: "main",
        inputs: { webhook_update_b64: base64Utf8(update) },
      }),
    },
  );
  if (response.status !== 204) {
    throw new Error(`GitHub workflow dispatch failed: ${response.status}`);
  }
}

function isStartText(text) {
  return ["/start", "start", "/menu", "menu", "ابدأ", "ابدأ البوت", "🏠 الرئيسية"].includes(String(text || "").trim());
}

function isResearchText(text) {
  return ["/research", "research", "بحث", "🔎 بحث جديد"].includes(String(text || "").trim());
}

function isCancelText(text) {
  return ["/cancel", "cancel", "إلغاء", "الغاء", "❌ إلغاء الاختيار"].includes(String(text || "").trim());
}

function isLastText(text) {
  return ["/last", "last", "آخر إنتاج", "اخر انتاج", "🎥 آخر إنتاج"].includes(String(text || "").trim());
}

function isStatusText(text) {
  return ["/status", "status", "الحالة", "حالة الإنتاج", "حاله الانتاج", "🟢 حالة الإنتاج"].includes(String(text || "").trim());
}

function isStatsText(text) {
  return ["/stats", "stats", "إحصائيات", "الاحصائيات", "الإحصائيات", "📊 الإحصائيات"].includes(String(text || "").trim());
}

function isSavedText(text) {
  return ["/saved", "saved", "محفوظات", "المحفوظات", "📚 المحفوظات"].includes(String(text || "").trim());
}

function isUsedText(text) {
  return ["/used", "used", "مستعملة", "المستعملة", "✅ المستعملة"].includes(String(text || "").trim());
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health") {
      return new Response(
        JSON.stringify({ ok: true, mode: "clean-v2-telegram-lite", phase: "C" }),
        { headers: { "content-type": "application/json" } },
      );
    }
    if (request.method !== "POST" || url.pathname !== "/telegram") {
      return new Response("Not found", { status: 404 });
    }
    if (!webhookSecretValid(request, env)) {
      return new Response("Forbidden", { status: 403 });
    }

    let update;
    try {
      update = await request.json();
    } catch (_) {
      return new Response("Bad JSON", { status: 400 });
    }
    if (!update || !Number.isInteger(update.update_id)) {
      return new Response("Bad update", { status: 400 });
    }
    const current = target(update);
    if (!authorized(update, env)) {
      if (current.callbackId) ctx.waitUntil(answerCallback(env, current.callbackId, "غير مصرح"));
      return new Response("OK");
    }

    if (update.callback_query) {
      if (current.data === "main:home") {
        ctx.waitUntil(answerCallback(env, current.callbackId));
        ctx.waitUntil(sendMainMenu(env, current.chat));
        return new Response("OK");
      }
      if (current.data === "main:research") {
        ctx.waitUntil(answerCallback(env, current.callbackId));
        ctx.waitUntil(sendScopeMenu(env, current.chat));
        return new Response("OK");
      }
      const libraryRoute = localLibraryRoute(current.data);
      if (libraryRoute) {
        ctx.waitUntil(answerCallback(env, current.callbackId, "📚 أفتح القائمة…"));
        ctx.waitUntil(
          sendLocalLibrary(env, current.chat, libraryRoute).catch(() =>
            telegram(env, "sendMessage", {
              chat_id: current.chat,
              text: "⚠️ تعذر فتح القائمة الآن. لم يبدأ أي إنتاج.",
            }),
          ),
        );
        return new Response("OK");
      }
      if (current.data.startsWith("scope:")) {
        ctx.waitUntil(answerCallback(env, current.callbackId, "🔎 بدأ البحث…"));
      } else if (current.data.startsWith("pick:") || current.data.startsWith("savedpick:")) {
        ctx.waitUntil(answerCallback(
          env,
          current.callbackId,
          "⏳ أسجل الاختيار؛ انتظر رسالة «تم اختيار الفكرة» قبل التأكيد.",
        ));
      } else if (current.data.startsWith("confirm:")) {
        ctx.waitUntil(answerCallback(env, current.callbackId, "⏳ أتحقق من التأكيد…"));
      } else if (/^(historyscope|history|resumevoice|resume|restart):/.test(current.data)) {
        ctx.waitUntil(answerCallback(env, current.callbackId, "⏳ أتحقق من المحاولة المحفوظة…"));
      } else if (current.data === "main:saved") {
        ctx.waitUntil(answerCallback(env, current.callbackId, "📚 أفتح سجل المحاولات…"));
      } else if (current.data.startsWith("main:")) {
        ctx.waitUntil(answerCallback(env, current.callbackId));
      } else {
        ctx.waitUntil(answerCallback(env, current.callbackId, "أمر غير معروف"));
        return new Response("OK");
      }
      ctx.waitUntil(
        dispatchControl(env, update).catch(() =>
          telegram(env, "sendMessage", {
            chat_id: current.chat,
            text: "⚠️ تعذر تمرير الأمر إلى GitHub الآن. لم يبدأ أي Production.",
          }),
        ),
      );
      return new Response("OK");
    }

    const text = String((update.message && update.message.text) || "").trim();
    if (isStartText(text)) {
      ctx.waitUntil(sendWelcome(env, current.chat));
      return new Response("OK");
    }
    if (isResearchText(text)) {
      ctx.waitUntil(sendScopeMenu(env, current.chat));
      return new Response("OK");
    }
    if (isSavedText(text)) {
      ctx.waitUntil(
        dispatchControl(env, update).catch(() =>
          telegram(env, "sendMessage", {
            chat_id: current.chat,
            text: "⚠️ تعذر فتح سجل المحاولات الآن. لم يبدأ أي إنتاج.",
          }),
        ),
      );
      return new Response("OK");
    }
    if (isUsedText(text)) {
      const route = { bucket: "used", kind: "" };
      ctx.waitUntil(
        sendLocalLibrary(env, current.chat, route).catch(() =>
          telegram(env, "sendMessage", {
            chat_id: current.chat,
            text: "⚠️ تعذر فتح القائمة الآن. لم يبدأ أي إنتاج.",
          }),
        ),
      );
      return new Response("OK");
    }
    if (isCancelText(text)) {
      ctx.waitUntil(
        dispatchControl(env, update).catch(() =>
          telegram(env, "sendMessage", {
            chat_id: current.chat,
            text: "⚠️ تعذر تمرير طلب الإلغاء الآن. لم يبدأ أي إنتاج جديد.",
          }),
        ),
      );
      return new Response("OK");
    }
    if (isLastText(text)) {
      ctx.waitUntil(
        dispatchControl(env, update).catch(() =>
          telegram(env, "sendMessage", {
            chat_id: current.chat,
            text: "⚠️ تعذر قراءة آخر إنتاج ناجح الآن.",
          }),
        ),
      );
      return new Response("OK");
    }
    if (isStatusText(text)) {
      ctx.waitUntil(
        dispatchControl(env, update).catch(() =>
          telegram(env, "sendMessage", {
            chat_id: current.chat,
            text: "⚠️ تعذر قراءة حالة الإنتاج الآن.",
          }),
        ),
      );
      return new Response("OK");
    }
    if (isStatsText(text)) {
      ctx.waitUntil(
        dispatchControl(env, update).catch(() =>
          telegram(env, "sendMessage", {
            chat_id: current.chat,
            text: "⚠️ تعذر تحديث إحصائيات YouTube الآن. لم يتأثر البحث أو الإنتاج.",
          }),
        ),
      );
      return new Response("OK");
    }
    if (text === CONFIRM_TEXT) {
      ctx.waitUntil(
        dispatchControl(env, update).catch(() =>
          telegram(env, "sendMessage", {
            chat_id: current.chat,
            text: "⚠️ تعذر تمرير تأكيد الإنتاج. لم يبدأ أي Production.",
          }),
        ),
      );
      return new Response("OK");
    }

    ctx.waitUntil(
      telegram(env, "sendMessage", {
        chat_id: current.chat,
        text: "استخدم /research للبحث، /saved للمحفوظات، /used للمستعملة، و/stats للإحصائيات. بدء الإنتاج يتطلب تأكيدًا منفصلًا من زر الطلب أو العبارة الدقيقة «تأكيد الإنتاج».",
      }),
    );
    return new Response("OK");
  },
};

export {
  localLibraryRoute,
  normalizeTitle,
  releaseLibraryRecords,
  renderLibraryMenu,
  renderSavedLibraryView,
  renderUsedLibraryView,
  sameTopic,
  savedLibraryItems,
  scopeKeyboard,
};
