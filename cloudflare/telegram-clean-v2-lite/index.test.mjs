import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const workerSource = await readFile(new URL("./index.js", import.meta.url), "utf8");
const workerModule = await import(`data:text/javascript;base64,${Buffer.from(workerSource).toString("base64")}`);

const {
  default: worker,
  localLibraryRoute,
  releaseLibraryRecords,
  renderLibraryMenu,
  sameTopic,
  savedLibraryItems,
  scopeKeyboard,
} = workerModule;

function sampleState() {
  return {
    schema_version: 1,
    ideas: [
      { idea_id: "long-1", title: "كيف نستعيد التركيز بعد التشتت؟" },
      { idea_id: "short-1", title: "لماذا نؤجل المهام المهمة؟" },
      { idea_id: "podcast-1", title: "لماذا لا تحل إدارة الوقت مشكلة الأولويات؟" },
    ],
    sessions: {
      long: {
        session_id: "long",
        scope: "long",
        idea_ids: ["long-1"],
        created_at: "2026-09-29T20:00:00Z",
      },
      short: {
        session_id: "short",
        scope: "short",
        idea_ids: ["short-1"],
        created_at: "2026-09-29T21:00:00Z",
      },
      podcast: {
        session_id: "podcast",
        scope: "podcast",
        idea_ids: ["podcast-1"],
        created_at: "2026-09-29T22:00:00Z",
      },
    },
    requests: {},
    current_request_id: null,
  };
}

function callbackUpdate(updateId, data) {
  return {
    update_id: updateId,
    callback_query: {
      id: `callback-${updateId}`,
      from: { id: 123 },
      message: { message_id: updateId, chat: { id: 123 } },
      data,
    },
  };
}

async function invoke(update, env) {
  const pending = [];
  const response = await worker.fetch(
    new Request("https://worker.example/telegram", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "X-Telegram-Bot-Api-Secret-Token": env.TELEGRAM_WEBHOOK_SECRET,
      },
      body: JSON.stringify(update),
    }),
    env,
    { waitUntil(promise) { pending.push(Promise.resolve(promise)); } },
  );
  await Promise.all(pending);
  return response;
}

test("scope menu matches the long, short, and podcast backend contract", () => {
  const callbacks = scopeKeyboard().inline_keyboard.flat().map((button) => button.callback_data);
  assert.deepEqual(callbacks, ["scope:long", "scope:short", "scope:podcast", "main:home"]);
  assert.ok(!callbacks.includes("scope:bundle"));
});

test("library helpers preserve type split, research order, and used filtering", () => {
  const releases = releaseLibraryRecords([
    {
      draft: false,
      tag_name: "clean-v2-final-short-example",
      name: "Clean V2 Short — لماذا نؤجل المهام المهمة؟",
      published_at: "2026-09-29T23:00:00Z",
    },
  ]);
  const state = sampleState();
  assert.equal(sameTopic("لماذا نؤجل المهام المهمة؟", "لماذا نؤجل المهام المهمة"), true);
  assert.equal(savedLibraryItems(state, "long", releases).length, 1);
  assert.equal(savedLibraryItems(state, "short", releases).length, 0);
  assert.equal(savedLibraryItems(state, "podcast", releases)[0].idea_id, "podcast-1");
  const menu = renderLibraryMenu(state, releases, "saved");
  assert.match(menu.text, /🎬 طويل — 1/);
  assert.match(menu.text, /⚡ شورت — 0/);
  assert.match(menu.text, /🎙️ بودكاست — 1/);
});

test("only read-only library callbacks are classified for local handling", () => {
  assert.equal(localLibraryRoute("main:saved"), null);
  assert.deepEqual(localLibraryRoute("library:saved"), { bucket: "saved", kind: "" });
  assert.deepEqual(localLibraryRoute("library:saved:podcast"), { bucket: "saved", kind: "podcast" });
  assert.deepEqual(localLibraryRoute("library:used:short"), { bucket: "used", kind: "short" });
  assert.equal(localLibraryRoute("savedpick:podcast:podcast-1"), null);
  assert.equal(localLibraryRoute("confirm:req-1"), null);
});

