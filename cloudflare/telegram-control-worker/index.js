const DEFAULT_REPO = "mymusa79-tech/Isco-Video-Runner";
const DEFAULT_CHANNEL_ID = "UC_fmWGRen6QUQNd4Dj80MgA";
const WORKFLOW = "telegram-editorial-control.yml";
const START_TEXT = "🏠 ابدأ";
const CONFIRM_TEXT = "تأكيد الإنتاج";

const ROOT_ROWS = [
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

function rootText() {
  return [
    "🏠 نداء اليقظة",
    "",
    "اختر القسم الذي تحتاجه:",
    "",
    "1️⃣ 🔎 البحث — بحث جديد للحلقة أو الشورت.",
    "2️⃣ 📚 المواضيع — المحفوظة والمستعملة.",
    "3️⃣ 🎁 آخر إنتاج — آخر حزمة وروابطها.",
    "4️⃣ 📊 الحالة — وضع البحث والإنتاج الآن.",
    "5️⃣ 📈 الإحصائيات — صورة سريعة ومحدثة عن القناة.",
    "",
    `🔐 بدء الإنتاج لا يتم بزر؛ بعد اعتماد موضوع محدد اكتب حرفيًا «${CONFIRM_TEXT}».`,
    "🔒 YouTube: الرفع والنشر والجدولة يدويًا فقط.",
  ].join("\n");
}

function searchText() {
  return "🔎 البحث\n\nاختر نوع البحث. هذا يبدأ البحث فقط ولا يبدأ Production:\n\n🎬 حلقة — 3 أفكار طويلة مرتبة ومقيّمة.\n⚡ شورت — 3 أفكار قصيرة مرتبة ومقيّمة.";
}

function libraryText() {
  return "📚 المواضيع\n\nاختر القائمة التي تريد فتحها:\n\n📚 المحفوظة — أفكار جيدة لم تُنتج بعد.\n✅ المستعملة — مواضيع اكتمل إنتاجها ولا تعاد في البحث.";
}

function statsText() {
  return "📈 إحصائيات نداء اليقظة\n\nاختر الصورة التي تريدها. الأرقام تُجلب من YouTube وقت الطلب وتُعرض كبوصلة سريعة، لا كتقرير محاسبي دقيق.";
}

function inline(rows) {
  return { inline_keyboard: rows };
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

async function answerCallback(env, id, text = "") {
  if (!id) return;
  const payload = { callback_query_id: id };
  if (text) payload.text = text;
  try {
    await telegram(env, "answerCallbackQuery", payload);
  } catch (_) {
    // The edge response is a UX optimization; never turn a stale callback toast into a side effect.
  }
}

async function send(env, chatId, text, rows = null) {
  const payload = { chat_id: chatId, text, disable_web_page_preview: true };
  if (rows) payload.reply_markup = inline(rows);
  return telegram(env, "sendMessage", payload);
}

async function edit(env, chatId, messageId, text, rows) {
  return telegram(env, "editMessageText", {
    chat_id: chatId,
    message_id: messageId,
    text,
    disable_web_page_preview: true,
    reply_markup: inline(rows),
  });
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

function b64Utf8(value) {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

async function dispatchToGitHub(env, update) {
  const token = String(env.GITHUB_CONTROL_TOKEN || "").trim();
  if (!token) throw new Error("GITHUB_CONTROL_TOKEN is missing");
  const repo = String(env.GITHUB_REPO || DEFAULT_REPO).trim();
  const ref = String(env.GITHUB_REF || "main").trim();
  const payload = b64Utf8(JSON.stringify(update));
  if (payload.length > 90000) throw new Error("Telegram update is too large for workflow dispatch");
  const response = await fetch(`https://api.github.com/repos/${repo}/actions/workflows/${WORKFLOW}/dispatches`, {
    method: "POST",
    headers: {
      accept: "application/vnd.github+json",
      authorization: `Bearer ${token}`,
      "content-type": "application/json",
      "user-agent": "isco-telegram-edge-control",
      "x-github-api-version": "2022-11-28",
    },
    body: JSON.stringify({ ref, inputs: { webhook_update_b64: payload } }),
  });
  if (!response.ok) {
    throw new Error(`GitHub workflow dispatch failed: ${response.status}`);
  }
}

function githubHeaders(env) {
  const headers = { accept: "application/vnd.github+json", "user-agent": "isco-telegram-edge-control" };
  const token = String(env.GITHUB_CONTROL_TOKEN || "").trim();
  if (token) headers.authorization = `Bearer ${token}`;
  return headers;
}

async function githubJson(env, url) {
  const response = await fetch(url, { headers: githubHeaders(env) });
  if (!response.ok) throw new Error(`GitHub read failed: ${response.status}`);
  return response.json();
}

function failedLocation(jobs) {
  for (const job of jobs || []) {
    if (!["failure", "cancelled", "timed_out"].includes(String(job.conclusion || ""))) continue;
    const jobName = String(job.name || "");
    for (const step of job.steps || []) {
      if (["failure", "cancelled", "timed_out"].includes(String(step.conclusion || ""))) {
        return [jobName, String(step.name || "")];
      }
    }
    return [jobName, ""];
  }
  return ["", ""];
}

function compactRunText(run) {
  const conclusion = String(run.conclusion || "");
  const suffix = run.run_number ? ` · Run #${run.run_number}` : "";
  if (conclusion === "success") return `✅ الإنتاج مكتمل${suffix}\n\nالحزمة النهائية جاهزة.\n\nQuality Gates: راجع نتيجة التشغيل عند الحاجة.`;
  if (["failure", "timed_out"].includes(conclusion)) return `❌ فشل الإنتاج${suffix}\n\nراجع التفاصيل لمعرفة المرحلة التي توقفت عندها المحاولة.`;
  if (conclusion === "cancelled") return `⏸️ توقف التشغيل${suffix}\n\nتم إلغاء التشغيل قبل اكتماله.`;
  return `🔵 حالة الإنتاج${suffix}\n\nالحالة الحالية: ${String(run.status || "غير معروفة")}`;
}

function detailRunText(run, jobs) {
  const [job, step] = failedLocation(jobs);
  const lines = [
    `📋 تفاصيل التشغيل${run.run_number ? ` · Run #${run.run_number}` : ""}`,
    "",
    `Workflow: ${String(run.name || "Workflow")}`,
    `الحالة: ${String(run.conclusion || "غير محسومة")}`,
    `الفرع: ${String(run.head_branch || "غير معروف")}`,
    `المشغّل: ${String(run.event || "غير معروف")}`,
  ];
  if (job) {
    lines.push("", `آخر موضع توقف: ${job}`);
    if (step) lines.push(`الخطوة: ${step}`);
  }
  lines.push("", "هذه شاشة معلومات فقط؛ لا تغيّر الإنتاج أو النشر أو أي Quality/Security Gate.");
  return lines.join("\n");
}

async function handleOperationsToggle(env, target) {
  const match = /^cmd:ops(details|compact)-(\d+)-(\d+)$/.exec(target.data);
  if (!match) return false;
  const action = match[1];
  const runId = match[2];
  const boundMessageId = Number(match[3]);
  if (!Number.isSafeInteger(boundMessageId) || Number(target.messageId) !== boundMessageId) {
    await answerCallback(env, target.callbackId, "هذا الزر لم يعد مرتبطًا بهذه الرسالة");
    return true;
  }
  await answerCallback(env, target.callbackId);
  const repo = String(env.GITHUB_REPO || DEFAULT_REPO).trim();
  const run = await githubJson(env, `https://api.github.com/repos/${repo}/actions/runs/${runId}`);
  if (String(run.id || "") !== runId || (run.repository && run.repository.full_name && run.repository.full_name !== repo)) return true;
  let text;
  if (action === "details") {
    const jobsPayload = await githubJson(env, `https://api.github.com/repos/${repo}/actions/runs/${runId}/jobs?per_page=100`);
    text = detailRunText(run, Array.isArray(jobsPayload.jobs) ? jobsPayload.jobs : []);
  } else {
    text = compactRunText(run);
  }
  const next = action === "details" ? "compact" : "details";
  const label = next === "compact" ? "⬅️ ملخص" : "📋 التفاصيل";
  const rows = [[{ text: label, callback_data: `cmd:ops${next}-${runId}-${boundMessageId}` }]];
  if (String(run.html_url || "").startsWith("https://")) rows.push([{ text: "🔗 GitHub", url: run.html_url }]);
  await edit(env, target.chatId, boundMessageId, text, rows);
  return true;
}

function parseDurationSeconds(value) {
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

function formatNum(value) {
  const n = Math.max(0, Number(value || 0));
  if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
  return Math.trunc(n).toLocaleString("en-US");
}

function omanTime(date) {
  return new Intl.DateTimeFormat("ar-OM", { timeZone: "Asia/Muscat", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}

function periodStartUtc(days, now = new Date()) {
  const shifted = new Date(now.getTime() + 4 * 3600 * 1000);
  const y = shifted.getUTCFullYear();
  const m = shifted.getUTCMonth();
  const d = shifted.getUTCDate() - Math.max(0, days - 1);
  return new Date(Date.UTC(y, m, d, 0, 0, 0) - 4 * 3600 * 1000);
}

async function sendStats(env, chatId, kind) {
  const live = await liveYoutube(env);
  const backRows = [[{ text: "↩️ الإحصائيات", callback_data: "cmd:stats_menu" }], [{ text: "🏠 الرئيسية", callback_data: "cmd:menu" }]];
  if (kind === "stats_overview") {
    const subs = live.hiddenSubscribers ? "مخفية" : formatNum(live.subscribers);
    const text = `📊 إحصائيات عامة\n\n👥 المشتركون: ${subs}\n👁️ مشاهدات القناة: ${formatNum(live.views)}\n🎞️ إجمالي الفيديوهات: ${formatNum(live.videoCount)}\n\n🔄 تحديث حي: ${omanTime(live.fetchedAt)} بتوقيت عُمان\nℹ️ لوحة تشغيل سريعة وليست بديلًا عن YouTube Studio.`;
    await send(env, chatId, text, backRows);
    return;
  }
  if (kind === "stats_last_long" || kind === "stats_last_short") {
    const wantShort = kind === "stats_last_short";
    const item = live.videos.find((video) => (video.duration > 0 && video.duration <= 180) === wantShort);
    if (!item) {
      await send(env, chatId, `📈 آخر ${wantShort ? "Short" : "فيديو طويل"}\n\nلم أجد عنصرًا حديثًا مناسبًا ضمن آخر الرفعات.`, backRows);
      return;
    }
    const rows = [[{ text: "▶️ فتح على YouTube", url: `https://youtu.be/${item.id}` }], ...backRows];
    let text = `📈 آخر ${wantShort ? "Short" : "فيديو طويل"}\n\n🎬 ${item.title}\n\n👁️ ${formatNum(item.views)} مشاهدة\n👍 ${formatNum(item.likes)} إعجاب\n💬 ${formatNum(item.comments)} تعليق\n\n🔄 تحديث حي: ${omanTime(live.fetchedAt)} بتوقيت عُمان`;
    if (wantShort) text += "\nℹ️ تصنيف Short هنا تقريبي بالمدة (≤3 دقائق).";
    await send(env, chatId, text, rows);
    return;
  }
  const days = kind === "stats_today" ? 1 : 7;
  const start = periodStartUtc(days, live.fetchedAt);
  const periodVideos = live.videos.filter((video) => new Date(video.publishedAt) >= start);
  const currentViews = periodVideos.reduce((sum, video) => sum + video.views, 0);
  const shorts = periodVideos.filter((video) => video.duration > 0 && video.duration <= 180).length;
  const label = days === 1 ? "اليوم" : "آخر 7 أيام";
  const text = `📈 ${label}\n\n🆕 رفعات منشورة ضمن الفترة: ${periodVideos.length}\n⚡ منها Shorts تقريبًا: ${shorts}\n👁️ المشاهدات الحالية لهذه الرفعات: ${formatNum(currentViews)}\n\n🔄 تحديث حي: ${omanTime(live.fetchedAt)} بتوقيت عُمان\nℹ️ هذه بوصلة تقريبية؛ ليست عدد المشاهدات المكتسبة داخل الفترة مثل YouTube Analytics.`;
  await send(env, chatId, text, backRows);
}

function directMenuKind(data) {
  if (data === "cmd:menu") return [rootText(), ROOT_ROWS];
  if (data === "cmd:search_menu") return [searchText(), SEARCH_ROWS];
  if (data === "cmd:library_menu") return [libraryText(), LIBRARY_ROWS];
  if (data === "cmd:stats_menu") return [statsText(), STATS_ROWS];
  return null;
}

function isStatsLeaf(data) {
  return ["cmd:stats_last_long", "cmd:stats_last_short", "cmd:stats_today", "cmd:stats_week", "cmd:stats_overview"].includes(data);
}

function statefulCallbackAck(data) {
  if (data === "cmd:topic") return "🎬 بدأ بحث الحلقة — ستظهر 3 أفكار مرقمة للاختيار";
  if (data === "cmd:short") return "⚡ بدأ بحث الشورت — ستظهر 3 أفكار مرقمة للاختيار";
  if (data === "cmd:saved") return "📚 أفتح المواضيع المحفوظة الآن…";
  if (data === "cmd:used") return "✅ أفتح المواضيع المستعملة الآن…";
  if (data === "cmd:last_delivery") return "🎁 أفتح آخر إنتاج الآن…";
  if (data === "cmd:status") return "📊 أتحقق من الحالة الآن…";
  return "⚡ تم استلام الأمر";
}

function textRoute(text) {
  const value = String(text || "").trim();
  if ([START_TEXT, "🎛 ابدأ", "/start", "/menu", "ابدأ", "القائمة"].includes(value)) return "menu";
  if (["بحث", "1", "١"].includes(value)) return "search";
  if (["المواضيع", "2", "٢"].includes(value)) return "library";
  if (["الإحصائيات", "الاحصائيات", "5", "٥"].includes(value)) return "stats";
  if (value === CONFIRM_TEXT) return "stateful";
  return "unknown";
}

async function handleDirectCallback(update, env, target) {
  if (await handleOperationsToggle(env, target)) return true;
  const menu = directMenuKind(target.data);
  if (menu) {
    await answerCallback(env, target.callbackId);
    await send(env, target.chatId, menu[0], menu[1]);
    return true;
  }
  if (isStatsLeaf(target.data)) {
    await answerCallback(env, target.callbackId, "أحدّث الأرقام الآن…");
    try {
      await sendStats(env, target.chatId, target.data.slice(4));
    } catch (_) {
      await send(env, target.chatId, "⚠️ تعذر تحديث إحصائيات YouTube الآن. لم يتأثر البحث أو الإنتاج.", STATS_ROWS);
    }
    return true;
  }
  return false;
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health") {
      return new Response(JSON.stringify({ ok: true, mode: "telegram-edge-control" }), { headers: { "content-type": "application/json" } });
    }
    if (request.method !== "POST" || url.pathname !== "/telegram") return new Response("Not found", { status: 404 });
    if (!secretHeaderValid(request, env)) return new Response("Forbidden", { status: 403 });

    let update;
    try {
      update = await request.json();
    } catch (_) {
      return new Response("Bad JSON", { status: 400 });
    }
    if (!update || !Number.isInteger(update.update_id)) return new Response("Bad update", { status: 400 });

    const target = actorAndChat(update);
    if (!authorized(update, env)) {
      if (target.callbackId) ctx.waitUntil(answerCallback(env, target.callbackId, "غير مصرح"));
      return new Response("OK");
    }

    if (update.callback_query) {
      const direct = directMenuKind(target.data) || isStatsLeaf(target.data) || /^cmd:ops(details|compact)-\d+-\d+$/.test(target.data);
      if (direct) {
        ctx.waitUntil(handleDirectCallback(update, env, target));
        return new Response("OK");
      }
      ctx.waitUntil((async () => {
        await answerCallback(env, target.callbackId, statefulCallbackAck(target.data));
        await dispatchToGitHub(env, update);
      })().catch(async () => {
        await send(env, target.chatId, "⚠️ تعذر تمرير الأمر إلى Control Plane الآن. لم يحدث أي إنتاج أو تغيير خارجي.", ROOT_ROWS);
      }));
      return new Response("OK");
    }

    const text = String((update.message && update.message.text) || "");
    const route = textRoute(text);
    if (route === "menu") ctx.waitUntil(send(env, target.chatId, rootText(), ROOT_ROWS));
    else if (route === "search") ctx.waitUntil(send(env, target.chatId, searchText(), SEARCH_ROWS));
    else if (route === "library") ctx.waitUntil(send(env, target.chatId, libraryText(), LIBRARY_ROWS));
    else if (route === "stats") ctx.waitUntil(send(env, target.chatId, statsText(), STATS_ROWS));
    else if (route === "stateful") {
      ctx.waitUntil((async () => {
        await send(env, target.chatId, "⚡ استلمت «تأكيد الإنتاج». أتحقق الآن من الطلب المعتمد والحدود الأمنية…");
        await dispatchToGitHub(env, update);
      })().catch(async () => {
        await send(env, target.chatId, "⚠️ تعذر تمرير التأكيد إلى Control Plane. لم يبدأ أي Production Run.", ROOT_ROWS);
      }));
    } else {
      ctx.waitUntil(send(env, target.chatId, "🏠 استخدم «🏠 ابدأ» وسأفتح لك الخيارات بالترتيب.", ROOT_ROWS));
    }
    return new Response("OK");
  },
};