import priorWorker from "./observability-worker.js";
import { STATUS_CONTRACT } from "./status-contract.generated.js";

const DEFAULT_REPO = "mymusa79-tech/Isco-Video-Runner";
const CANONICAL_PRODUCTION_WORKFLOW = "produce-resilient-v4.yml";
const CANONICAL_PRODUCTION_PATH = ".github/workflows/produce-resilient-v4.yml";
const PROGRESS_REF = "control-plane-state";
const PROGRESS_PATH = "state/production-progress.json";
const INTERNAL_STAGE_ORDER = ["planning", "voice", "visuals", "mux"];

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

async function ack(env, callbackId, text = "") {
  if (!callbackId) return;
  try {
    await telegram(env, "answerCallbackQuery", {
      callback_query_id: callbackId,
      ...(text ? { text } : {}),
    });
  } catch (_) {
    // Read-only callback acknowledgement is best-effort.
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

function githubHeaders(env, authenticated = true) {
  const headers = {
    accept: "application/vnd.github+json",
    "user-agent": "isco-telegram-control-v6-live-progress",
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

function decodeBase64Utf8(value) {
  const compact = String(value || "").replace(/\s+/g, "");
  const binary = atob(compact);
  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

async function liveProgress(env, run) {
  if (!run || !run.id) return null;
  try {
    const payload = await githubJson(env, `contents/${PROGRESS_PATH}?ref=${encodeURIComponent(PROGRESS_REF)}`);
    if (!payload || payload.encoding !== "base64" || !payload.content) return null;
    const value = JSON.parse(decodeBase64Utf8(payload.content));
    if (!value || Number(value.schema_version) !== 1) return null;
    if (String(value.run_id || "") !== String(run.id)) return null;
    const stage = String(value.stage || "");
    if (!["starting", ...INTERNAL_STAGE_ORDER].includes(stage)) return null;
    return value;
  } catch (_) {
    return null;
  }
}

async function canonicalProductionState(env) {
  // Query the canonical workflow directly. Never infer production authority from a
  // display name, and never confuse the short Telegram gateway run with Production V4.
  const payload = await githubJson(
    env,
    `actions/workflows/${CANONICAL_PRODUCTION_WORKFLOW}/runs?branch=main&per_page=20`,
  );
  const runs = Array.isArray(payload.workflow_runs) ? payload.workflow_runs : [];
  const canonical = runs
    .filter((run) => String(run.path || "") === CANONICAL_PRODUCTION_PATH)
    .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  const run = canonical.find((item) => String(item.status || "") !== "completed") || canonical[0] || null;
  if (!run || !run.id) return { run: null, jobs: [], progress: null };
  const jobsPayload = await githubJson(env, `actions/runs/${run.id}/jobs?per_page=100`);
  const jobs = Array.isArray(jobsPayload.jobs) ? jobsPayload.jobs : [];
  const progress = await liveProgress(env, run);
  return { run, jobs, progress };
}

function currentLocation(jobs) {
  for (const job of jobs || []) {
    const steps = Array.isArray(job.steps) ? job.steps : [];
    const current = steps.find((step) => String(step.status || "") === "in_progress");
    if (current) return { job: String(job.name || ""), step: String(current.name || "") };
  }
  return { job: "", step: "" };
}

function failedLocation(jobs) {
  for (const job of jobs || []) {
    if (!["failure", "cancelled", "timed_out"].includes(String(job.conclusion || ""))) continue;
    const steps = Array.isArray(job.steps) ? job.steps : [];
    const failed = steps.find((step) => ["failure", "cancelled", "timed_out"].includes(String(step.conclusion || "")));
    return { job: String(job.name || ""), step: String((failed && failed.name) || "") };
  }
  return { job: "", step: "" };
}

function stageLabel(code) {
  if (code === "starting") return "تهيئة الإنتاج";
  return String(((STATUS_CONTRACT.status_labels || {})[code]) || code || "الإنتاج الجاري");
}

function externalStage(step) {
  const folded = String(step || "").toLowerCase();
  const rule = (STATUS_CONTRACT.stage_rules || []).find((item) =>
    (item.contains || []).some((needle) => folded.includes(String(needle).toLowerCase())),
  );
  return String((rule && rule.label) || step || "الإنتاج الجاري");
}

function productionView(value) {
  const run = value && value.run;
  const jobs = (value && value.jobs) || [];
  const progress = value && value.progress;
  if (!run) {
    return {
      headline: "⚪ لا يوجد Production V4 معروف.",
      detail: "لا توجد محاولة إنتاج مسجلة في المسار canonical.",
      internal: "",
    };
  }
  const conclusion = String(run.conclusion || "").toLowerCase();
  if (String(run.status || "") === "completed") {
    const terminal = (STATUS_CONTRACT.run_terminal || {})[conclusion];
    const failed = failedLocation(jobs);
    const label = String((terminal && terminal.label) || conclusion || "مكتمل");
    const detail = conclusion === "success"
      ? "اكتمل Production V4 بنجاح."
      : failed.step ? `توقف عند: ${failed.step}` : failed.job ? `توقف عند Job: ${failed.job}` : `الحالة النهائية: ${label}`;
    return { headline: `${conclusion === "success" ? "✅" : "❌"} ${label}`, detail, internal: "" };
  }

  const current = currentLocation(jobs);
  let label = externalStage(current.step);
  let internal = "";
  if (progress) {
    label = stageLabel(String(progress.stage || ""));
    const stageIndex = INTERNAL_STAGE_ORDER.indexOf(String(progress.stage || ""));
    internal = stageIndex >= 0 ? `المرحلة الداخلية: ${label} · ${stageIndex + 1}/${INTERNAL_STAGE_ORDER.length}` : `المرحلة الداخلية: ${label}`;
  }
  return {
    headline: `🚀 جارٍ · ${label}`,
    detail: current.step ? `خطوة GitHub الحالية: ${current.step}` : "Workflow قيد التنفيذ.",
    internal,
  };
}

function omanTime(value = new Date()) {
  return new Intl.DateTimeFormat("ar-OM", {
    timeZone: "Asia/Muscat",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function progressFreshness(progress) {
  const value = String((progress && progress.updated_at) || "").trim();
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "";
  return `آخر انتقال Stage: ${omanTime(parsed)} · عُمان`;
}

async function telegramHealth(env) {
  try {
    const info = await telegram(env, "getWebhookInfo", {});
    return {
      ok: true,
      pending: Number((info && info.pending_update_count) || 0),
      error: String((info && info.last_error_message) || "").trim(),
    };
  } catch (_) {
    return { ok: false, pending: null, error: "" };
  }
}

function rowsFor(value, includeHome = true) {
  const rows = [[
    { text: "🔄 تحديث", callback_data: "cmd:status" },
    { text: "🔄 تحديث الكل", callback_data: "cmd:refresh_all" },
  ]];
  if (value && value.run && String(value.run.html_url || "").startsWith("https://")) {
    rows.push([{ text: "🔗 فتح Run في GitHub", url: value.run.html_url }]);
  }
  if (includeHome) rows.push([{ text: "🏠 الرئيسية", callback_data: "cmd:menu" }]);
  return rows;
}

async function showLiveStatus(env, target, stageHint = "") {
  const value = await canonicalProductionState(env);
  // A stage hint comes only from the lifecycle message that Production itself edits.
  // It may improve display while the durable progress commit is propagating, but it
  // never overrides a matching durable stage from the canonical run.
  if (!value.progress && INTERNAL_STAGE_ORDER.includes(stageHint) && value.run && String(value.run.status || "") !== "completed") {
    value.progress = { stage: stageHint, run_id: String(value.run.id), updated_at: "" };
  }
  const view = productionView(value);
  const lines = [
    `🧭 Production V4${value.run && value.run.run_number ? ` · Run #${value.run.run_number}` : ""}`,
    "",
    view.headline,
  ];
  if (view.internal) lines.push(view.internal);
  lines.push(view.detail);
  const freshness = progressFreshness(value.progress);
  if (freshness) lines.push(freshness);
  lines.push("", `🕒 قراءة مباشرة: ${omanTime()} · عُمان`, "🔐 قراءة فقط؛ لا تبدأ ولا تعيد Production.");
  await updatePanel(env, target, lines.join("\n"), rowsFor(value));
}

async function showRefreshAll(env, target) {
  const [productionResult, hookResult] = await Promise.allSettled([
    canonicalProductionState(env),
    telegramHealth(env),
  ]);
  const value = productionResult.status === "fulfilled" ? productionResult.value : { run: null, jobs: [], progress: null };
  const view = productionResult.status === "fulfilled"
    ? productionView(value)
    : { headline: "⚠️ تعذر قراءة Production الآن", detail: "GitHub read failed.", internal: "" };
  const lines = [
    "🔄 تحديث الكل — قراءة حية",
    "",
    `🎬 Production${value.run && value.run.run_number ? ` · Run #${value.run.run_number}` : ""}`,
    view.headline,
  ];
  if (view.internal) lines.push(view.internal);
  lines.push(view.detail);
  const freshness = progressFreshness(value.progress);
  if (freshness) lines.push(freshness);
  lines.push("");
  if (hookResult.status === "fulfilled" && hookResult.value.ok) {
    const hook = hookResult.value;
    lines.push(`🛰️ Telegram: webhook ✅ · pending ${hook.pending}${hook.error ? " · ⚠️ خطأ مسجل" : ""}`);
  } else {
    lines.push("🛰️ Telegram: ⚠️ تعذر فحص webhook الآن");
  }
  lines.push("", `🕒 آخر تحقق: ${omanTime()} · عُمان`, "ℹ️ لا يتم تشغيل أو إعادة أي Production من هذا الزر.");
  await updatePanel(env, target, lines.join("\n"), rowsFor(value));
}

function liveRoute(data) {
  if (data === "cmd:status" || data === "cmd:system_status") return { kind: "status", stage: "" };
  if (data === "cmd:refresh_all") return { kind: "refresh", stage: "" };
  const match = /^cmd:progress_stage:(planning|voice|visuals|mux)$/.exec(String(data || ""));
  if (match) return { kind: "status", stage: match[1] };
  return null;
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
    const route = update.callback_query ? liveRoute(target.data) : null;
    if (!route) return priorWorker.fetch(request, env, ctx);
    ctx.waitUntil((async () => {
      await ack(env, target.callbackId, "أقرأ مرحلة الرن الآن…");
      try {
        if (route.kind === "refresh") await showRefreshAll(env, target);
        else await showLiveStatus(env, target, route.stage);
      } catch (error) {
        console.error("Telegram live production read failed", String((error && error.message) || error || "unknown"));
        await updatePanel(
          env,
          target,
          `⚠️ تعذر قراءة المرحلة الحية الآن. لم يبدأ ولم يتغير أي Production Run.\n\n🕒 ${omanTime()} · عُمان`,
          [[{ text: "🔄 إعادة القراءة", callback_data: "cmd:status" }], [{ text: "🏠 الرئيسية", callback_data: "cmd:menu" }]],
        );
      }
    })());
    return new Response("OK");
  },
};