test("live routing keeps research browsing local and dispatches selection and confirmation", async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  const state = sampleState();
  const releases = [{
    draft: false,
    tag_name: "clean-v2-final-short-example",
    name: "Clean V2 Short — لماذا نؤجل المهام المهمة؟",
    published_at: "2026-09-29T23:00:00Z",
  }];
  const env = {
    TELEGRAM_BOT_TOKEN: "telegram-token",
    TELEGRAM_CHAT_ID: "123",
    TELEGRAM_WEBHOOK_SECRET: "webhook-secret",
    GITHUB_CONTROL_TOKEN: "github-token",
    GITHUB_REPO: `owner/live-route-contract-${Date.now()}`,
  };

  globalThis.fetch = async (input, init = {}) => {
    const url = String(input);
    const body = init.body ? JSON.parse(String(init.body)) : null;
    calls.push({ url, body });
    if (url.includes("api.telegram.org")) {
      return new Response(JSON.stringify({ ok: true, result: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    if (url.includes("/contents/state/telegram-clean-v2.json")) {
      return new Response(JSON.stringify({ content: Buffer.from(JSON.stringify(state)).toString("base64") }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    if (url.includes("/releases?per_page=100")) {
      return new Response(JSON.stringify(releases), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    if (url.includes("/actions/workflows/")) return new Response(null, { status: 204 });
    throw new Error(`Unexpected fetch: ${url}`);
  };

  try {
    await invoke(callbackUpdate(1, "library:saved"), env);
    await invoke(callbackUpdate(2, "library:saved:podcast"), env);

    let workflowCalls = calls.filter((call) => call.url.includes("/actions/workflows/"));
    assert.equal(workflowCalls.length, 0, "browsing must not consume serialized workflow runs");
    const sentMessages = calls.filter((call) => call.url.endsWith("/sendMessage"));
    assert.match(sentMessages[0].body.text, /📚 المحفوظات/);
    const podcastButtons = sentMessages[1].body.reply_markup.inline_keyboard.flat();
    assert.ok(podcastButtons.some((button) => button.callback_data === "savedpick:podcast:podcast-1"));

    await invoke(callbackUpdate(3, "savedpick:podcast:podcast-1"), env);
    workflowCalls = calls.filter((call) => call.url.includes("/actions/workflows/"));
    assert.equal(workflowCalls.length, 1, "saved selection remains an authoritative state transition");
    const selectionAck = calls
      .filter((call) => call.url.endsWith("/answerCallbackQuery"))
      .find((call) => call.body.callback_query_id === "callback-3");
    assert.match(selectionAck.body.text, /انتظر رسالة/);
    assert.ok(!calls.some((call) => call.url.endsWith("/editMessageReplyMarkup")));

    await invoke(callbackUpdate(4, "confirm:req-current"), env);
    workflowCalls = calls.filter((call) => call.url.includes("/actions/workflows/"));
    assert.equal(workflowCalls.length, 2, "confirmation remains a separate authoritative transition");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("production history and resume callbacks reach the durable control owner", async () => {
  const originalFetch = globalThis.fetch;
  const dispatches = [];
  const env = {
    TELEGRAM_BOT_TOKEN: "test-token", TELEGRAM_CHAT_ID: "123",
    TELEGRAM_WEBHOOK_SECRET: "test-secret", GITHUB_CONTROL_TOKEN: "test-token",
    GITHUB_REPO: "owner/resume-route-contract",
  };
  globalThis.fetch = async (input, init = {}) => {
    const url = String(input);
    if (url.includes("api.telegram.org")) {
      return Response.json({ ok: true, result: true });
    }
    assert.match(url, /actions\/workflows\/telegram-clean-v2-control.yml\/dispatches$/);
    dispatches.push(JSON.parse(String(init.body)));
    return new Response(null, { status: 204 });
  };
  try {
    for (const [i, data] of ["main:saved", "historyscope:long", "historyscope:short", "historyscope:podcast", "history:req-62", "resume:req-62", "resumevoice:req-62", "restart:req-62"].entries()) {
      const update = callbackUpdate(100 + i, data);
      const response = await invoke(update, env);
      assert.equal(response.status, 200);
      const body = dispatches.at(-1);
      assert.equal(body.ref, "main");
      const forwarded = JSON.parse(Buffer.from(body.inputs.webhook_update_b64, "base64").toString("utf8"));
      assert.equal(forwarded.callback_query.data, data);
    }
    await invoke({ update_id: 107, message: { from: { id: 123 }, chat: { id: 123 }, text: "/saved" } }, env);
    assert.equal(dispatches.length, 9);
    const message = JSON.parse(Buffer.from(dispatches.at(-1).inputs.webhook_update_b64, "base64").toString("utf8"));
    assert.equal(message.message.text, "/saved");
  } finally {
    globalThis.fetch = originalFetch;
  }
});
