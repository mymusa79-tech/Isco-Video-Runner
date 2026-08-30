import priorWorker from "./observability-worker-v4-core.js";
import { STATUS_CONTRACT } from "./status-contract.generated.js";

const DEFAULT_REPO = "mymusa79-tech/Isco-Video-Runner";
const DEFAULT_CHANNEL_ID = "UC_fmWGRen6QUQNd4Dj80MgA";
const CONFIRM_TEXT = "تأكيد الإنتاج";
const STATE_TTL_MS = 15_000;
let stateCache = null;
let stateCacheAt = 0;

const ROOT_ROWS = [
  [
    { text: "🔎 البحث", callback_data: "cmd:search_menu" },
    { text: "📚 المواضيع", callback_data: "cmd:library_menu" },
  ],
  [
    { text: "🎁 آخر إنتاج", callback_data: "cmd:last_delivery" },
    { text: "📈 الإحصائيات", callback_data: "cmd:stats_menu" },
  ],
  [
    { text: "🧭 الحالة", callback_data: "cmd:status" },
    { text: "🔄 تحديث الكل", callback_data: "cmd:refresh_all" },
  ],
];

const SEARCH_ROWS = [
  [
    { text: "🎬 حلقة", callback_data: "cmd:topic" },
    { text: "⚡ شورت", callback_data: "cmd:short" },
  ],
  [{ text: "↩️ الرئيسية", callback_data: "cmd:menu" }],
];

const STATS_ROWS = [
  [{ text: "🌐 نظرة عامة", callback_data: "cmd:stats_overview" }],
  [
    { text: "🎬 آخر فيديو", callback_data: "cmd:stats_last_long" },
    { text: "⚡ آخر Short", callback_data: "cmd:stats_last_short" },
  ],
  [
    { text: "🗓️ اليوم", callback_data: "cmd:stats_today" },
    { text: "📅 7 أيام", callback_data: "cmd:stats_week" },
  ],
  [{ text: "↩️ الرئيسية", callback_data: "cmd:menu" }],
];

function inline(rows) {
  return { inline_keyboard: rows };
}

function rootText() {
  return [
    "🏠 نداء اليقظة — مركز التحكم",
    "",
    "اختر ما تريد إنجازه الآن:",
    "🔎 فرص جديدة · 📚 مكتبة المواضيع · 🎁 التسليم",
    "📈 أداء القناة · 🧭 ما يحدث الآن",
    "",
    "🔐 أزرار القراءة والاختيار لا تبدأ Production.",
  ].join("\n");
}

function searchText() {
  return [
    "🔎 بحث جديد",
    "",
    "اختر النوع. سأبحث عن 3 فرص حية مرتبة وأعرض سبب قوة كل واحدة.",
    "",
    "هذا بحث فقط؛ لا يبدأ Production.",
  ].join("\n");
}

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
  if (!response.ok || !body.ok) throw new Error(`Telegram ${method} failed: ${String(body.description || response.status)}`);
  return body.result;
}

async function ack(env, id, text = "") {
  if (!id) return;
  try {
    await telegram(env, "answerCallbackQuery", { callback_query_id: id, ...(text ? { text } : {}) });
  } catch (_) {
    // Callback toasts are UX only and never own a state change.
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
      if (String((error && error.message) || "").toLowerCase().includes("message is not modified")) return null;
    }
  }
  return telegram(env, "sendMessage", {
    chat_id: target.chatId,
    text,
    disable_web_page_preview: true,
    reply_markup: inline(rows),
  });
}

