import priorWorker from "./observability-worker-v5-core.js";
import { STATUS_CONTRACT } from "./status-contract.generated.js";

const DEFAULT_REPO = "mymusa79-tech/Isco-Video-Runner";
const DEFAULT_CHANNEL_ID = "UC_fmWGRen6QUQNd4Dj80MgA";
const CANONICAL_PRODUCTION_PATHS = new Set([
  ".github/workflows/telegram-production-request.yml",
  ".github/workflows/produce-resilient-v4.yml",
]);
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
    // Read-side callback UX never owns a production side effect.
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
        reply_markup: { inline_keyboard: rows },
      });
    } catch (error) {
      if (String((error && error.message) || "").toLowerCase().includes("message is not modified")) return null;
    }
  }
  return telegram(env, "sendMessage", {
    chat_id: target.chatId,
    text,
    disable_web_page_preview: true,
    reply_markup: { inline_keyboard: rows },
  });
}

function githubHeaders(env, authenticated = true) {
  const headers = {
    accept: "application/vnd.github+json",
    "user-agent": "isco-telegram-production-observer-v1",
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
  let response = await fetch(url, { headers: githubHeaders(env, true), cache: "no-store" });
  if (!response.ok && [401, 403, 404].includes(response.status)) {
    response = await fetch(url, { headers: githubHeaders(env, false), cache: "no-store" });
  }
  if (!response.ok) throw new Error(`GitHub read failed: ${response.status}`);
  return response.json();
}

function canonicalRunPath(run) {
  return String((run && run.path) || "").replace(/^\/+/, "").trim();
}

function isProductionRun(run) {
  return CANONICAL_PRODUCTION_PATHS.has(canonicalRunPath(run));
}

function isV4Run(run) {
  return canonicalRunPath(run) === ".github/workflows/produce-resilient-v4.yml";
}

async function productionState(env) {
  const payload = await githubJson(env, "actions/runs?per_page=50");
  const runs = (Array.isArray(payload.workflow_runs) ? payload.workflow_runs : [])
    .filter(isProductionRun)
    .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  const active = runs.filter((item) => String(item.status || "") !== "completed");
  const run = active.find(isV4Run) || active[0] || runs.find(isV4Run) || runs[0] || null;
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
  if (!isV4Run(run) && String(run.status || "") !== "completed") {
    return { label: "بوابة التفويض", detail: "تم قبول الطلب ويجري تسليمه إلى Production V4.", progress: null };
  }
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

async function publicProjection(env) {
  const repo = String(env.GITHUB_REPO || DEFAULT_REPO).trim();
  const response = await fetch(`https://raw.githubusercontent.com/${repo}/control-plane-state/state/telegram-status.json`, {
    headers: { "user-agent": "isco-telegram-production-observer-v1" },
    cache: "no-store",
  });
  if (!response.ok) throw new Error(`Projection read failed: ${response.status}`);
  const value = await response.json();
  if (!value || Number(value.schema_version) !== 1) throw new Error("Unsupported editorial projection");
  return value;
}

function formatNum(value) {
  const n = Math.max(0, Number(value || 0));
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return Math.trunc(n).toLocaleString("en-US");
}

function omanTime(value = new Date()) {
  return new Intl.DateTimeFormat("ar-OM", {
    timeZone: "Asia/Muscat",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
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
  const response = await fetch(`https://www.googleapis.com/youtube/v3/channels?${query.toString()}`, { cache: "no-store" });
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

async function showStatus(env, target, system = false) {
  const [productionResult, projectionResult] = await Promise.allSettled([productionState(env), publicProjection(env)]);
  const production = productionResult.status === "fulfilled" ? productionResult.value : { run: null, jobs: [] };
  const projection = projectionResult.status === "fulfilled" ? projectionResult.value : null;
  const stage = productionStage(production.run, production.jobs);
  const active = production.run && String(production.run.status || "") !== "completed";
  const editorial = projection && projection.editorial ? projection.editorial : {};
  let now;
  let action;

  if (active) {
    now = `🚀 Production جارٍ: ${stage.label}${Number.isFinite(stage.progress) ? ` · ${stage.progress}%` : ""}`;
    action = "لا شيء الآن — لا تكرر تأكيد الإنتاج.";
  } else if (Number(editorial.production_waiting_count || 0) > 0 || Number(editorial.production_reserved_count || 0) > 0) {
    now = "🚀 طلب Production مؤكد وموجود في مسار الإرسال المحمي.";
    action = "لا شيء الآن — الإرسال يتم تلقائيًا، ولا يحتاج زر التحديث.";
  } else if (Boolean(editorial.approved_target)) {
    now = "✅ لديك موضوع معتمد ينتظر قرار التشغيل.";
    action = "إذا كان القرار نهائيًا، أرسل حرفيًا: تأكيد الإنتاج";
  } else if (production.run) {
    now = `آخر Production V4: ${stage.label}${production.run.run_number ? ` · Run #${production.run.run_number}` : ""}`;
    action = "لا يوجد إجراء مطلوب.";
  } else {
    now = "🟢 لا يوجد Production Run نشط.";
    action = "لا يوجد إجراء مطلوب.";
  }

  const lines = system
    ? [
        `📋 تفاصيل النظام${production.run && production.run.run_number ? ` · Run #${production.run.run_number}` : ""}`,
        "",
        `الحالة: ${stage.label}${Number.isFinite(stage.progress) ? ` · ${stage.progress}%` : ""}`,
        stage.detail,
        production.run ? `المسار: ${canonicalRunPath(production.run)}` : "المسار: لا يوجد تشغيل معروف",
        "",
        "✅ قراءة مباشرة من GitHub بالـworkflow path، لا بالاسم الظاهر.",
        "🔐 هذه شاشة قراءة فقط؛ لا تبدأ Production.",
      ]
    : [
        "🧭 الحالة — ماذا يحدث الآن؟",
        "",
        "الآن",
        now,
        "",
        "مطلوب منك",
        action,
        "",
        `🕒 تحقق حي: ${omanTime()} · عُمان`,
      ];

  const rows = system
    ? [[{ text: "↩️ الحالة", callback_data: "cmd:status" }], [{ text: "🏠 الرئيسية", callback_data: "cmd:menu" }]]
    : [[{ text: "📋 تفاصيل النظام", callback_data: "cmd:system_status" }], [{ text: "🔄 تحديث", callback_data: "cmd:status" }, { text: "🏠 الرئيسية", callback_data: "cmd:menu" }]];
  if (production.run && String(production.run.html_url || "").startsWith("https://")) {
    rows.splice(rows.length - 1, 0, [{ text: "🔗 GitHub", url: production.run.html_url }]);
  }
  await updatePanel(env, target, lines.join("\n"), rows);
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
    "✅ Production يُقرأ حيًا من GitHub بالـworkflow path.",
    "ℹ️ «تحديث الكل» قراءة فقط؛ التشغيل يبدأ فقط بعد «تأكيد الإنتاج».",
  );
  await updatePanel(env, target, lines.join("\n"), ROOT_ROWS);
}

function monitorRoute(update) {
  const callback = update && update.callback_query;
  if (callback && typeof callback === "object") {
    const data = String(callback.data || "");
    if (data === "cmd:status") return "status";
    if (data === "cmd:system_status") return "system";
    if (data === "cmd:refresh_all") return "dashboard";
    return "";
  }
  const text = String(update && update.message && update.message.text || "").trim();
  if (["الحالة", "حالة", "status", "4", "٤"].includes(text)) return "status";
  return "";
}

async function serveMonitor(route, update, env) {
  const target = actor(update);
  await ack(env, target.callbackId, route === "dashboard" ? "أحدّث الصورة الكاملة الآن…" : "أتحقق من Production الآن…");
  if (route === "dashboard") return showDashboard(env, target);
  return showStatus(env, target, route === "system");
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/telegram" && secretHeaderValid(request, env)) {
      let update;
      try {
        update = await request.clone().json();
      } catch (_) {
        return priorWorker.fetch(request, env, ctx);
      }
      if (update && Number.isInteger(update.update_id) && authorized(update, env)) {
        const route = monitorRoute(update);
        if (route) {
          ctx.waitUntil((async () => {
            try {
              await serveMonitor(route, update, env);
            } catch (error) {
              console.error("Production observability authority failed", String((error && error.message) || error || "unknown"));
              try {
                await updatePanel(env, actor(update), "⚠️ تعذر تحديث مراقبة Production الآن. لم يبدأ ولم يتغير أي Production Run.", ROOT_ROWS);
              } catch (_) {
                // Telegram itself may be unavailable; read-side failure must stay side-effect free.
              }
            }
          })());
          return new Response("OK");
        }
      }
    }
    return priorWorker.fetch(request, env, ctx);
  },
};
