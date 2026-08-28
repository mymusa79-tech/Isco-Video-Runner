import baseWorker from "./index.js";
import { STATUS_CONTRACT } from "./status-contract.generated.js";

const DEFAULT_REPO = "mymusa79-tech/Isco-Video-Runner";
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

function inline(rows) { return { inline_keyboard: rows }; }
function omanTime(value = new Date()) {
  return new Intl.DateTimeFormat("ar-OM", { timeZone: "Asia/Muscat", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value));
}
function actor(update) {
  const cb = update && update.callback_query;
  if (cb) return { userId: cb.from && cb.from.id, chatId: cb.message && cb.message.chat && cb.message.chat.id, messageId: cb.message && cb.message.message_id, callbackId: cb.id, data: String(cb.data || "") };
  const msg = update && update.message;
  return { userId: msg && msg.from && msg.from.id, chatId: msg && msg.chat && msg.chat.id, messageId: msg && msg.message_id, callbackId: "", data: "" };
}
function authorized(update, env) {
  const a = actor(update);
  return Boolean(String(env.TELEGRAM_ALLOWED_USER_ID || "").trim()) && Boolean(String(env.TELEGRAM_CHAT_ID || "").trim()) && String(a.userId ?? "") === String(env.TELEGRAM_ALLOWED_USER_ID).trim() && String(a.chatId ?? "") === String(env.TELEGRAM_CHAT_ID).trim();
}
function secretHeaderValid(request, env) {
  return Boolean(String(env.TELEGRAM_WEBHOOK_SECRET || "").trim()) && String(request.headers.get("X-Telegram-Bot-Api-Secret-Token") || "").trim() === String(env.TELEGRAM_WEBHOOK_SECRET).trim();
}
async function telegram(env, method, payload = {}) {
  const token = String(env.TELEGRAM_BOT_TOKEN || "").trim();
  if (!token) throw new Error("TELEGRAM_BOT_TOKEN is missing");
  const response = await fetch(`https://api.telegram.org/bot${token}/${method}`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload) });
  const body = await response.json().catch(() => ({}));
  if (!response.ok || !body.ok) throw new Error(`Telegram ${method} failed: ${String(body.description || response.status)}`);
  return body.result;
}
async function ack(env, id, text = "") {
  if (!id) return;
  try { await telegram(env, "answerCallbackQuery", { callback_query_id: id, ...(text ? { text } : {}) }); } catch (_) {}
}
async function updatePanel(env, target, text, rows) {
  if (target.callbackId && target.messageId) {
    try {
      return await telegram(env, "editMessageText", { chat_id: target.chatId, message_id: target.messageId, text, disable_web_page_preview: true, reply_markup: inline(rows) });
    } catch (error) {
      if (String(error && error.message || "").toLowerCase().includes("message is not modified")) return null;
    }
  }
  return telegram(env, "sendMessage", { chat_id: target.chatId, text, disable_web_page_preview: true, reply_markup: inline(rows) });
}
function rootText() {
  return "🏠 نداء اليقظة\n\n1️⃣ 🔎 البحث\n2️⃣ 📚 المواضيع\n3️⃣ 🎁 آخر إنتاج\n4️⃣ 📊 الحالة\n5️⃣ 📈 الإحصائيات\n\n⚡ التنقل والقراءة السريعة تعمل مباشرة من Edge.\n🔐 لا يبدأ Production من أزرار القراءة.";
}
function menuRoute(data) {
  if (data === "cmd:menu") return [rootText(), ROOT_ROWS];
  if (data === "cmd:search_menu") return ["🔎 البحث\n\nاختر نوع البحث. هذا بحث فقط ولا يبدأ Production.", SEARCH_ROWS];
  if (data === "cmd:library_menu") return ["📚 المواضيع\n\nاختر المحفوظة أو المستعملة.", LIBRARY_ROWS];
  if (data === "cmd:stats_menu") return ["📈 الإحصائيات\n\nاختر القراءة التي تريدها من YouTube.", STATS_ROWS];
  return null;
}
function githubHeaders(env, auth = true) {
  const h = { accept: "application/vnd.github+json", "user-agent": "isco-telegram-edge-v2", "x-github-api-version": "2022-11-28" };
  if (auth && String(env.GITHUB_CONTROL_TOKEN || "").trim()) h.authorization = `Bearer ${String(env.GITHUB_CONTROL_TOKEN).trim()}`;
  return h;
}
async function githubJson(env, suffix) {
  const repo = String(env.GITHUB_REPO || DEFAULT_REPO).trim();
  const url = `https://api.github.com/repos/${repo}/${String(suffix).replace(/^\/+/, "")}`;
  let response = await fetch(url, { headers: githubHeaders(env, true) });
  if (!response.ok && [401, 403, 404].includes(response.status)) response = await fetch(url, { headers: githubHeaders(env, false) });
  if (!response.ok) throw new Error(`GitHub read failed: ${response.status}`);
  return response.json();
}
function isProductionRun(run) {
  const name = String(run && run.name || "");
  return name === "Telegram Explicit Production Request" || name.startsWith("Produce Resilient");
}
async function productionState(env) {
  const payload = await githubJson(env, "actions/runs?per_page=50");
  const runs = (Array.isArray(payload.workflow_runs) ? payload.workflow_runs : []).filter(isProductionRun).sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  const run = runs.find((x) => String(x.status || "") !== "completed") || runs[0] || null;
  if (!run || !run.id || String(run.status || "") === "completed") return { run, jobs: [] };
  const jobs = await githubJson(env, `actions/runs/${run.id}/jobs?per_page=100`);
  return { run, jobs: Array.isArray(jobs.jobs) ? jobs.jobs : [] };
}
function productionStage(run, jobs) {
  if (!run) return { label: "غير نشط", detail: "لا يوجد Production Run معروف.", progress: null };
  const conclusion = String(run.conclusion || "").toLowerCase();
  const terminal = (STATUS_CONTRACT.run_terminal || {})[conclusion];
  if (terminal) return { label: terminal.label, detail: conclusion === "success" ? "اكتمل Workflow بنجاح." : `الحالة: ${conclusion}`, progress: conclusion === "success" ? 100 : null };
  const steps = (jobs || []).flatMap((job) => Array.isArray(job.steps) ? job.steps : []);
  const current = steps.find((step) => String(step.status || "") === "in_progress");
  const progress = steps.length ? Math.round(100 * steps.filter((step) => String(step.status || "") === "completed").length / steps.length) : null;
  if (current) {
    const folded = String(current.name || "").toLowerCase();
    const rule = (STATUS_CONTRACT.stage_rules || []).find((item) => (item.contains || []).some((needle) => folded.includes(String(needle).toLowerCase())));
    return { label: String(rule && rule.label || current.name || "الإنتاج الجاري"), detail: `الخطوة الحالية: ${String(current.name || "")}`, progress };
  }
  return { label: String(run.status || "غير نشط"), detail: "Workflow قيد التنفيذ.", progress };
}
async function showStatus(env, target) {
  const value = await productionState(env);
  const stage = productionStage(value.run, value.jobs);
  const rows = [[{ text: "🔄 تحديث الحالة", callback_data: "cmd:status" }, { text: "🔄 تحديث الكل", callback_data: "cmd:refresh_all" }], [{ text: "🏠 الرئيسية", callback_data: "cmd:menu" }]];
  if (value.run && String(value.run.html_url || "").startsWith("https://")) rows.splice(1, 0, [{ text: "🔗 GitHub", url: value.run.html_url }]);
  const title = `📊 حالة الإنتاج${value.run && value.run.run_number ? ` · Run #${value.run.run_number}` : ""}`;
  await updatePanel(env, target, `${title}\n\n${stage.label}${Number.isFinite(stage.progress) ? ` · ${stage.progress}%` : ""}\n${stage.detail}\n\n✅ قراءة مباشرة من GitHub · ${omanTime()} عُمان\nℹ️ قراءة فقط؛ لا تعيد Production.`, rows);
}
function bytesFromBase64(value) {
  const raw = atob(String(value || "").replace(/\s+/g, ""));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}
