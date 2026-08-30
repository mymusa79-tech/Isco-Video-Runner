import baseWorker from "./index.js";
import { STATUS_CONTRACT } from "./status-contract.generated.js";

const DEFAULT_REPO = "mymusa79-tech/Isco-Video-Runner";
const DEFAULT_CHANNEL_ID = "UC_fmWGRen6QUQNd4Dj80MgA";
const PAGE_SIZE = 5;
const STATE_TTL_MS = 15_000;
let stateCache = null;
let stateCacheAt = 0;

const ROOT_ROWS = [
  [{ text: "🔄 تحديث الكل", callback_data: "cmd:refresh_all" }],
  [{ text: "1️⃣ 🔎 البحث", callback_data: "cmd:search_menu" }],
  [{ text: "2️⃣ 📚 المواضيع", callback_data: "cmd:library_menu" }],
  [{ text: "3️⃣ 🎁 آخر إنتاج", callback_data: "cmd:last_delivery" }],
  [{ text: "4️⃣ 📊 الحالة", callback_data: "cmd:status" }],
  [{ text: "5️⃣ 📈 الإحصائيات", callback_data: "cmd:stats_menu" }],
];
const SEARCH_ROWS = [
  [{ text: "🎬 بحث حلقة", callback_data: "cmd:topic" }],
  [{ text: "⚡ بحث شورت", callback_data: "cmd:short" }],
  [{ text: "↩️ الرئيسية", callback_data: "cmd:menu" }],
];
const LIBRARY_ROWS = [
  [{ text: "📚 المحفوظة", callback_data: "cmd:saved" }],
  [{ text: "✅ المستعملة", callback_data: "cmd:used" }],
  [{ text: "↩️ الرئيسية", callback_data: "cmd:menu" }],
];
const STATS_ROWS = [
  [{ text: "🎬 آخر فيديو", callback_data: "cmd:stats_last_long" }],
  [{ text: "⚡ آخر Short", callback_data: "cmd:stats_last_short" }],
  [{ text: "🗓️ اليوم", callback_data: "cmd:stats_today" }],
  [{ text: "📅 آخر 7 أيام", callback_data: "cmd:stats_week" }],
  [{ text: "🌐 عامة", callback_data: "cmd:stats_overview" }],
  [{ text: "↩️ الرئيسية", callback_data: "cmd:menu" }],
];

function inline(rows) {
  return { inline_keyboard: rows };
}

