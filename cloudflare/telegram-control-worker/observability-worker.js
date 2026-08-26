import baseWorker from "./index.js";
import { STATUS_CONTRACT } from "./status-contract.generated.js";

const DEFAULT_REPO = "mymusa79-tech/Isco-Video-Runner";
const DEFAULT_CHANNEL_ID = "UC_fmWGRen6QUQNd4Dj80MgA";
const SOURCE_CACHE = new Map();

const ROOT_ROWS = [
  [{ text: "🔄 تحديث الكل", callback_data: "cmd:refresh_all" }],
  [{ text: "1️⃣ 🔎 البحث", callback_data: "cmd:search_menu" }],
  [{ text: "2️⃣ 📚 المواضيع", callback_data: "cmd:library_menu" }],
  [{ text: "3️⃣ 🎁 آخر إنتاج", callback_data: "cmd:last_delivery" }],
  [{ text: "4️⃣ 📊 الحالة", callback_data: "cmd:status" }],
  [{ text: "5️⃣ 📈 الإحصائيات", callback_data: "cmd:stats_menu" }],
];

const STATS_LEAVES = new Set([
  "cmd:stats_last_long",
  "cmd:stats_last_short",
  "cmd:stats_today",
  "cmd:stats_week",
  "cmd:stats_overview",
]);

function inline(rows) {
  return { inline_keyboard: rows };
}

function actorAndChat(update) {
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
  const target = actorAndChat(update);
  const allowedUser = String(env.TELEGRAM_ALLOWED_USER_ID || "").trim();
  const allowedChat = String(env.TELEGRAM_CHAT_ID || "").trim();
  if (!allowedUser || !allowedChat) return false;
  return String(target.userId ?? "") === allowedUser && String(target.chatId ?? "") === allowedChat;
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
    const description = String(body.description || response.status || "unknown");
    throw new Error(`Telegram ${method} failed: ${description}`);
  }
  return body.result;
}

async function answerCallback(env, id, text = "") {
  if (!id) return;
  const payload = { callback_query_id: id };
  if (text) payload.text = text;
  try {
    await telegram(env, "answerCallbackQuery", payload);
  } catch (_) {
    // Callback acknowledgement is a UX optimization only.
  }
}

async function send(env, chatId, text, rows = null) {
  const payload = { chat_id: chatId, text, disable_web_page_preview: true };
  if (rows) payload.reply_markup = inline(rows);
  return telegram(env, "sendMessage", payload);
}

async function updatePanel(env, target, text, rows) {
  if (target && target.callbackId && target.messageId) {
    try {
      return await telegram(env, "editMessageText", {
        chat_id: target.chatId,
        message_id: target.messageId,
        text,
        disable_web_page_preview: true,
        reply_markup: inline(rows),
      });
    } catch (error) {
      const message = String((error && error.message) || error || "");
      if (message.toLowerCase().includes("message is not modified")) return null;
      // Preserve the action result if an old message can no longer be edited.
    }
  }
  return send(env, target.chatId, text, rows);
}

function githubHeaders(env, authenticated = true) {
  const headers = {
    accept: "application/vnd.github+json",
    "user-agent": "isco-telegram-observability-v1",
    "x-github-api-version": "2022-11-28",
  };
  if (authenticated) {
    const token = String(env.GITHUB_CONTROL_TOKEN || "").trim();
    if (token) headers.authorization = `Bearer ${token}`;
  }
  return headers;
}

async function githubJson(env, pathOrUrl) {
  const repo = String(env.GITHUB_REPO || DEFAULT_REPO).trim();
  const url = String(pathOrUrl || "").startsWith("https://")
    ? String(pathOrUrl)
    : `https://api.github.com/repos/${repo}/${String(pathOrUrl || "").replace(/^\/+/, "")}`;
  let response = await fetch(url, { headers: githubHeaders(env, true) });
  if (!response.ok && [401, 403, 404].includes(response.status)) {
    // Public Runner reads can safely degrade to unauthenticated GitHub access if a
    // fine-grained control token lacks a read permission on a particular endpoint.
    response = await fetch(url, { headers: githubHeaders(env, false) });
  }
  if (!response.ok) throw new Error(`GitHub read failed: ${response.status}`);
  return response.json();
}

