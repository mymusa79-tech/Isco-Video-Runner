import base from "./index.js";

const DEFAULT_CHANNEL_ID = "UC_fmWGRen6QUQNd4Dj80MgA";
const START_TEXT = "🏠 ابدأ";
const CONFIRM_TEXT = "تأكيد الإنتاج";

const ROOT_ROWS = [
  [{ text: "🔎 البحث", callback_data: "cmd:search_menu", style: "primary" }],
  [{ text: "📚 المواضيع", callback_data: "cmd:library_menu" }],
  [{ text: "🎁 آخر إنتاج", callback_data: "cmd:last_delivery" }],
  [{ text: "📊 الحالة", callback_data: "cmd:status" }],
  [{ text: "📈 الإحصائيات", callback_data: "cmd:stats_menu", style: "primary" }],
];

const SEARCH_ROWS = [
  [{ text: "🎬 بحث حلقة · 3 خيارات", callback_data: "cmd:topic", style: "primary" }],
  [{ text: "⚡ بحث شورت · 3 خيارات", callback_data: "cmd:short", style: "primary" }],
  [{ text: "↩️ الرئيسية", callback_data: "cmd:menu" }],
];

const LIBRARY_ROWS = [
  [{ text: "📚 المحفوظة", callback_data: "cmd:saved" }],
  [{ text: "✅ المستعملة", callback_data: "cmd:used" }],
  [{ text: "↩️ الرئيسية", callback_data: "cmd:menu" }],
];

const STATS_ROWS = [
  [{ text: "🎬 آخر فيديو", callback_data: "cmd:stats_last_long", style: "primary" }],
  [{ text: "⚡ آخر Short", callback_data: "cmd:stats_last_short", style: "primary" }],
  [{ text: "🗓️ اليوم", callback_data: "cmd:stats_today" }],
  [{ text: "📅 آخر 7 أيام", callback_data: "cmd:stats_week" }],
  [{ text: "🌐 نظرة عامة", callback_data: "cmd:stats_overview" }],
  [{ text: "↩️ الرئيسية", callback_data: "cmd:menu" }],
];

function inline(rows) {
  return { inline_keyboard: rows };
}

function rootText() {
  return [
    "🏠 نداء اليقظة",
    "",
    "ماذا تريد أن تفعل الآن؟",
    "",
    "🔎 البحث — يولّد 3 خيارات واضحة ثم تنتقي واحدًا.",
    "📚 المواضيع — محفوظة أو مستعملة.",
    "🎁 آخر إنتاج — الحزمة والروابط.",
    "📊 الحالة — أين يقف النظام الآن.",
    "📈 الإحصائيات — بطاقات حية من YouTube.",
    "",
    `🔐 Production لا يبدأ من أي زر هنا. بعد اعتماد موضوع محدد اكتب حرفيًا «${CONFIRM_TEXT}».`,
    "🔒 النشر والجدولة في YouTube يبقيان يدويين.",
  ].join("\n");
}

function searchText() {
  return [
    "🔎 بحث جديد",
    "",
    "اختر نوع المحتوى:",
    "",
    "🎬 الحلقة — بحث + ترتيب + 3 أفكار قابلة للاختيار.",
    "⚡ الشورت — 3 أفكار قصيرة مرتبة.",
    "",
    "بعد انتهاء البحث سترى 1️⃣ 2️⃣ 3️⃣ مع زر اختيار واضح لكل فكرة.",
    "لا يبدأ أي Production من هذه الشاشة.",
  ].join("\n");
}

function libraryText() {
  return [
    "📚 مكتبة المواضيع",
    "",
    "📚 المحفوظة — أفكار جيدة لم يكتمل إنتاجها بعد.",
    "✅ المستعملة — مواضيع اكتمل إنتاجها ولا تعاد في البحث.",
  ].join("\n");
}

function statsText() {
  return [
    "📈 لوحة القناة",
    "",
    "اختر البطاقة التي تريدها. سأعرض أرقام YouTube الحية مع صورة القناة أو صورة الفيديو الحقيقية.",
    "",
    "ℹ️ اليوم/7 أيام هي بوصلة تشغيلية من البيانات العامة، وليست بديلًا عن YouTube Analytics.",
  ].join("\n");
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
  try {
    await telegram(env, "answerCallbackQuery", { callback_query_id: id, ...(text ? { text } : {}) });
  } catch (_) {
    // Callback acknowledgements are UX only and never own side effects.
  }
}