function githubHeaders(env, authenticated = true) {
  const headers = {
    accept: "application/vnd.github+json",
    "user-agent": "isco-telegram-control-v5",
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
    if (conclusion === "success") return { label: terminal.label, detail: "اكتمل Workflow بنجاح.", progress: 100 };
    const [job, step] = failedLocation(jobs);
    return {
      label: String(terminal.label || conclusion),
      detail: step ? `توقف عند: ${step}` : job ? `توقف عند: ${job}` : `الحالة: ${conclusion}`,
      progress: null,
    };
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
  return { label: String(run.status || "الإنتاج الجاري"), detail: "Workflow قيد التنفيذ.", progress };
}

async function decryptState(bytes, passphrase) {
  if (new TextDecoder().decode(bytes.slice(0, 8)) !== "Salted__") throw new Error("Unsupported encrypted state envelope");
  const salt = bytes.slice(8, 16);
  const password = await crypto.subtle.importKey("raw", new TextEncoder().encode(passphrase), "PBKDF2", false, ["deriveBits"]);
  const derived = new Uint8Array(await crypto.subtle.deriveBits({
    name: "PBKDF2", hash: "SHA-256", salt, iterations: 10000,
  }, password, 384));
  const key = await crypto.subtle.importKey("raw", derived.slice(0, 32), { name: "AES-CBC" }, false, ["decrypt"]);
  const plain = await crypto.subtle.decrypt({ name: "AES-CBC", iv: derived.slice(32, 48) }, key, bytes.slice(16));
  const state = JSON.parse(new TextDecoder().decode(plain));
  if (!state || typeof state !== "object" || Array.isArray(state)) throw new Error("Invalid Telegram state");
  return state;
}

async function controlState(env) {
  if (stateCache && Date.now() - stateCacheAt <= STATE_TTL_MS) return stateCache;
  const secret = String(env.STATE_ENCRYPTION_KEY || "").trim();
  if (!secret) throw new Error("STATE_ENCRYPTION_KEY is missing at Edge");
  const repo = String(env.GITHUB_REPO || DEFAULT_REPO).trim();
  const response = await fetch(`https://raw.githubusercontent.com/${repo}/control-plane-state/state/control-panel.json.enc`, {
    headers: { "user-agent": "isco-telegram-control-v5" },
  });
  if (!response.ok) throw new Error(`Encrypted control state read failed: ${response.status}`);
  stateCache = await decryptState(new Uint8Array(await response.arrayBuffer()), secret);
  stateCacheAt = Date.now();
  return stateCache;
}

function savedItems(state) {
  return (Array.isArray(state && state.saved_suggestions) ? state.saved_suggestions : [])
    .filter((item) => item && item.status === "available" && item.candidate && String(item.candidate.title || "").trim());
}

function usedItems(state) {
  return (Array.isArray(state && state.used_topics) ? state.used_topics : [])
    .filter((item) => item && ["long", "short"].includes(String(item.kind || "")) && String(item.topic || "").trim());
}

async function showLibraryOverview(env, target) {
  const state = await controlState(env);
  const saved = savedItems(state);
  const used = usedItems(state);
  const savedLong = saved.filter((item) => String(item.kind || "") === "long").length;
  const savedShort = saved.filter((item) => String(item.kind || "") === "short").length;
  const usedLong = used.filter((item) => String(item.kind || "") === "long").length;
  const usedShort = used.filter((item) => String(item.kind || "") === "short").length;
  const text = [
    "📚 مكتبة المواضيع",
    "",
    `📥 محفوظة: ${saved.length} · 🎬 ${savedLong} حلقات · ⚡ ${savedShort} Shorts`,
    `✅ مستعملة: ${used.length} · 🎬 ${usedLong} حلقات · ⚡ ${usedShort} Shorts`,
    "",
    "كل نوع يبقى في قائمته المستقلة.",
  ].join("\n");
  await updatePanel(env, target, text, [
    [
      { text: `📥 المحفوظة (${saved.length})`, callback_data: "cmd:saved" },
      { text: `✅ المستعملة (${used.length})`, callback_data: "cmd:used" },
    ],
    [{ text: "↩️ الرئيسية", callback_data: "cmd:menu" }],
  ]);
}

function currentTarget(state) {
  const target = state && state.production_target;
  if (!target || typeof target !== "object") return null;
  const requestId = String(target.request_id || "").trim();
  const sessionId = String(target.session_id || "").trim();
  if (!requestId || !sessionId || String(state.active_research_session_id || "") !== sessionId) return null;
  return { requestId, sessionId };
}

async function showOperatorStatus(env, target) {
  const [productionResult, stateResult] = await Promise.allSettled([productionState(env), controlState(env)]);
  const production = productionResult.status === "fulfilled" ? productionResult.value : { run: null, jobs: [] };
  const state = stateResult.status === "fulfilled" ? stateResult.value : null;
  const activeRun = production.run && String(production.run.status || "") !== "completed";
  let now = "🟢 لا توجد مهمة معلقة تحتاج تدخلك الآن.";
  let action = "لا يوجد إجراء مطلوب.";

  if (activeRun) {
    const stage = productionStage(production.run, production.jobs);
    now = `🚀 Production جارٍ: ${stage.label}${Number.isFinite(stage.progress) ? ` · ${stage.progress}%` : ""}`;
    action = "لا شيء الآن — لا تكرر التأكيد أثناء التشغيل.";
  } else if (state) {
    const pending = (Array.isArray(state.pending_actions) ? state.pending_actions : []).find((item) => item && item.status === "pending");
    const queued = (Array.isArray(state.production_queue) ? state.production_queue : []).find((item) => item && ["queued", "reserved"].includes(String(item.status || "")));
    const bound = currentTarget(state);
    if (pending) {
      now = `🔎 بحث ${String(pending.kind || "") === "short" ? "الشورت" : "الحلقة"} قيد التنفيذ أو الانتظار.`;
      action = "لا شيء الآن — انتظر ظهور 3 الخيارات.";
    } else if (queued) {
      now = "🚀 طلب Production مؤكد وموجود في مسار الإرسال المحمي.";
      action = "لا شيء الآن — لا تكرر التأكيد.";
    } else if (bound && state.requests && state.requests[bound.requestId]) {
      const request = state.requests[bound.requestId];
      const topic = String(request.approved_topic || "").trim();
      now = `✅ لديك موضوع معتمد ينتظر قرار التشغيل.${topic ? `\n🎯 ${topic.slice(0, 140)}` : ""}`;
      action = `إذا كان القرار نهائيًا، أرسل حرفيًا: ${CONFIRM_TEXT}`;
    }
  } else {
    now = "⚠️ لا يوجد Production Run نشط، لكن تعذر قراءة حالة الاختيار الحالية.";
    action = "لا ترسل تأكيد Production اعتمادًا على هذه الشاشة؛ حدّث الحالة بعد قليل أو افتح التفاصيل.";
  }

  const latest = production.run ? productionStage(production.run, production.jobs) : null;
  const lines = [
    "🧭 الحالة — ماذا يحدث الآن؟",
    "",
    "الآن",
    now,
    "",
    "مطلوب منك",
    action,
    "",
    "آخر تشغيل معروف",
    latest ? `${latest.label}${production.run && production.run.run_number ? ` · Run #${production.run.run_number}` : ""}` : "لا يوجد تشغيل معروف.",
    "",
    "ℹ️ هذه شاشة تشغيلية؛ التفاصيل التقنية خلف زر مستقل.",
  ];
  await updatePanel(env, target, lines.join("\n"), [
    [{ text: "📋 تفاصيل النظام", callback_data: "cmd:system_status" }],
    [{ text: "🔄 تحديث", callback_data: "cmd:status" }, { text: "🏠 الرئيسية", callback_data: "cmd:menu" }],
  ]);
}

async function showSystemStatus(env, target) {
  const value = await productionState(env);
  const stage = productionStage(value.run, value.jobs);
  const lines = [
    `📋 تفاصيل النظام${value.run && value.run.run_number ? ` · Run #${value.run.run_number}` : ""}`,
    "",
    `الحالة: ${stage.label}${Number.isFinite(stage.progress) ? ` · ${stage.progress}%` : ""}`,
    stage.detail,
    "",
    "هذه شاشة تشخيص فقط؛ لا تغيّر Production أو Quality Gates.",
  ];
  const rows = [[{ text: "↩️ الحالة", callback_data: "cmd:status" }]];
  if (value.run && String(value.run.html_url || "").startsWith("https://")) rows.push([{ text: "🔗 GitHub", url: value.run.html_url }]);
  rows.push([{ text: "🏠 الرئيسية", callback_data: "cmd:menu" }]);
  await updatePanel(env, target, lines.join("\n"), rows);
}

async function releases(env) {
  const payload = await githubJson(env, "releases?per_page=30");
  return Array.isArray(payload) ? payload.filter((release) => release && !release.draft) : [];
}

function latestByPrefix(items, prefix) {
  return items
    .filter((item) => String(item.tag_name || "").startsWith(prefix))
    .sort((a, b) => String(b.published_at || b.created_at || "").localeCompare(String(a.published_at || a.created_at || "")))[0] || null;
}

function releaseTitle(release, fallback) {
  const name = String((release && release.name) || "").trim();
  const tag = String((release && release.tag_name) || "").trim();
  return name && name !== tag ? name.slice(0, 140) : fallback;
}

async function showLastDelivery(env, target) {
  const items = await releases(env);
  const longRelease = latestByPrefix(items, "video-");
  const shortRelease = latestByPrefix(items, "short-");
  const lines = ["🎁 آخر إنتاج", ""];
  if (longRelease) {
    lines.push("🎬 آخر حلقة", releaseTitle(longRelease, "حزمة الحلقة الأخيرة"), `✅ جاهزة · ${String(longRelease.published_at || longRelease.created_at || "").slice(0, 10)}`);
  } else {
    lines.push("🎬 آخر حلقة", "لا توجد حزمة حلقة منشورة بعد.");
  }
  lines.push("");
  if (shortRelease) {
    lines.push("⚡ آخر Short", releaseTitle(shortRelease, "حزمة الشورت الأخيرة"), `✅ جاهزة · ${String(shortRelease.published_at || shortRelease.created_at || "").slice(0, 10)}`);
  } else {
    lines.push("⚡ آخر Short", "لا توجد حزمة Short منشورة بعد.");
  }
  lines.push("", "📦 افتح الحزمة فقط عندما تحتاج الملفات أو خيارات النشر.");
  const rows = [];
  if (longRelease && String(longRelease.html_url || "").startsWith("https://")) rows.push([{ text: "🎬 حزمة الحلقة", url: longRelease.html_url }]);
  if (shortRelease && String(shortRelease.html_url || "").startsWith("https://")) rows.push([{ text: "⚡ حزمة الشورت", url: shortRelease.html_url }]);
  if (longRelease) {
    const tag = String(longRelease.tag_name || "");
    if (tag && tag.length <= 35) rows.push([{ text: "🅰️ عناوين وصور A/B/C", callback_data: `pack:${tag}` }]);
  }
  rows.push([{ text: "🔄 تحديث", callback_data: "cmd:last_delivery" }, { text: "🏠 الرئيسية", callback_data: "cmd:menu" }]);
  await updatePanel(env, target, lines.join("\n"), rows);
}

function durationSeconds(value) {
  const match = /^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$/.exec(String(value || ""));
  if (!match) return 0;
  return Number(match[1] || 0) * 86400 + Number(match[2] || 0) * 3600 + Number(match[3] || 0) * 60 + Number(match[4] || 0);
}

async function youtubeJson(env, resource, params) {
  const key = String(env.YOUTUBE_API_KEY || "").trim();
  if (!key) throw new Error("YOUTUBE_API_KEY is missing");
  const query = new URLSearchParams({ ...params, key });
  const response = await fetch(`https://www.googleapis.com/youtube/v3/${resource}?${query.toString()}`);
  if (!response.ok) throw new Error(`YouTube API failed: ${response.status}`);
  return response.json();
}

async function liveYoutube(env) {
  const channelId = String(env.YOUTUBE_CHANNEL_ID || DEFAULT_CHANNEL_ID).trim();
  const channelPayload = await youtubeJson(env, "channels", { part: "snippet,statistics,contentDetails", id: channelId, maxResults: "1" });
  const channel = (channelPayload.items || [])[0];
  if (!channel) throw new Error("YouTube channel not found");
  const uploads = channel.contentDetails && channel.contentDetails.relatedPlaylists && channel.contentDetails.relatedPlaylists.uploads;
  if (!uploads) throw new Error("YouTube uploads playlist unavailable");
  const playlist = await youtubeJson(env, "playlistItems", { part: "contentDetails", playlistId: uploads, maxResults: "25" });
  const ids = (playlist.items || []).map((item) => item.contentDetails && item.contentDetails.videoId).filter(Boolean);
  let videos = [];
  if (ids.length) {
    const payload = await youtubeJson(env, "videos", { part: "snippet,statistics,contentDetails", id: ids.join(","), maxResults: "50" });
    videos = (payload.items || []).map((item) => ({
      id: String(item.id || ""),
      title: String((item.snippet && item.snippet.title) || "").trim(),
      publishedAt: String((item.snippet && item.snippet.publishedAt) || ""),
      duration: durationSeconds(item.contentDetails && item.contentDetails.duration),
      views: Number((item.statistics && item.statistics.viewCount) || 0),
      likes: Number((item.statistics && item.statistics.likeCount) || 0),
      comments: Number((item.statistics && item.statistics.commentCount) || 0),
    })).sort((a, b) => String(b.publishedAt).localeCompare(String(a.publishedAt)));
  }
  const stats = channel.statistics || {};
  return {
    fetchedAt: new Date(),
    channelTitle: String((channel.snippet && channel.snippet.title) || "نداء اليقظة"),
    hiddenSubscribers: Boolean(stats.hiddenSubscriberCount),
    subscribers: Number(stats.subscriberCount || 0),
    views: Number(stats.viewCount || 0),
    videoCount: Number(stats.videoCount || 0),
    videos,
  };
}

function isShort(video) {
  return Number(video && video.duration || 0) > 0 && Number(video.duration) <= 180;
}

function formatNum(value) {
  const n = Math.max(0, Number(value || 0));
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return Math.trunc(n).toLocaleString("en-US");
}

function omanTime(value = new Date()) {
  return new Intl.DateTimeFormat("ar-OM", { timeZone: "Asia/Muscat", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value));
}

function miniContent(label, item) {
  if (!item) return `${label}\nلا يوجد عنصر حديث مناسب.`;
  return `${label}\n${String(item.title || "").slice(0, 100)}\n👁️ ${formatNum(item.views)} · 👍 ${formatNum(item.likes)} · 💬 ${formatNum(item.comments)}`;
}

function visibleEngagement(item) {
  const views = Number(item && item.views || 0);
  if (views <= 0) return null;
  return (100 * (Number(item.likes || 0) + Number(item.comments || 0)) / views).toFixed(1);
}

function periodStartUtc(days, now = new Date()) {
  const shifted = new Date(now.getTime() + 4 * 3600 * 1000);
  return new Date(Date.UTC(shifted.getUTCFullYear(), shifted.getUTCMonth(), shifted.getUTCDate() - Math.max(0, days - 1), 0, 0, 0) - 4 * 3600 * 1000);
}

async function showStats(env, target, kind) {
  const live = await liveYoutube(env);
  const latestLong = live.videos.find((video) => !isShort(video));
  const latestShort = live.videos.find((video) => isShort(video));
  const rows = [...STATS_ROWS];

  if (kind === "stats_overview" || kind === "stats_menu") {
    const subs = live.hiddenSubscribers ? "مخفية" : formatNum(live.subscribers);
    const text = [
      `📊 ${live.channelTitle || "نداء اليقظة"} — نظرة سريعة`,
      "",
      "القناة الآن",
      `👥 ${subs} مشترك`,
      `👁️ ${formatNum(live.views)} مشاهدة إجمالية`,
      `🎞️ ${formatNum(live.videoCount)} منشورًا`,
      "",
      miniContent("🎬 آخر فيديو", latestLong),
      "",
      miniContent("⚡ آخر Short", latestShort),
      "",
      `🔄 تحديث: ${omanTime(live.fetchedAt)} بتوقيت عُمان`,
      "↗️ CTR والاحتفاظ ومدة المشاهدة والمقارنة بالأداء المعتاد: YouTube Studio.",
    ].join("\n");
    await updatePanel(env, target, text, rows);
    return;
  }

  if (kind === "stats_last_long" || kind === "stats_last_short") {
    const wantShort = kind === "stats_last_short";
    const item = wantShort ? latestShort : latestLong;
    if (!item) {
      await updatePanel(env, target, `📈 آخر ${wantShort ? "Short" : "فيديو طويل"}\n\nلم أجد عنصرًا حديثًا مناسبًا.`, rows);
      return;
    }
    const engagement = visibleEngagement(item);
    const published = item.publishedAt ? new Intl.DateTimeFormat("ar-OM", { timeZone: "Asia/Muscat", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(item.publishedAt)) : "غير معروف";
    const lines = [
      `${wantShort ? "⚡" : "🎬"} آخر ${wantShort ? "Short" : "فيديو طويل"}`,
      "",
      String(item.title || "").slice(0, 140),
      "",
      `👁️ ${formatNum(item.views)} مشاهدة`,
      `👍 ${formatNum(item.likes)} إعجاب · 💬 ${formatNum(item.comments)} تعليق`,
    ];
    if (engagement !== null) lines.push(`💬 تفاعل ظاهر: ${engagement}%  (إعجاب + تعليق ÷ مشاهدة)`);
    lines.push(`🕒 نُشر: ${published} بتوقيت عُمان`, "", `🔄 آخر تحديث: ${omanTime(live.fetchedAt)}`);
    if (wantShort) lines.push("ℹ️ تصنيف Short هنا تقريبي بالمدة (≤3 دقائق).");
    lines.push("↗️ CTR والاحتفاظ ومدة المشاهدة التفصيلية تبقى في YouTube Studio.");
    const contentRows = [[{ text: "▶️ فتح على YouTube", url: `https://youtu.be/${item.id}` }], ...rows];
    await updatePanel(env, target, lines.join("\n"), contentRows);
    return;
  }

  const days = kind === "stats_today" ? 1 : 7;
  const start = periodStartUtc(days, live.fetchedAt);
  const items = live.videos.filter((video) => new Date(video.publishedAt) >= start);
  const shorts = items.filter(isShort).length;
  const longs = items.length - shorts;
  const currentViews = items.reduce((sum, item) => sum + Number(item.views || 0), 0);
  const label = days === 1 ? "اليوم" : "آخر 7 أيام";
  const text = [
    `📈 ${label} — المحتوى المنشور`,
    "",
    `🎬 فيديو طويل: ${longs}`,
    `⚡ Shorts تقريبًا: ${shorts}`,
    `🆕 الإجمالي: ${items.length}`,
    `👁️ المشاهدات الحالية لهذه الرفعات: ${formatNum(currentViews)}`,
    "",
    `🔄 تحديث: ${omanTime(live.fetchedAt)} بتوقيت عُمان`,
    "ℹ️ هذه لا تدّعي عدد المشاهدات المكتسبة داخل الفترة؛ النمو الدقيق وCTR/Retention في YouTube Studio.",
  ].join("\n");
  await updatePanel(env, target, text, rows);
}

function callbackRoute(data) {
  if (data === "cmd:menu") return { kind: "menu" };
  if (data === "cmd:search_menu") return { kind: "search" };
  if (data === "cmd:library_menu") return { kind: "library" };
  if (data === "cmd:stats_menu" || ["cmd:stats_overview", "cmd:stats_last_long", "cmd:stats_last_short", "cmd:stats_today", "cmd:stats_week"].includes(data)) {
    return { kind: "stats", data: data.slice(4) };
  }
  if (data === "cmd:status") return { kind: "status" };
  if (data === "cmd:system_status") return { kind: "system_status" };
  if (data === "cmd:last_delivery") return { kind: "delivery" };
  return null;
}

function textRoute(text) {
  const value = String(text || "").trim();
  if (["🏠 ابدأ", "🎛 ابدأ", "/start", "/menu", "ابدأ", "القائمة"].includes(value)) return { kind: "menu" };
  if (["بحث", "1", "١"].includes(value)) return { kind: "search" };
  if (["المواضيع", "2", "٢"].includes(value)) return { kind: "library" };
  if (["آخر إنتاج", "اخر انتاج", "3", "٣"].includes(value)) return { kind: "delivery" };
  if (["الحالة", "حالة", "status", "4", "٤"].includes(value)) return { kind: "status" };
  if (["الإحصائيات", "الاحصائيات", "5", "٥"].includes(value)) return { kind: "stats", data: "stats_menu" };
  return null;
}

async function handleRoute(env, target, route) {
  if (route.kind === "menu") return updatePanel(env, target, rootText(), ROOT_ROWS);
  if (route.kind === "search") return updatePanel(env, target, searchText(), SEARCH_ROWS);
  if (route.kind === "library") return showLibraryOverview(env, target);
  if (route.kind === "stats") return showStats(env, target, route.data);
  if (route.kind === "status") return showOperatorStatus(env, target);
  if (route.kind === "system_status") return showSystemStatus(env, target);
  return showLastDelivery(env, target);
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
    const route = update.callback_query ? callbackRoute(target.data) : textRoute(update.message && update.message.text);
    if (!route) return priorWorker.fetch(request, env, ctx);
    ctx.waitUntil((async () => {
      const toast = route.kind === "stats" ? "أحدّث الأرقام الآن…" : route.kind === "library" ? "أفتح المكتبة الآن…" : "أحدّث اللوحة الآن…";
      await ack(env, target.callbackId, toast);
      try {
        await handleRoute(env, target, route);
      } catch (error) {
        console.error("Creator Control Center V5 read failed", String((error && error.message) || error || "unknown"));
        await updatePanel(env, target, "⚠️ تعذر تحديث هذه القراءة الآن. لم يبدأ ولم يتغير أي Production Run.", ROOT_ROWS);
      }
    })());
    return new Response("OK");
  },
};