function omanTime(value = new Date()) {
  return new Intl.DateTimeFormat("ar-OM", {
    timeZone: "Asia/Muscat",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function formatNum(value) {
  const n = Math.max(0, Number(value || 0));
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return Math.trunc(n).toLocaleString("en-US");
}

function actor(update) {
  const callback = update && update.callback_query;
  if (callback) {
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
  if (!response.ok || !body.ok) {
    throw new Error(`Telegram ${method} failed: ${String(body.description || response.status)}`);
  }
  return body.result;
}

async function ack(env, id, text = "") {
  if (!id) return;
  try {
    await telegram(env, "answerCallbackQuery", {
      callback_query_id: id,
      ...(text ? { text } : {}),
    });
  } catch (_) {
    // A callback toast is UX only. Never convert an expired callback into a side effect.
  }
}

async function updatePanel(env, target, text, rows) {
  if (target.callbackId && target.messageId) {
    try {
      return await telegram(env, "editMessageText", {
        chat_id: target.chatId,
        message_id: target.messageId,
        text,
        disable_web_page_preview: true,
        reply_markup: inline(rows),
      });
    } catch (error) {
      if (String((error && error.message) || "").toLowerCase().includes("message is not modified")) {
        return null;
      }
      // If Telegram no longer permits editing an old message, keep one bounded send fallback.
    }
  }
  return telegram(env, "sendMessage", {
    chat_id: target.chatId,
    text,
    disable_web_page_preview: true,
    reply_markup: inline(rows),
  });
}

function rootText() {
  return [
    "🏠 نداء اليقظة",
    "",
    "1️⃣ 🔎 البحث",
    "2️⃣ 📚 المواضيع",
    "3️⃣ 🎁 آخر إنتاج",
    "4️⃣ 📊 الحالة",
    "5️⃣ 📈 الإحصائيات",
    "",
    "⚡ التنقل والقراءة السريعة تعمل مباشرة من Edge.",
    "🔐 لا يبدأ Production من أزرار القراءة.",
  ].join("\n");
}

function menuRoute(data) {
  if (data === "cmd:menu") return [rootText(), ROOT_ROWS];
  if (data === "cmd:search_menu") {
    return ["🔎 البحث\n\nاختر نوع البحث. هذا بحث فقط ولا يبدأ Production.", SEARCH_ROWS];
  }
  if (data === "cmd:library_menu") {
    return ["📚 المواضيع\n\nاختر المحفوظة أو المستعملة.", LIBRARY_ROWS];
  }
  if (data === "cmd:stats_menu") {
    return ["📈 الإحصائيات\n\nاختر القراءة التي تريدها من YouTube.", STATS_ROWS];
  }
  return null;
}

function githubHeaders(env, authenticated = true) {
  const headers = {
    accept: "application/vnd.github+json",
    "user-agent": "isco-telegram-edge-v2",
    "x-github-api-version": "2022-11-28",
  };
  if (authenticated) {
    const token = String(env.GITHUB_CONTROL_TOKEN || "").trim();
    if (token) headers.authorization = `Bearer ${token}`;
  }
  return headers;
}

async function githubJson(env, suffix) {
  const repo = String(env.GITHUB_REPO || DEFAULT_REPO).trim();
  const url = `https://api.github.com/repos/${repo}/${String(suffix).replace(/^\/+/, "")}`;
  let response = await fetch(url, { headers: githubHeaders(env, true) });
  if (!response.ok && [401, 403, 404].includes(response.status)) {
    response = await fetch(url, { headers: githubHeaders(env, false) });
  }
  if (!response.ok) throw new Error(`GitHub read failed: ${response.status}`);
  return response.json();
}

function isProductionRun(run) {
  const name = String((run && run.name) || "");
  return name === "Telegram Explicit Production Request" || name.startsWith("Produce Resilient");
}

async function productionState(env) {
  const payload = await githubJson(env, "actions/runs?per_page=50");
  const runs = (Array.isArray(payload.workflow_runs) ? payload.workflow_runs : [])
    .filter(isProductionRun)
    .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  const run = runs.find((item) => String(item.status || "") !== "completed") || runs[0] || null;
  if (!run || !run.id) return { run: null, jobs: [] };
  const jobsPayload = await githubJson(env, `actions/runs/${run.id}/jobs?per_page=100`);
  return { run, jobs: Array.isArray(jobsPayload.jobs) ? jobsPayload.jobs : [] };
}

function failedLocation(jobs) {
  for (const job of jobs || []) {
    if (!["failure", "cancelled", "timed_out"].includes(String(job.conclusion || ""))) continue;
    for (const step of job.steps || []) {
      if (["failure", "cancelled", "timed_out"].includes(String(step.conclusion || ""))) {
        return [String(job.name || ""), String(step.name || "")];
      }
    }
    return [String(job.name || ""), ""];
  }
  return ["", ""];
}

function productionStage(run, jobs) {
  if (!run) return { label: "غير نشط", detail: "لا يوجد Production Run معروف.", progress: null };
  const conclusion = String(run.conclusion || "").toLowerCase();
  const terminal = (STATUS_CONTRACT.run_terminal || {})[conclusion];
  if (terminal) {
    if (conclusion === "success") {
      return { label: terminal.label, detail: "اكتمل Workflow بنجاح.", progress: 100 };
    }
    const [job, step] = failedLocation(jobs);
    const detail = step ? `توقف عند: ${step}` : job ? `توقف عند: ${job}` : `الحالة: ${conclusion}`;
    return { label: terminal.label, detail, progress: null };
  }
  const steps = (jobs || []).flatMap((job) => Array.isArray(job.steps) ? job.steps : []);
  const current = steps.find((step) => String(step.status || "") === "in_progress");
  const progress = steps.length
    ? Math.round((100 * steps.filter((step) => String(step.status || "") === "completed").length) / steps.length)
    : null;
  if (current) {
    const folded = String(current.name || "").toLowerCase();
    const rule = (STATUS_CONTRACT.stage_rules || []).find((item) =>
      (item.contains || []).some((needle) => folded.includes(String(needle).toLowerCase())),
    );
    return {
      label: String((rule && rule.label) || current.name || "الإنتاج الجاري"),
      detail: `الخطوة الحالية: ${String(current.name || "")}`,
      progress,
    };
  }
  return { label: String(run.status || "غير نشط"), detail: "Workflow قيد التنفيذ.", progress };
}

async function showStatus(env, target) {
  const value = await productionState(env);
  const stage = productionStage(value.run, value.jobs);
  const rows = [
    [
      { text: "🔄 تحديث الحالة", callback_data: "cmd:status" },
      { text: "🔄 تحديث الكل", callback_data: "cmd:refresh_all" },
    ],
    [{ text: "🏠 الرئيسية", callback_data: "cmd:menu" }],
  ];
  if (value.run && String(value.run.html_url || "").startsWith("https://")) {
    rows.splice(1, 0, [{ text: "🔗 GitHub", url: value.run.html_url }]);
  }
  const title = `📊 حالة الإنتاج${value.run && value.run.run_number ? ` · Run #${value.run.run_number}` : ""}`;
  await updatePanel(
    env,
    target,
    `${title}\n\n${stage.label}${Number.isFinite(stage.progress) ? ` · ${stage.progress}%` : ""}\n${stage.detail}\n\n✅ قراءة مباشرة من GitHub · ${omanTime()} عُمان\nℹ️ قراءة فقط؛ لا تعيد Production.`,
    rows,
  );
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
  if (!state || typeof state !== "object" || Array.isArray(state)) throw new Error("Invalid Telegram state");
  return state;
}

async function controlState(env) {
  if (stateCache && Date.now() - stateCacheAt <= STATE_TTL_MS) return stateCache;
  const secret = String(env.STATE_ENCRYPTION_KEY || "").trim();
  if (!secret) throw new Error("STATE_ENCRYPTION_KEY is missing at Edge");
  const repo = String(env.GITHUB_REPO || DEFAULT_REPO).trim();
  const response = await fetch(
    `https://raw.githubusercontent.com/${repo}/control-plane-state/state/control-panel.json.enc`,
    { headers: { "user-agent": "isco-telegram-edge-v2" } },
  );
  if (!response.ok) throw new Error(`Encrypted control state read failed: ${response.status}`);
  stateCache = await decryptState(new Uint8Array(await response.arrayBuffer()), secret);
  stateCacheAt = Date.now();
  return stateCache;
}

function savedItems(state) {
  return (Array.isArray(state.saved_suggestions) ? state.saved_suggestions : [])
    .filter((item) => item && item.status === "available" && item.candidate && String(item.candidate.title || "").trim())
    .sort((a, b) => String(b.last_seen_at || b.saved_at || "").localeCompare(String(a.last_seen_at || a.saved_at || "")));
}

function usedItems(state) {
  return (Array.isArray(state.used_topics) ? state.used_topics : [])
    .filter((item) => item && ["long", "short"].includes(String(item.kind || "")) && String(item.topic || "").trim())
    .sort((a, b) => String(b.used_at || "").localeCompare(String(a.used_at || "")));
}

function byKind(items, kind) {
  return items.filter((item) => String(item.kind || "") === kind);
}

function libraryKindMenu(state, bucket) {
  const items = bucket === "saved" ? savedItems(state) : usedItems(state);
  const longCount = byKind(items, "long").length;
  const shortCount = byKind(items, "short").length;
  const isSaved = bucket === "saved";
  return {
    text: `${isSaved ? "📚 المحفوظة" : "✅ المستعملة"}\n\n🎬 طويل — ${longCount}\n⚡ شورت — ${shortCount}\n\n⚡ قراءة مباشرة من Edge؛ لا تنتظر GitHub Actions.`,
    rows: [
      [{ text: `🎬 طويل (${longCount})`, callback_data: `cmd:${bucket}-long` }],
      [{ text: `⚡ شورت (${shortCount})`, callback_data: `cmd:${bucket}-short` }],
      [{ text: "↩️ المواضيع", callback_data: "cmd:library_menu" }],
    ],
  };
}

function pageSpec(data) {
  const match = /^cmd:(saved|used)-(long|short)(?:-page-(\d+))?$/.exec(String(data || ""));
  return match
    ? { bucket: match[1], kind: match[2], page: Math.max(0, Number(match[3] || 0) || 0) }
    : null;
}

function libraryPage(state, spec) {
  const source = spec.bucket === "saved" ? savedItems(state) : usedItems(state);
  const items = byKind(source, spec.kind);
  const icon = spec.kind === "long" ? "🎬" : "⚡";
  const label = spec.kind === "long" ? "طويل" : "شورت";
  const pages = Math.max(1, Math.ceil(items.length / PAGE_SIZE));
  const page = Math.min(spec.page, pages - 1);
  const start = page * PAGE_SIZE;
  const current = items.slice(start, start + PAGE_SIZE);
  const rows = [];
  const lines = [
    `${spec.bucket === "saved" ? "📚 المحفوظة" : "✅ المستعملة"} — ${icon} ${label}`,
    "",
    `${items.length} موضوعًا — صفحة ${page + 1}/${pages}.`,
  ];
  if (!current.length) lines.push("", "لا توجد عناصر حاليًا.");
  current.forEach((item, index) => {
    if (spec.bucket === "saved") {
      const title = String(item.candidate.title || "").trim();
      const shortTitle = title.length <= 42 ? title : `${title.slice(0, 39).trim()}…`;
      rows.push([{
        text: `${icon} ${shortTitle}`,
        callback_data: `cmd:savedpick-${String(item.archive_id || "")}`,
      }]);
    } else {
      lines.push("", `${start + index + 1}) ${icon} ${String(item.topic || "")}`);
      if (item.used_at) lines.push(`   ${String(item.used_at).slice(0, 10)}`);
    }
  });
  const nav = [];
  if (page > 0) {
    nav.push({ text: "⬅️ أحدث", callback_data: `cmd:${spec.bucket}-${spec.kind}-page-${page - 1}` });
  }
  if (page + 1 < pages) {
    nav.push({ text: "أقدم ➡️", callback_data: `cmd:${spec.bucket}-${spec.kind}-page-${page + 1}` });
  }
  if (nav.length) rows.push(nav);
  rows.push([{
    text: spec.bucket === "saved" ? "↩️ المحفوظة" : "↩️ المستعملة",
    callback_data: `cmd:${spec.bucket}`,
  }]);
  lines.push("", "⚡ هذه الصفحة من Edge. اختيار موضوع محفوظ لا يبدأ Production.");
  return { text: lines.join("\n"), rows };
}

async function showLibrary(env, target, data) {
  const state = await controlState(env);
  const panel = data === "cmd:saved"
    ? libraryKindMenu(state, "saved")
    : data === "cmd:used"
      ? libraryKindMenu(state, "used")
      : libraryPage(state, pageSpec(data));
  await updatePanel(env, target, panel.text, panel.rows);
}

async function publicProjection(env) {
  const repo = String(env.GITHUB_REPO || DEFAULT_REPO).trim();
  const response = await fetch(
    `https://raw.githubusercontent.com/${repo}/control-plane-state/state/telegram-status.json`,
    { headers: { "user-agent": "isco-telegram-edge-v2" } },
  );
  if (!response.ok) throw new Error(`Projection read failed: ${response.status}`);
  const value = await response.json();
  if (!value || Number(value.schema_version) !== 1) throw new Error("Unsupported editorial projection");
  return value;
}

async function latestDelivery(env) {
  const releases = await githubJson(env, "releases?per_page=30");
  if (!Array.isArray(releases)) return null;
  return releases
    .filter((release) => {
      if (!release || release.draft) return false;
      const tag = String(release.tag_name || "");
      return tag.startsWith("video-") || tag.startsWith("short-");
    })
    .sort((a, b) => String(b.published_at || b.created_at || "").localeCompare(String(a.published_at || a.created_at || "")))[0] || null;
}

async function youtubeOverview(env) {
  const key = String(env.YOUTUBE_API_KEY || "").trim();
  if (!key) throw new Error("YOUTUBE_API_KEY is missing");
  const channelId = String(env.YOUTUBE_CHANNEL_ID || DEFAULT_CHANNEL_ID).trim();
  const query = new URLSearchParams({ part: "statistics", id: channelId, maxResults: "1", key });
  const response = await fetch(`https://www.googleapis.com/youtube/v3/channels?${query.toString()}`);
  if (!response.ok) throw new Error(`YouTube API failed: ${response.status}`);
  const payload = await response.json();
  const channel = (payload.items || [])[0];
  if (!channel) throw new Error("YouTube channel not found");
  const stats = channel.statistics || {};
  return {
    subscribers: Number(stats.subscriberCount || 0),
    hiddenSubscribers: Boolean(stats.hiddenSubscriberCount),
    views: Number(stats.viewCount || 0),
  };
}

async function showDashboard(env, target) {
  const results = await Promise.allSettled([
    productionState(env),
    publicProjection(env),
    telegram(env, "getWebhookInfo", {}),
    latestDelivery(env),
    youtubeOverview(env),
  ]);
  const [productionResult, projectionResult, hookResult, deliveryResult, youtubeResult] = results;
  const lines = ["🏠 نداء اليقظة · لوحة التشغيل", ""];

  if (productionResult.status === "fulfilled") {
    const production = productionResult.value;
    const stage = productionStage(production.run, production.jobs);
    lines.push(`🎬 الإنتاج: ${stage.label}${Number.isFinite(stage.progress) ? ` · ${stage.progress}%` : ""}`);
  } else {
    lines.push("🎬 الإنتاج: ⚠️ تعذر التحقق الآن");
  }

  if (projectionResult.status === "fulfilled") {
    const editorial = (projectionResult.value && projectionResult.value.editorial) || {};
    lines.push(`📚 التحرير: محفوظة ${Number(editorial.saved_count || 0)} · مستعملة ${Number(editorial.used_count || 0)}`);
  } else {
    lines.push("📚 التحرير: ⚠️ تعذر التحقق الآن");
  }

  if (deliveryResult.status === "fulfilled") {
    const release = deliveryResult.value;
    lines.push(`🎁 آخر حزمة: ${release ? String(release.name || release.tag_name || "الحزمة الأخيرة") : "لا توجد حزمة منشورة"}`);
  } else {
    lines.push("🎁 آخر حزمة: ⚠️ تعذر التحقق الآن");
  }

  if (youtubeResult.status === "fulfilled") {
    const yt = youtubeResult.value;
    const subs = yt.hiddenSubscribers ? "مخفية" : formatNum(yt.subscribers);
    lines.push(`📈 YouTube: ${subs} مشترك · ${formatNum(yt.views)} مشاهدة`);
  } else {
    lines.push("📈 YouTube: ⚠️ تعذر التحقق الآن");
  }

  if (hookResult.status === "fulfilled") {
    const hook = hookResult.value || {};
    const pending = Number(hook.pending_update_count || 0);
    const error = String(hook.last_error_message || "").trim();
    lines.push(`🛰️ Telegram: webhook ✅ · pending ${pending}${error ? " · ⚠️ خطأ مسجل" : ""}`);
  } else {
    lines.push("🛰️ Telegram: ⚠️ تعذر فحص webhook الآن");
  }

  lines.push(
    "",
    `🕒 آخر تحقق شامل: ${omanTime()} · عُمان`,
    "ℹ️ «تحديث الكل» قراءة فقط؛ لا يبدأ ولا يعيد أي Production Run.",
  );
  await updatePanel(env, target, lines.join("\n"), ROOT_ROWS);
}

function isMenuText(text) {
  return ["🏠 ابدأ", "🎛 ابدأ", "/start", "/menu", "ابدأ", "القائمة"].includes(String(text || "").trim());
}

function fastRoute(update) {
  const callback = update && update.callback_query;
  if (callback) {
    const data = String(callback.data || "");
    if (menuRoute(data)) return { kind: "menu", data };
    if (data === "cmd:status") return { kind: "status" };
    if (data === "cmd:refresh_all") return { kind: "dashboard" };
    if (data === "cmd:saved" || data === "cmd:used" || pageSpec(data)) {
      return { kind: "library", data };
    }
    return null;
  }
  return isMenuText(update && update.message && update.message.text)
    ? { kind: "menu", data: "cmd:menu" }
    : null;
}

async function handleFast(route, update, env) {
  const target = actor(update);
  if (route.kind === "menu") {
    await ack(env, target.callbackId);
    const [text, rows] = menuRoute(route.data);
    await updatePanel(env, target, text, rows);
    return;
  }
  if (route.kind === "status") {
    await ack(env, target.callbackId, "أتحقق من GitHub الآن…");
    await showStatus(env, target);
    return;
  }
  if (route.kind === "dashboard") {
    await ack(env, target.callbackId, "أحدّث الصورة الكاملة الآن…");
    await showDashboard(env, target);
    return;
  }
  await ack(env, target.callbackId, "أفتح القائمة مباشرة…");
  await showLibrary(env, target, route.data);
}

async function safeFast(route, update, env) {
  const target = actor(update);
  try {
    await handleFast(route, update, env);
  } catch (error) {
    console.error("Fast Telegram read failed", String((error && error.message) || error || "unknown"));
    try {
      await updatePanel(
        env,
        target,
        `⚠️ تعذر إكمال القراءة السريعة الآن. لم يبدأ ولم يتغير أي Production Run.\n\n🕒 ${omanTime()} · عُمان`,
        ROOT_ROWS,
      );
    } catch (_) {
      // Telegram itself may be unavailable. Do not mutate anything else.
    }
  }
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health") {
      return new Response(JSON.stringify({
        ok: true,
        mode: "telegram-edge-control",
        observability: "v1",
        edge_read_version: 2,
        status_schema: STATUS_CONTRACT.schema_version,
        fast_library: Boolean(String(env.STATE_ENCRYPTION_KEY || "").trim()),
      }), { headers: { "content-type": "application/json" } });
    }

    if (request.method === "POST" && url.pathname === "/telegram" && secretHeaderValid(request, env)) {
      let update;
      try {
        update = await request.clone().json();
      } catch (_) {
        return baseWorker.fetch(request, env, ctx);
      }
      if (update && Number.isInteger(update.update_id) && authorized(update, env)) {
        const route = fastRoute(update);
        if (route) {
          if (route.kind === "library" && !String(env.STATE_ENCRYPTION_KEY || "").trim()) {
            return baseWorker.fetch(request, env, ctx);
          }
          ctx.waitUntil(safeFast(route, update, env));
          return new Response("OK");
        }
      }
    }
    return baseWorker.fetch(request, env, ctx);
  },
};