async function send(env, chatId, text, rows = null) {
  const payload = { chat_id: chatId, text, disable_web_page_preview: true };
  if (rows) payload.reply_markup = inline(rows);
  return telegram(env, "sendMessage", payload);
}

async function edit(env, chatId, messageId, text, rows) {
  try {
    return await telegram(env, "editMessageText", {
      chat_id: chatId,
      message_id: messageId,
      text,
      disable_web_page_preview: true,
      reply_markup: inline(rows),
    });
  } catch (_) {
    return send(env, chatId, text, rows);
  }
}

async function sendPhotoCard(env, chatId, photo, caption, rows) {
  if (!photo) return send(env, chatId, caption, rows);
  try {
    return await telegram(env, "sendPhoto", {
      chat_id: chatId,
      photo,
      caption,
      reply_markup: inline(rows),
    });
  } catch (_) {
    return send(env, chatId, caption, rows);
  }
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

function secretHeaderValid(request, env) {
  const expected = String(env.TELEGRAM_WEBHOOK_SECRET || "").trim();
  const actual = String(request.headers.get("X-Telegram-Bot-Api-Secret-Token") || "").trim();
  return Boolean(expected) && actual === expected;
}

function authorized(update, env) {
  const target = actorAndChat(update);
  const allowedUser = String(env.TELEGRAM_ALLOWED_USER_ID || "").trim();
  const allowedChat = String(env.TELEGRAM_CHAT_ID || "").trim();
  if (!allowedUser || !allowedChat) return false;
  return String(target.userId ?? "") === allowedUser && String(target.chatId ?? "") === allowedChat;
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

function bestThumbnail(thumbnails) {
  const value = thumbnails || {};
  return String((value.maxres || value.standard || value.high || value.medium || value.default || {}).url || "");
}

async function liveYoutube(env) {
  const channelId = String(env.YOUTUBE_CHANNEL_ID || DEFAULT_CHANNEL_ID).trim();
  const channelPayload = await youtubeJson(env, "channels", {
    part: "snippet,statistics,contentDetails",
    id: channelId,
    maxResults: "1",
  });
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
      thumbnail: bestThumbnail(item.snippet && item.snippet.thumbnails),
      duration: parseDurationSeconds(item.contentDetails && item.contentDetails.duration),
      views: Number((item.statistics && item.statistics.viewCount) || 0),
      likes: Number((item.statistics && item.statistics.likeCount) || 0),
      comments: Number((item.statistics && item.statistics.commentCount) || 0),
    })).sort((a, b) => b.publishedAt.localeCompare(a.publishedAt));
  }
  const stats = channel.statistics || {};
  return {
    fetchedAt: new Date(),
    channelTitle: String((channel.snippet && channel.snippet.title) || "نداء اليقظة"),
    channelThumbnail: bestThumbnail(channel.snippet && channel.snippet.thumbnails),
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
  return new Intl.DateTimeFormat("ar-OM", {
    timeZone: "Asia/Muscat",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function periodStartUtc(days, now = new Date()) {
  const shifted = new Date(now.getTime() + 4 * 3600 * 1000);
  const y = shifted.getUTCFullYear();
  const m = shifted.getUTCMonth();
  const d = shifted.getUTCDate() - Math.max(0, days - 1);
  return new Date(Date.UTC(y, m, d, 0, 0, 0) - 4 * 3600 * 1000);
}

function statsBackRows(url = "") {
  const rows = [];
  if (url) rows.push([{ text: "▶️ فتح على YouTube", url, style: "primary" }]);
  rows.push([{ text: "↩️ الإحصائيات", callback_data: "cmd:stats_menu" }]);
  rows.push([{ text: "🏠 الرئيسية", callback_data: "cmd:menu" }]);
  return rows;
}

async function sendStatsCard(env, chatId, kind) {
  const live = await liveYoutube(env);
  if (kind === "stats_overview") {
    const subs = live.hiddenSubscribers ? "مخفية" : formatNum(live.subscribers);
    const caption = [
      `📊 ${live.channelTitle} · نظرة عامة`,
      "",
      `👥 المشتركون   ${subs}`,
      `👁️ المشاهدات   ${formatNum(live.views)}`,
      `🎞️ الفيديوهات   ${formatNum(live.videoCount)}`,
      "",
      `🔄 تحديث حي ${omanTime(live.fetchedAt)} · عُمان`,
      "ℹ️ بيانات عامة مباشرة من YouTube.",
    ].join("\n");
    await sendPhotoCard(env, chatId, live.channelThumbnail, caption, statsBackRows());
    return;
  }

  if (kind === "stats_last_long" || kind === "stats_last_short") {
    const wantShort = kind === "stats_last_short";
    const item = live.videos.find((video) => (video.duration > 0 && video.duration <= 180) === wantShort);
    if (!item) {
      await sendPhotoCard(
        env,
        chatId,
        live.channelThumbnail,
        `📈 آخر ${wantShort ? "Short" : "فيديو طويل"}\n\nلم أجد عنصرًا حديثًا مطابقًا ضمن آخر الرفعات.`,
        statsBackRows(),
      );
      return;
    }
    const caption = [
      `📈 آخر ${wantShort ? "Short" : "فيديو طويل"}`,
      "",
      `🎬 ${item.title}`,
      "",
      `👁️ ${formatNum(item.views)}   👍 ${formatNum(item.likes)}   💬 ${formatNum(item.comments)}`,
      "",
      `🔄 تحديث حي ${omanTime(live.fetchedAt)} · عُمان`,
      ...(wantShort ? ["ℹ️ تصنيف Short تقريبي بالمدة ≤3 دقائق."] : []),
    ].join("\n");
    await sendPhotoCard(env, chatId, item.thumbnail || live.channelThumbnail, caption, statsBackRows(`https://youtu.be/${item.id}`));
    return;
  }

  const days = kind === "stats_today" ? 1 : 7;
  const start = periodStartUtc(days, live.fetchedAt);
  const periodVideos = live.videos.filter((video) => new Date(video.publishedAt) >= start);
  const currentViews = periodVideos.reduce((sum, video) => sum + video.views, 0);
  const currentLikes = periodVideos.reduce((sum, video) => sum + video.likes, 0);
  const shorts = periodVideos.filter((video) => video.duration > 0 && video.duration <= 180).length;
  const label = days === 1 ? "اليوم" : "آخر 7 أيام";
  const image = (periodVideos[0] && periodVideos[0].thumbnail) || live.channelThumbnail;
  const caption = [
    `📈 ${label}`,
    "",
    `🆕 رفعات الفترة   ${periodVideos.length}`,
    `⚡ Shorts تقريبًا   ${shorts}`,
    `👁️ مشاهدات الرفعات الآن   ${formatNum(currentViews)}`,
    `👍 إعجابات الرفعات الآن   ${formatNum(currentLikes)}`,
    "",
    `🔄 تحديث حي ${omanTime(live.fetchedAt)} · عُمان`,
    "ℹ️ هذه لقطة للرفعات المنشورة في الفترة، وليست YouTube Analytics المكتسبة زمنيًا.",
  ].join("\n");
  await sendPhotoCard(env, chatId, image, caption, statsBackRows());
}

function directMenu(data) {
  if (data === "cmd:menu") return [rootText(), ROOT_ROWS];
  if (data === "cmd:search_menu") return [searchText(), SEARCH_ROWS];
  if (data === "cmd:library_menu") return [libraryText(), LIBRARY_ROWS];
  if (data === "cmd:stats_menu") return [statsText(), STATS_ROWS];
  return null;
}

function isStatsLeaf(data) {
  return ["cmd:stats_last_long", "cmd:stats_last_short", "cmd:stats_today", "cmd:stats_week", "cmd:stats_overview"].includes(data);
}

function statefulAck(data) {
  const mapping = {
    "cmd:topic": "🔎 بدأ بحث الحلقة. سأرسل 3 خيارات مرقمة عند اكتماله.",
    "cmd:short": "⚡ بدأ بحث الشورت. سأرسل 3 خيارات مرقمة عند اكتماله.",
    "cmd:saved": "📚 أفتح المواضيع المحفوظة…",
    "cmd:used": "✅ أفتح سجل المواضيع المستعملة…",
    "cmd:last_delivery": "🎁 أتحقق من آخر حزمة إنتاج…",
    "cmd:status": "📊 أتحقق من حالة Control Plane الآن…",
  };
  return mapping[data] || "⚡ تم استلام الأمر.";
}

function textRoute(text) {
  const value = String(text || "").trim();
  if ([START_TEXT, "🎛 ابدأ", "/start", "/menu", "ابدأ", "القائمة"].includes(value)) return "menu";
  if (["بحث", "1", "١"].includes(value)) return "search";
  if (["المواضيع", "2", "٢"].includes(value)) return "library";
  if (["الإحصائيات", "الاحصائيات", "5", "٥"].includes(value)) return "stats";
  return "other";
}

async function intercept(request, env, ctx) {
  const url = new URL(request.url);
  if (request.method !== "POST" || url.pathname !== "/telegram") return null;
  if (!secretHeaderValid(request, env)) return null;

  let update;
  try {
    update = await request.clone().json();
  } catch (_) {
    return null;
  }
  if (!update || !Number.isInteger(update.update_id) || !authorized(update, env)) return null;
  const target = actorAndChat(update);

  if (update.callback_query) {
    const menu = directMenu(target.data);
    if (menu) {
      ctx.waitUntil((async () => {
        await answerCallback(env, target.callbackId);
        await edit(env, target.chatId, target.messageId, menu[0], menu[1]);
      })());
      return new Response("OK");
    }
    if (isStatsLeaf(target.data)) {
      ctx.waitUntil((async () => {
        await answerCallback(env, target.callbackId, "أحدّث البطاقة الآن…");
        try {
          await sendStatsCard(env, target.chatId, target.data.slice(4));
        } catch (_) {
          await send(env, target.chatId, "⚠️ تعذر تحديث بطاقة YouTube الآن. لم يتأثر البحث أو الإنتاج.", STATS_ROWS);
        }
      })());
      return new Response("OK");
    }
    if (target.data === "cmd:topic" || target.data === "cmd:short") {
      ctx.waitUntil((async () => {
        await answerCallback(env, target.callbackId, statefulAck(target.data));
        const isLong = target.data === "cmd:topic";
        await edit(
          env,
          target.chatId,
          target.messageId,
          `${isLong ? "🎬" : "⚡"} البحث قيد التنفيذ\n\nتم إرسال الطلب إلى Control Plane. عند اكتماله ستصلك 3 خيارات مرقمة مع زر اختيار واضح لكل فكرة.\n\n⏳ إذا كانت حصة Gemini المجانية مشغولة مؤقتًا فسأنتظر ضمن حد آمن وأخبرك، ولن أستخدم مسارًا مدفوعًا.\n🔐 لا يبدأ أي Production من البحث.`,
          [[{ text: "📊 الحالة", callback_data: "cmd:status" }], [{ text: "🏠 الرئيسية", callback_data: "cmd:menu" }]],
        );
      })());
      return null;
    }
    if (["cmd:saved", "cmd:used", "cmd:last_delivery", "cmd:status"].includes(target.data)) {
      ctx.waitUntil(answerCallback(env, target.callbackId, statefulAck(target.data)));
      return null;
    }
    return null;
  }

  const text = String((update.message && update.message.text) || "");
  const route = textRoute(text);
  if (route === "menu") {
    ctx.waitUntil(send(env, target.chatId, rootText(), ROOT_ROWS));
    return new Response("OK");
  }
  if (route === "search") {
    ctx.waitUntil(send(env, target.chatId, searchText(), SEARCH_ROWS));
    return new Response("OK");
  }
  if (route === "library") {
    ctx.waitUntil(send(env, target.chatId, libraryText(), LIBRARY_ROWS));
    return new Response("OK");
  }
  if (route === "stats") {
    ctx.waitUntil(send(env, target.chatId, statsText(), STATS_ROWS));
    return new Response("OK");
  }
  return null;
}

export default {
  async fetch(request, env, ctx) {
    const handled = await intercept(request, env, ctx);
    if (handled) return handled;
    return base.fetch(request, env, ctx);
  },
};