async function publicEditorialProjection(env) {
  const repo = String(env.GITHUB_REPO || DEFAULT_REPO).trim();
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo)) throw new Error("Invalid GitHub repository name");
  const url = `https://raw.githubusercontent.com/${repo}/control-plane-state/state/telegram-status.json`;
  const response = await fetch(url, { headers: { "user-agent": "isco-telegram-observability-v1" } });
  if (!response.ok) throw new Error(`Editorial projection read failed: ${response.status}`);
  const value = await response.json();
  if (!value || Number(value.schema_version) !== 1) throw new Error("Unsupported editorial projection");
  return value;
}

async function youtubeJson(env, resource, params) {
  const key = String(env.YOUTUBE_API_KEY || "").trim();
  if (!key) throw new Error("YOUTUBE_API_KEY is missing");
  const query = new URLSearchParams({ ...params, key });
  const response = await fetch(`https://www.googleapis.com/youtube/v3/${resource}?${query.toString()}`);
  if (!response.ok) throw new Error(`YouTube API failed: ${response.status}`);
  return response.json();
}

function nowIso() {
  return new Date().toISOString();
}

function omanTime(value = new Date()) {
  const date = value instanceof Date ? value : new Date(value);
  if (!Number.isFinite(date.getTime())) return "غير معروف";
  return new Intl.DateTimeFormat("ar-OM", {
    timeZone: "Asia/Muscat",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function ageSeconds(value, now = new Date()) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return null;
  return Math.max(0, Math.round((now.getTime() - date.getTime()) / 1000));
}

function humanAge(seconds) {
  if (!Number.isFinite(seconds)) return "وقت غير معروف";
  if (seconds < 60) return `${seconds}ث`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}د`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours}س`;
  return `${Math.round(hours / 24)}ي`;
}

async function withSourceCache(name, loader) {
  const checkedAt = nowIso();
  try {
    const value = await loader();
    const result = { name, state: "fresh", value, verifiedAt: checkedAt, checkedAt, error: "" };
    SOURCE_CACHE.set(name, result);
    return result;
  } catch (error) {
    const cached = SOURCE_CACHE.get(name);
    if (cached) {
      return {
        ...cached,
        state: "stale",
        checkedAt,
        error: String((error && error.message) || error || "source unavailable"),
      };
    }
    return {
      name,
      state: "unavailable",
      value: null,
      verifiedAt: "",
      checkedAt,
      error: String((error && error.message) || error || "source unavailable"),
    };
  }
}

function sourceIcon(source) {
  if (!source || source.state === "unavailable") return "❌";
  if (source.state === "stale") return "⚠️";
  return "✅";
}

function sourceSuffix(source, now = new Date()) {
  if (!source || source.state === "unavailable") return "غير متاح";
  if (source.state === "stale") {
    const age = ageSeconds(source.verifiedAt, now);
    return `آخر حالة معروفة قبل ${humanAge(age)}`;
  }
  return `حي · ${omanTime(source.verifiedAt)}`;
}

function isProductionWorkflowRun(run) {
  const name = String((run && run.name) || "");
  return name === "Telegram Explicit Production Request" || name.startsWith("Produce Resilient");
}

async function loadProduction(env) {
  const payload = await githubJson(env, "actions/runs?per_page=50");
  const runs = (Array.isArray(payload.workflow_runs) ? payload.workflow_runs : [])
    .filter(isProductionWorkflowRun)
    .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  const run = runs.find((item) => String(item.status || "") !== "completed") || runs[0] || null;
  if (!run || !run.id) return { run: null, jobs: [] };
  const jobsPayload = await githubJson(env, `actions/runs/${run.id}/jobs?per_page=100`);
  return { run, jobs: Array.isArray(jobsPayload.jobs) ? jobsPayload.jobs : [] };
}

function currentRunStep(jobs) {
  for (const job of jobs || []) {
    for (const step of job.steps || []) {
      if (String(step.status || "") === "in_progress") return step;
    }
  }
  return null;
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

function canonicalStageForStep(stepName) {
  const raw = String(stepName || "").trim();
  const folded = raw.toLowerCase();
  for (const rule of STATUS_CONTRACT.stage_rules || []) {
    const needles = Array.isArray(rule.contains) ? rule.contains : [];
    if (folded && needles.some((needle) => folded.includes(String(needle).toLowerCase()))) {
      return { key: String(rule.key || "unknown"), label: String(rule.label || raw || "الإنتاج الجاري") };
    }
  }
  return { key: "unknown", label: raw || "الإنتاج الجاري" };
}

function canonicalRunStage(run, jobs) {
  if (!run) return { key: "idle", label: "غير نشط", progress: null, detail: "لا يوجد Production Run معروف." };
  const conclusion = String(run.conclusion || "").toLowerCase();
  const terminal = (STATUS_CONTRACT.run_terminal || {})[conclusion];
  if (terminal) {
    const [job, step] = failedLocation(jobs);
    if (conclusion === "success") {
      return { key: "success", label: terminal.label, progress: 100, detail: "اكتمل Workflow بنجاح." };
    }
    const detail = step ? `توقف عند: ${step}` : job ? `توقف عند: ${job}` : `الحالة: ${conclusion}`;
    return { key: conclusion, label: terminal.label, progress: null, detail };
  }
  const steps = (jobs || []).flatMap((job) => Array.isArray(job.steps) ? job.steps : []);
  const current = currentRunStep(jobs);
  const completed = steps.filter((step) => String(step.status || "") === "completed").length;
  const progress = steps.length ? Math.round((completed * 100) / steps.length) : null;
  if (current) {
    const stage = canonicalStageForStep(current.name);
    return { ...stage, progress, detail: `الخطوة الحالية: ${String(current.name || "")}` };
  }
  const status = String(run.status || "غير نشط");
  return {
    key: status,
    label: String((STATUS_CONTRACT.status_labels || {})[status.toLowerCase()] || status),
    progress,
    detail: "Workflow قيد التنفيذ.",
  };
}

async function loadDelivery(env) {
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

async function loadEditorialProjection(env) {
  const [projection, runs] = await Promise.all([
    publicEditorialProjection(env),
    githubJson(env, "actions/workflows/telegram-editorial-control.yml/runs?per_page=1"),
  ]);
  const controlRun = Array.isArray(runs.workflow_runs) ? runs.workflow_runs[0] || null : null;
  return { projection, controlRun };
}

function editorialHealth(value, now = new Date()) {
  const run = value && value.controlRun;
  if (!run) return { state: "stale", note: "لا توجد قراءة حديثة لـ Control Plane" };
  const status = String(run.status || "");
  const conclusion = String(run.conclusion || "");
  const age = ageSeconds(run.updated_at || run.created_at, now);
  if (status !== "completed") return { state: "fresh", note: "Control Plane يعمل الآن" };
  if (conclusion === "success" && Number.isFinite(age) && age <= 15 * 60) {
    return { state: "fresh", note: `Control Plane سليم · ${humanAge(age)}` };
  }
  return { state: "stale", note: conclusion ? `آخر Control Plane: ${conclusion} · ${humanAge(age)}` : "Control Plane غير حديث" };
}

async function loadTelegramHealth(env) {
  return telegram(env, "getWebhookInfo", {});
}

async function loadYoutubeOverview(env) {
  const channelId = String(env.YOUTUBE_CHANNEL_ID || DEFAULT_CHANNEL_ID).trim();
  const payload = await youtubeJson(env, "channels", {
    part: "statistics,contentDetails",
    id: channelId,
    maxResults: "1",
  });
  const channel = (payload.items || [])[0];
  if (!channel) throw new Error("YouTube channel not found");
  const stats = channel.statistics || {};
  return {
    subscribers: Number(stats.subscriberCount || 0),
    hiddenSubscribers: Boolean(stats.hiddenSubscriberCount),
    views: Number(stats.viewCount || 0),
    videoCount: Number(stats.videoCount || 0),
  };
}

function formatNum(value) {
  const n = Math.max(0, Number(value || 0));
  if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
  return Math.trunc(n).toLocaleString("en-US");
}

async function controlSnapshot(env) {
  const [production, delivery, editorial, youtube, telegramHealth] = await Promise.all([
    withSourceCache("production", () => loadProduction(env)),
    withSourceCache("delivery", () => loadDelivery(env)),
    withSourceCache("editorial", () => loadEditorialProjection(env)),
    withSourceCache("youtube", () => loadYoutubeOverview(env)),
    withSourceCache("telegram", () => loadTelegramHealth(env)),
  ]);
  return { production, delivery, editorial, youtube, telegramHealth, checkedAt: nowIso() };
}

function dashboardText(snapshot) {
  const now = new Date(snapshot.checkedAt);
  const lines = ["🏠 نداء اليقظة · لوحة التشغيل", ""];

  const prod = snapshot.production;
  if (prod.state === "unavailable") {
    lines.push("🎬 الإنتاج: ❌ غير متاح الآن");
  } else {
    const value = prod.value || {};
    const run = value.run;
    const stage = canonicalRunStage(run, value.jobs || []);
    const runSuffix = run && run.run_number ? ` · Run #${run.run_number}` : "";
    const progress = Number.isFinite(stage.progress) ? ` · ${stage.progress}%` : "";
    lines.push(`🎬 الإنتاج: ${stage.label}${progress}${runSuffix}${prod.state === "stale" ? " ⚠️" : ""}`);
    if (prod.state === "stale") lines.push(`   ↳ ${sourceSuffix(prod, now)}`);
  }

  const editorial = snapshot.editorial;
  if (editorial.state === "unavailable") {
    lines.push("📚 التحرير: ❌ الإسقاط الآمن غير متاح بعد");
  } else {
    const value = editorial.value || {};
    const projection = value.projection || {};
    const state = projection.editorial || {};
    const health = editorialHealth(value, now);
    const effectiveStale = editorial.state === "stale" || health.state === "stale";
    const research = state.research_active ? "بحث جارٍ" : "لا بحث جارٍ";
    const approved = state.approved_target ? " · اعتماد جاهز" : "";
    lines.push(`📚 التحرير: ${research} · محفوظة ${Number(state.saved_count || 0)} · مستعملة ${Number(state.used_count || 0)}${approved}${effectiveStale ? " ⚠️" : ""}`);
    if (effectiveStale) lines.push(`   ↳ ${editorial.state === "stale" ? sourceSuffix(editorial, now) : health.note}`);
  }

  const delivery = snapshot.delivery;
  if (delivery.state === "unavailable") {
    lines.push("🎁 آخر حزمة: ❌ تعذر التحقق");
  } else if (!delivery.value) {
    lines.push(`🎁 آخر حزمة: لا توجد حزمة منشورة${delivery.state === "stale" ? " ⚠️" : ""}`);
  } else {
    const release = delivery.value;
    const label = String(release.name || release.tag_name || "الحزمة الأخيرة");
    lines.push(`🎁 آخر حزمة: ${label}${delivery.state === "stale" ? " ⚠️" : ""}`);
  }

  const yt = snapshot.youtube;
  if (yt.state === "unavailable") {
    lines.push("📈 YouTube: ❌ غير متاح الآن");
  } else {
    const data = yt.value || {};
    const subs = data.hiddenSubscribers ? "مخفية" : formatNum(data.subscribers);
    lines.push(`📈 YouTube: ${subs} مشترك · ${formatNum(data.views)} مشاهدة${yt.state === "stale" ? " ⚠️" : ""}`);
  }

  const tg = snapshot.telegramHealth;
  let telegramDetail = `${sourceIcon(tg)} ${sourceSuffix(tg, now)}`;
  if (tg.state !== "unavailable" && tg.value) {
    const pending = Number(tg.value.pending_update_count || 0);
    const lastError = String(tg.value.last_error_message || "").trim();
    telegramDetail = `${sourceIcon(tg)} webhook · pending ${pending}`;
    if (lastError) telegramDetail += " · ⚠️ خطأ مسجل";
  }
  lines.extend([
    "",
    `🛰️ المصادر: GitHub ${sourceIcon(prod)} · Telegram ${sourceIcon(tg)} · YouTube ${sourceIcon(yt)} · Editorial ${sourceIcon(editorial)}`,
    `   Telegram: ${telegramDetail}`,
    "",
    `🕒 آخر تحقق شامل: ${omanTime(now)} · عُمان`,
    "ℹ️ «تحديث الكل» قراءة فقط؛ لا يبدأ ولا يعيد أي Production Run.",
  ]);
  return lines.join("\n");
}

async function showDashboard(env, target) {
  const snapshot = await controlSnapshot(env);
  await updatePanel(env, target, dashboardText(snapshot), ROOT_ROWS);
}

async function showCanonicalStatus(env, target) {
  const source = await withSourceCache("production", () => loadProduction(env));
  const rows = [
    [
      { text: "🔄 تحديث الحالة", callback_data: "cmd:status" },
      { text: "🔄 تحديث الكل", callback_data: "cmd:refresh_all" },
    ],
    [{ text: "🏠 الرئيسية", callback_data: "cmd:menu" }],
  ];
  if (source.state === "unavailable") {
    await updatePanel(
      env,
      target,
      `📊 حالة الإنتاج\n\n❌ تعذر قراءة GitHub الآن. لا يتم عرض حالة قديمة على أنها حية.\n\n🕒 آخر محاولة تحقق: ${omanTime(source.checkedAt)} · عُمان`,
      rows,
    );
    return;
  }
  const value = source.value || {};
  const run = value.run;
  const stage = canonicalRunStage(run, value.jobs || []);
  const lines = ["📊 حالة الإنتاج", ""];
  if (run && run.run_number) lines[0] += ` · Run #${run.run_number}`;
  lines.push(`${stage.label}${Number.isFinite(stage.progress) ? ` · ${stage.progress}%` : ""}`);
  lines.push(stage.detail);
  if (run && String(run.html_url || "").startsWith("https://")) {
    rows.splice(rows.length - 1, 0, [{ text: "🔗 GitHub", url: run.html_url }]);
  }
  if (source.state === "stale") lines.extend(["", `⚠️ ${sourceSuffix(source)}`]);
  else lines.extend(["", `✅ GitHub حي · آخر تحقق: ${omanTime(source.verifiedAt)} · عُمان`]);
  lines.push("ℹ️ شاشة قراءة فقط؛ لا تعيد الإنتاج ولا تتجاوز Quality Gates.");
  await updatePanel(env, target, lines.join("\n"), rows);
}

function parseDurationSeconds(value) {
  const match = /^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$/.exec(String(value || ""));
  if (!match) return 0;
  return Number(match[1] || 0) * 86400 + Number(match[2] || 0) * 3600 + Number(match[3] || 0) * 60 + Number(match[4] || 0);
}

async function liveYoutube(env) {
  const channelId = String(env.YOUTUBE_CHANNEL_ID || DEFAULT_CHANNEL_ID).trim();
  const channelPayload = await youtubeJson(env, "channels", { part: "statistics,contentDetails", id: channelId, maxResults: "1" });
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
      title: String((item.snippet && item.snippet.title) || ""),
      publishedAt: String((item.snippet && item.snippet.publishedAt) || ""),
      duration: parseDurationSeconds(item.contentDetails && item.contentDetails.duration),
      views: Number((item.statistics && item.statistics.viewCount) || 0),
      likes: Number((item.statistics && item.statistics.likeCount) || 0),
      comments: Number((item.statistics && item.statistics.commentCount) || 0),
    })).sort((a, b) => b.publishedAt.localeCompare(a.publishedAt));
  }
  const stats = channel.statistics || {};
  return {
    fetchedAt: new Date(),
    hiddenSubscribers: Boolean(stats.hiddenSubscriberCount),
    subscribers: Number(stats.subscriberCount || 0),
    views: Number(stats.viewCount || 0),
    videoCount: Number(stats.videoCount || 0),
    videos,
  };
}