async function decryptState(bytes, passphrase) {
  if (new TextDecoder().decode(bytes.slice(0, 8)) !== "Salted__") throw new Error("Unsupported encrypted state envelope");
  const salt = bytes.slice(8, 16);
  const password = await crypto.subtle.importKey("raw", new TextEncoder().encode(passphrase), "PBKDF2", false, ["deriveBits"]);
  const derived = new Uint8Array(await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt, iterations: 10000 }, password, 384));
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
  const response = await fetch(`https://raw.githubusercontent.com/${repo}/control-plane-state/state/control-panel.json.enc`, { headers: { "user-agent": "isco-telegram-edge-v2" } });
  if (!response.ok) throw new Error(`Encrypted control state read failed: ${response.status}`);
  stateCache = await decryptState(new Uint8Array(await response.arrayBuffer()), secret);
  stateCacheAt = Date.now();
  return stateCache;
}
function savedItems(state) {
  return (Array.isArray(state.saved_suggestions) ? state.saved_suggestions : []).filter((x) => x && x.status === "available" && x.candidate && String(x.candidate.title || "").trim()).sort((a, b) => String(b.last_seen_at || b.saved_at || "").localeCompare(String(a.last_seen_at || a.saved_at || "")));
}
function usedItems(state) {
  return (Array.isArray(state.used_topics) ? state.used_topics : []).filter((x) => x && ["long", "short"].includes(String(x.kind || "")) && String(x.topic || "").trim()).sort((a, b) => String(b.used_at || "").localeCompare(String(a.used_at || "")));
}
function byKind(items, kind) { return items.filter((x) => String(x.kind || "") === kind); }
function libraryKindMenu(state, bucket) {
  const items = bucket === "saved" ? savedItems(state) : usedItems(state);
  const longCount = byKind(items, "long").length;
  const shortCount = byKind(items, "short").length;
  const saved = bucket === "saved";
  return {
    text: `${saved ? "📚 المحفوظة" : "✅ المستعملة"}\n\n🎬 طويل — ${longCount}\n⚡ شورت — ${shortCount}\n\n⚡ قراءة مباشرة من Edge؛ لا تنتظر GitHub Actions.`,
    rows: [[{ text: `🎬 طويل (${longCount})`, callback_data: `cmd:${bucket}-long` }], [{ text: `⚡ شورت (${shortCount})`, callback_data: `cmd:${bucket}-short` }], [{ text: "↩️ المواضيع", callback_data: "cmd:library_menu" }]],
  };
}
function pageSpec(data) {
  const match = /^cmd:(saved|used)-(long|short)(?:-page-(\d+))?$/.exec(String(data || ""));
  return match ? { bucket: match[1], kind: match[2], page: Math.max(0, Number(match[3] || 0) || 0) } : null;
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
  const lines = [`${spec.bucket === "saved" ? "📚 المحفوظة" : "✅ المستعملة"} — ${icon} ${label}`, "", `${items.length} موضوعًا — صفحة ${page + 1}/${pages}.`];
  if (!current.length) lines.push("", "لا توجد عناصر حاليًا.");
  current.forEach((item, index) => {
    if (spec.bucket === "saved") {
      const title = String(item.candidate.title || "").trim();
      rows.push([{ text: `${icon} ${title.length <= 42 ? title : `${title.slice(0, 39).trim()}…`}`, callback_data: `cmd:savedpick-${String(item.archive_id || "")}` }]);
    } else {
      lines.push("", `${start + index + 1}) ${icon} ${String(item.topic || "")}`);
      if (item.used_at) lines.push(`   ${String(item.used_at).slice(0, 10)}`);
    }
  });
  const nav = [];
  if (page > 0) nav.push({ text: "⬅️ أحدث", callback_data: `cmd:${spec.bucket}-${spec.kind}-page-${page - 1}` });
  if (page + 1 < pages) nav.push({ text: "أقدم ➡️", callback_data: `cmd:${spec.bucket}-${spec.kind}-page-${page + 1}` });
  if (nav.length) rows.push(nav);
  rows.push([{ text: spec.bucket === "saved" ? "↩️ المحفوظة" : "↩️ المستعملة", callback_data: `cmd:${spec.bucket}` }]);
  lines.push("", "⚡ هذه الصفحة من Edge. اختيار موضوع محفوظ لا يبدأ Production.");
  return { text: lines.join("\n"), rows };
}
async function showLibrary(env, target, data) {
  const state = await controlState(env);
  const panel = data === "cmd:saved" ? libraryKindMenu(state, "saved") : data === "cmd:used" ? libraryKindMenu(state, "used") : libraryPage(state, pageSpec(data));
  await updatePanel(env, target, panel.text, panel.rows);
}
async function publicProjection(env) {
  const repo = String(env.GITHUB_REPO || DEFAULT_REPO).trim();
  const response = await fetch(`https://raw.githubusercontent.com/${repo}/control-plane-state/state/telegram-status.json`, { headers: { "user-agent": "isco-telegram-edge-v2" } });
  if (!response.ok) throw new Error(`Projection read failed: ${response.status}`);
  return response.json();
}
async function showDashboard(env, target) {
  const [prod, projection, hook] = await Promise.all([productionState(env), publicProjection(env), telegram(env, "getWebhookInfo", {})]);
  const stage = productionStage(prod.run, prod.jobs);
  const e = projection && projection.editorial || {};
  const pending = Number(hook && hook.pending_update_count || 0);
  const lines = ["🏠 نداء اليقظة · لوحة التشغيل", "", `🎬 الإنتاج: ${stage.label}${Number.isFinite(stage.progress) ? ` · ${stage.progress}%` : ""}`, `📚 التحرير: محفوظة ${Number(e.saved_count || 0)} · مستعملة ${Number(e.used_count || 0)}`, `🛰️ Telegram: webhook ✅ · pending ${pending}${String(hook && hook.last_error_message || "").trim() ? " · ⚠️ خطأ مسجل" : ""}`, "", `🕒 آخر تحقق شامل: ${omanTime()} · عُمان`, "ℹ️ «تحديث الكل» قراءة فقط؛ لا يبدأ ولا يعيد أي Production Run."];
  await updatePanel(env, target, lines.join("\n"), ROOT_ROWS);
}
function isMenuText(text) { return ["🏠 ابدأ", "🎛 ابدأ", "/start", "/menu", "ابدأ", "القائمة"].includes(String(text || "").trim()); }
function fastRoute(update) {
  const cb = update && update.callback_query;
  if (cb) {
    const data = String(cb.data || "");
    if (menuRoute(data)) return { kind: "menu", data };
    if (data === "cmd:status") return { kind: "status" };
    if (data === "cmd:refresh_all") return { kind: "dashboard" };
    if (data === "cmd:saved" || data === "cmd:used" || pageSpec(data)) return { kind: "library", data };
    return null;
  }
  return isMenuText(update && update.message && update.message.text) ? { kind: "menu", data: "cmd:menu" } : null;
}
async function handleFast(route, update, env) {
  const target = actor(update);
  if (route.kind === "menu") {
    await ack(env, target.callbackId);
    const [text, rows] = menuRoute(route.data);
    await updatePanel(env, target, text, rows);
  } else if (route.kind === "status") {
    await ack(env, target.callbackId, "أتحقق من GitHub الآن…");
    await showStatus(env, target);
  } else if (route.kind === "dashboard") {
    await ack(env, target.callbackId, "أحدّث الصورة الكاملة الآن…");
    await showDashboard(env, target);
  } else {
    await ack(env, target.callbackId, "أفتح القائمة مباشرة…");
    await showLibrary(env, target, route.data);
  }
}
async function safeFast(route, update, env) {
  const target = actor(update);
  try { await handleFast(route, update, env); }
  catch (error) {
    console.error("Fast Telegram read failed", String(error && error.message || error || "unknown"));
    try { await updatePanel(env, target, `⚠️ تعذر إكمال القراءة السريعة الآن. لم يبدأ ولم يتغير أي Production Run.\n\n🕒 ${omanTime()} · عُمان`, ROOT_ROWS); } catch (_) {}
  }
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health") {
      return new Response(JSON.stringify({ ok: true, mode: "telegram-edge-control", observability: "v2", status_schema: STATUS_CONTRACT.schema_version, fast_library: Boolean(String(env.STATE_ENCRYPTION_KEY || "").trim()) }), { headers: { "content-type": "application/json" } });
    }
    if (request.method === "POST" && url.pathname === "/telegram" && secretHeaderValid(request, env)) {
      let update;
      try { update = await request.clone().json(); } catch (_) { return baseWorker.fetch(request, env, ctx); }
      if (update && Number.isInteger(update.update_id) && authorized(update, env)) {
        const route = fastRoute(update);
        if (route) {
          if (route.kind === "library" && !String(env.STATE_ENCRYPTION_KEY || "").trim()) return baseWorker.fetch(request, env, ctx);
          ctx.waitUntil(safeFast(route, update, env));
          return new Response("OK");
        }
      }
    }
    return baseWorker.fetch(request, env, ctx);
  },
};
