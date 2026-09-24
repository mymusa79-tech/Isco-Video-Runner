const DEFAULT_REPO = "mymusa79-tech/Isco-Video-Runner";
const CONTROL_WORKFLOW = "telegram-clean-v2-control.yml";
const CONFIRM_TEXT = "تأكيد الإنتاج";

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
      [{ text: "🎬 Long فقط", callback_data: "scope:long" }],
      [{ text: "🎬➕⚡ Long + Short", callback_data: "scope:bundle" }],
      [{ text: "⚡ Short فقط", callback_data: "scope:short" }],
    ],
  };
}

async function sendScopeMenu(env, chatId) {
  await telegram(env, "sendMessage", {
    chat_id: chatId,
    text: "🧭 Clean V2 Editorial Lite\n\nاختر نطاق البحث. البحث والاختيار لا يبدأان Production.",
    reply_markup: scopeKeyboard(),
  });
}

async function clearCallbackKeyboard(env, current) {
  if (!current.chat || !current.messageId) return;
  try {
    await telegram(env, "editMessageReplyMarkup", {
      chat_id: current.chat,
      message_id: Number(current.messageId),
      reply_markup: { inline_keyboard: [] },
    });
  } catch (_) {
    // Visual cleanup is best-effort; server-side session closure remains authoritative.
  }
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

function isLocalMenuText(text) {
  return ["/start", "/menu", "/research", "بحث"].includes(String(text || "").trim());
}

function isStatsText(text) {
  return ["/stats", "stats", "إحصائيات", "الاحصائيات", "الإحصائيات"].includes(String(text || "").trim());
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
      if (current.data.startsWith("scope:")) {
        ctx.waitUntil(answerCallback(env, current.callbackId, "🔎 بدأ البحث…"));
      } else if (current.data.startsWith("pick:")) {
        ctx.waitUntil(answerCallback(env, current.callbackId, "✅ أسجل الاختيار…"));
        ctx.waitUntil(clearCallbackKeyboard(env, current));
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
    if (isLocalMenuText(text)) {
      ctx.waitUntil(sendScopeMenu(env, current.chat));
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
        text: "استخدم /research لطلب 3 أفكار جديدة، و/stats لإحصائيات القناة. بدء الإنتاج يتطلب العبارة الدقيقة «تأكيد الإنتاج».",
      }),
    );
    return new Response("OK");
  },
};