function periodStartUtc(days, now = new Date()) {
  const shifted = new Date(now.getTime() + 4 * 3600 * 1000);
  const y = shifted.getUTCFullYear();
  const m = shifted.getUTCMonth();
  const d = shifted.getUTCDate() - Math.max(0, days - 1);
  return new Date(Date.UTC(y, m, d, 0, 0, 0) - 4 * 3600 * 1000);
}

async function showStats(env, target, data) {
  const kind = String(data || "").replace(/^cmd:/, "");
  const source = await withSourceCache(`youtube:${kind}`, () => liveYoutube(env));
  const rows = [
    [
      { text: "🔄 تحديث", callback_data: `cmd:${kind}` },
      { text: "🔄 تحديث الكل", callback_data: "cmd:refresh_all" },
    ],
    [{ text: "↩️ الإحصائيات", callback_data: "cmd:stats_menu" }],
    [{ text: "🏠 الرئيسية", callback_data: "cmd:menu" }],
  ];
  if (source.state === "unavailable") {
    await updatePanel(env, target, `📈 الإحصائيات\n\n❌ تعذر تحديث YouTube الآن. لا أعرض أرقامًا قديمة على أنها حية.\n\n🕒 آخر محاولة: ${omanTime(source.checkedAt)} · عُمان`, rows);
    return;
  }
  const live = source.value;
  const suffix = source.state === "stale"
    ? `\n\n⚠️ ${sourceSuffix(source)}`
    : `\n\n✅ تحديث حي: ${omanTime(source.verifiedAt)} · عُمان`;
  if (kind === "stats_overview") {
    const subs = live.hiddenSubscribers ? "مخفية" : formatNum(live.subscribers);
    await updatePanel(env, target, `📊 إحصائيات عامة\n\n👥 المشتركون: ${subs}\n👁️ مشاهدات القناة: ${formatNum(live.views)}\n🎞️ إجمالي الفيديوهات: ${formatNum(live.videoCount)}${suffix}`, rows);
    return;
  }
  if (kind === "stats_last_long" || kind === "stats_last_short") {
    const wantShort = kind === "stats_last_short";
    const item = live.videos.find((video) => (video.duration > 0 && video.duration <= 180) === wantShort);
    if (!item) {
      await updatePanel(env, target, `📈 آخر ${wantShort ? "Short" : "فيديو طويل"}\n\nلم أجد عنصرًا حديثًا مناسبًا ضمن آخر الرفعات.${suffix}`, rows);
      return;
    }
    rows.splice(1, 0, [{ text: "▶️ فتح على YouTube", url: `https://youtu.be/${item.id}` }]);
    await updatePanel(env, target, `📈 آخر ${wantShort ? "Short" : "فيديو طويل"}\n\n🎬 ${item.title}\n\n👁️ ${formatNum(item.views)} مشاهدة\n👍 ${formatNum(item.likes)} إعجاب\n💬 ${formatNum(item.comments)} تعليق${suffix}`, rows);
    return;
  }
  const days = kind === "stats_today" ? 1 : 7;
  const start = periodStartUtc(days, live.fetchedAt);
  const periodVideos = live.videos.filter((video) => new Date(video.publishedAt) >= start);
  const currentViews = periodVideos.reduce((sum, video) => sum + video.views, 0);
  const shorts = periodVideos.filter((video) => video.duration > 0 && video.duration <= 180).length;
  const label = days === 1 ? "اليوم" : "آخر 7 أيام";
  await updatePanel(env, target, `📈 ${label}\n\n🆕 رفعات منشورة ضمن الفترة: ${periodVideos.length}\n⚡ منها Shorts تقريبًا: ${shorts}\n👁️ المشاهدات الحالية لهذه الرفعات: ${formatNum(currentViews)}${suffix}\nℹ️ بوصلة تقريبية وليست YouTube Analytics.`, rows);
}

function isMenuText(text) {
  const value = String(text || "").trim();
  return ["🏠 ابدأ", "🎛 ابدأ", "/start", "/menu", "ابدأ", "القائمة"].includes(value);
}

function readOnlyRoute(update) {
  const callback = update && update.callback_query;
  if (callback) {
    const data = String(callback.data || "");
    if (data === "cmd:refresh_all" || data === "cmd:menu") return "dashboard";
    if (data === "cmd:status") return "status";
    if (STATS_LEAVES.has(data)) return data;
    return "";
  }
  const text = String((update && update.message && update.message.text) || "");
  return isMenuText(text) ? "dashboard" : "";
}

async function handleReadOnly(route, update, env) {
  const target = actorAndChat(update);
  if (route === "dashboard") {
    await answerCallback(env, target.callbackId, "أحدّث الصورة الكاملة الآن…");
    await showDashboard(env, target);
    return;
  }
  if (route === "status") {
    await answerCallback(env, target.callbackId, "أتحقق من GitHub الآن…");
    await showCanonicalStatus(env, target);
    return;
  }
  if (STATS_LEAVES.has(route)) {
    await answerCallback(env, target.callbackId, "أحدّث YouTube الآن…");
    await showStats(env, target, route);
  }
}

async function safeReadOnly(route, update, env) {
  const target = actorAndChat(update);
  try {
    await handleReadOnly(route, update, env);
  } catch (error) {
    console.error("Observability read failed", String((error && error.message) || error || "unknown"));
    try {
      await updatePanel(
        env,
        target,
        `⚠️ تعذر إكمال التحديث الآن. لم يبدأ ولم يتغير أي Production Run.\n\n🕒 آخر محاولة: ${omanTime()} · عُمان`,
        ROOT_ROWS,
      );
    } catch (_) {
      // Telegram itself may be the unavailable source; do not mutate anything else.
    }
  }
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health") {
      return new Response(
        JSON.stringify({ ok: true, mode: "telegram-edge-control", observability: "v1", status_schema: STATUS_CONTRACT.schema_version }),
        { headers: { "content-type": "application/json" } },
      );
    }

    if (request.method === "POST" && url.pathname === "/telegram" && secretHeaderValid(request, env)) {
      let update = null;
      try {
        update = await request.clone().json();
      } catch (_) {
        return baseWorker.fetch(request, env, ctx);
      }
      if (update && Number.isInteger(update.update_id) && authorized(update, env)) {
        const route = readOnlyRoute(update);
        if (route) {
          ctx.waitUntil(safeReadOnly(route, update, env));
          return new Response("OK");
        }
      }
    }
    return baseWorker.fetch(request, env, ctx);
  },
};
