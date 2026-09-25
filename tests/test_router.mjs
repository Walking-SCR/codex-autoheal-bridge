import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { Readable } from "node:stream";
import test from "node:test";

import {
  classifyCompactionCapsule,
  classifyAntigravityAuthUnavailable,
  createConfig,
  createRouterServer,
  inspectCompactionForRoute,
  inspectProviderSwitchState,
  PROVIDER_SWITCH_CHOICES,
  removeProviderScopedResponseState,
  sanitizeBodyForRoute,
} from "../scripts/codex-model-router.mjs";

const headers = { "content-type": "application/json" };
const geminiRoute = { provider: "gemini" };
const claudeRoute = { provider: "claude" };
const openaiRoute = { provider: "openai" };

function body(payload) {
  return Buffer.from(JSON.stringify(payload));
}

function decode(result) {
  return JSON.parse(result.buffer.toString("utf8"));
}

test("missing compaction capsule is incompatible with Antigravity and fails closed", () => {
  const result = sanitizeBodyForRoute(
    body({ model: "gemini-3.8-flash-high", input: [{ type: "compaction", id: "cmp_1" }] }),
    headers,
    geminiRoute,
  );

  assert.equal(result.compactionGuard.action, "blocked");
  assert.equal(result.compactionGuard.incompatible[0].format, "missing");
  assert.equal(result.changed, false);
});

test("OpenAI capsule is blocked when the target is Claude through Antigravity", () => {
  const result = sanitizeBodyForRoute(
    body({
      model: "claude-sonnet-4-6",
      input: [{ type: "compaction", encrypted_content: "gAAAAABforeign" }],
    }),
    headers,
    claudeRoute,
  );

  assert.equal(result.compactionGuard.action, "blocked");
  assert.equal(result.compactionGuard.incompatible[0].format, "openai");
});

test("provider switch exposes explicit handoff and cancel choices", () => {
  assert.deepEqual(
    PROVIDER_SWITCH_CHOICES.map(({ id }) => id),
    ["handoff", "cancel"],
  );
  assert.equal(PROVIDER_SWITCH_CHOICES[0].recommended, true);
  assert.equal(PROVIDER_SWITCH_CHOICES[1].recommended, false);
  assert.equal(PROVIDER_SWITCH_CHOICES[0].label_zh, "新建目标模型任务并迁移摘要");
  assert.equal(PROVIDER_SWITCH_CHOICES[1].label_zh, "取消切换并继续原模型任务");
});

test("valid Antigravity capsule is preserved", () => {
  const payload = {
    model: "gemini-3.8-flash-high",
    input: [{ type: "compaction", encrypted_content: "cpa-ag-compact-v1:sealed" }],
  };
  const result = sanitizeBodyForRoute(body(payload), headers, geminiRoute);

  assert.equal(result.compactionGuard, null);
  assert.deepEqual(decode(result), payload);
});

test("drop_foreign removes invalid compaction and previous response linkage", () => {
  const result = sanitizeBodyForRoute(
    body({
      model: "gemini-3.8-flash-high",
      previous_response_id: "resp_foreign",
      input: [
        { type: "message", role: "user", content: "ping" },
        { type: "compaction", encrypted_content: "gAAAAABforeign" },
      ],
    }),
    headers,
    geminiRoute,
    { providerSwitchCompactionMode: "drop_foreign" },
  );

  const sanitized = decode(result);
  assert.equal(result.compactionGuard.action, "dropped");
  assert.equal(result.compactionDropped, 1);
  assert.equal("previous_response_id" in sanitized, false);
  assert.deepEqual(sanitized.input, [{ type: "message", role: "user", content: "ping" }]);
});

test("foreign Gemini carrier is blocked when targeting OpenAI", () => {
  const inspection = inspectCompactionForRoute(
    { input: [{ type: "compaction", encrypted_content: "cpa-ag-compact-v1:sealed" }] },
    openaiRoute,
  );
  assert.equal(inspection.compatible, false);
  assert.equal(inspection.incompatible[0].capsule.family, "antigravity");
});

test("identity rewrite remains limited to Antigravity routes", () => {
  const payload = {
    model: "gemini-3.8-flash-high",
    input: [{ type: "message", content: "You are Codex, an agent based on GPT-5." }],
  };
  const result = sanitizeBodyForRoute(body(payload), headers, geminiRoute);
  assert.equal(result.promptRewriteCount, 1);
  assert.equal(decode(result).input[0].content, "You are a helpful AI coding assistant.");
});

test("compaction mode rejects unknown values", () => {
  assert.throws(
    () => createConfig({ CODEX_BRIDGE_PROVIDER_SWITCH_COMPACTION_MODE: "unsafe" }),
    /Invalid provider switch compaction mode/,
  );
});

test("same-provider Responses state is preserved", () => {
  const payload = {
    model: "gpt-5.6-sol",
    previous_response_id: "resp_same_provider",
    input: [{ type: "reasoning", id: "rs_resp_same_provider" }],
  };
  const inspection = inspectProviderSwitchState(payload, openaiRoute, "openai");
  assert.equal(inspection.compatible, true);
  const result = sanitizeBodyForRoute(body(payload), headers, openaiRoute, {
    previousProvider: "openai",
  });
  assert.equal(result.providerSwitchGuard, null);
  assert.deepEqual(decode(result), payload);
});

test("cross-provider Responses state fails closed before upstream", () => {
  const payload = {
    model: "gpt-5.6-sol",
    store: false,
    previous_response_id: "resp_gemini_state",
    input: [
      { type: "message", role: "user", content: "continue" },
      { type: "reasoning", id: "rs_resp_8yuyagfzBM6X9tMPv8KXgQ0_0" },
      { type: "item_reference", item_id: "msg_gemini_1" },
    ],
  };
  const result = sanitizeBodyForRoute(body(payload), headers, openaiRoute, {
    previousProvider: "gemini",
    providerSwitchStateMode: "fail_closed",
  });
  assert.equal(result.providerSwitchGuard.action, "blocked");
  assert.equal(result.providerSwitchGuard.sourceProvider, "gemini");
  assert.equal(result.providerSwitchGuard.targetProvider, "openai");
  assert.match(result.providerSwitchGuard.reasons.join(" "), /rs_resp_/);
  assert.equal(result.changed, false);
});

test("explicit drop_foreign removes provider state but preserves ordinary messages", () => {
  const payload = {
    model: "gemini-3.8-flash-high",
    store: false,
    previous_response_id: "resp_openai_state",
    input: [
      { type: "message", role: "user", content: "continue" },
      { type: "reasoning", id: "rs_resp_openai_1", summary: [] },
      { type: "function_call", id: "fc_openai_1", name: "shell" },
      { type: "item_reference", item_id: "msg_openai_1" },
    ],
  };
  const result = sanitizeBodyForRoute(body(payload), headers, geminiRoute, {
    previousProvider: "openai",
    providerSwitchStateMode: "drop_foreign",
  });
  const sanitized = decode(result);
  assert.equal(result.providerSwitchGuard.action, "dropped");
  assert.equal(result.providerStateDropped, 4);
  assert.equal("previous_response_id" in sanitized, false);
  assert.deepEqual(sanitized.input, [{ type: "message", role: "user", content: "continue" }]);
  assert.equal(removeProviderScopedResponseState(payload).dropped >= 4, true);
});

test("provider switch state mode rejects unknown values", () => {
  assert.throws(
    () => createConfig({ CODEX_BRIDGE_PROVIDER_SWITCH_STATE_MODE: "unsafe" }),
    /Invalid provider switch state mode/,
  );
  assert.equal(
    createConfig({ CODEX_BRIDGE_PROVIDER_SWITCH_STATE_MODE: "drop_foreign" }).providerSwitchStateMode,
    "drop_foreign",
  );
});

const poolError = JSON.stringify({
  error: {
    code: "internal_server_error",
    message: 'auth_unavailable: no auth available (providers=antigravity, model=gemini-3.8-flash-high; last upstream error: {"reason":"VALIDATION_REQUIRED","validation_url":"private-flow-token"})',
  },
});

function makeBlockedPool(t, now = Date.now()) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pool-test-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  for (const [name, httpStatus, reason] of [
    ["one", 403, "VALIDATION_REQUIRED"],
    ["two", 403, "VALIDATION_REQUIRED"],
    ["three", 429, "quota"],
  ]) {
    const authId = `${name}.json`;
    fs.writeFileSync(path.join(dir, authId), JSON.stringify({ type: "antigravity", disabled: false }));
    fs.writeFileSync(path.join(dir, `${name}.cds`), JSON.stringify({
      provider: "antigravity",
      auth_id: authId,
      records: [{
        model: "gemini-3.8-flash-high",
        status: "cooling",
        next_retry_after: new Date(now + 30 * 60 * 1000).toISOString(),
        reason,
        last_error: {
          http_status: httpStatus,
          message: reason === "quota" ? "quota exhausted" :
            '{"reason":"VALIDATION_REQUIRED","validation_url":"private-flow-token"}',
        },
      }],
    }));
  }
  return dir;
}

test("classifies only a fully cooling Antigravity pool and never returns private URLs", (t) => {
  const now = Date.now();
  const dir = makeBlockedPool(t, now);
  const result = classifyAntigravityAuthUnavailable(
    503, Buffer.from(poolError), dir, "gemini-3.8-flash-high", now,
  );
  assert.equal(result.enabled, 3);
  assert.equal(result.validation, 2);
  assert.equal(result.quota, 1);
  assert.equal(JSON.stringify(result).includes("private-flow-token"), false);
  assert.equal(classifyAntigravityAuthUnavailable(
    503, Buffer.from(poolError), dir, "gemini-3.8-flash-high", now + 31 * 60 * 1000,
  ), null);
  assert.equal(classifyAntigravityAuthUnavailable(
    503, Buffer.from('{"error":{"message":"No capacity available"}}'), dir,
    "gemini-3.8-flash-high", now,
  ), null);
  assert.equal(classifyAntigravityAuthUnavailable(
    503, Buffer.from(poolError), dir, "gpt-6-luna", now,
  ), null);
});

test("one healthy or unknown credential keeps the original upstream failure", (t) => {
  const now = Date.now();
  const dir = makeBlockedPool(t, now);
  fs.writeFileSync(path.join(dir, "healthy.json"), JSON.stringify({ type: "antigravity" }));
  assert.equal(classifyAntigravityAuthUnavailable(
    503, Buffer.from(poolError), dir, "gemini-3.8-flash-high", now,
  ), null);
});

test("Gemini pool exhaustion is one sanitized, non-retryable handoff error", async (t) => {
  const dir = makeBlockedPool(t);
  const helper = path.join(dir, "client-key.py");
  fs.writeFileSync(helper, 'process.stdout.write("dummy-local-key");\n');
  let attempts = 0;
  const server = createRouterServer({
    config: { authDir: dir, helper, helperPython: process.execPath },
    log: () => {},
    requestImpl: (_target, _options, callback) => {
      const request = new EventEmitter();
      request.setTimeout = () => {};
      request.end = (sentBody) => {
        attempts += 1;
        const input = JSON.parse(sentBody.toString("utf8"));
        const reply = input.input === "capacity"
          ? JSON.stringify({ error: { message: "No capacity available for model" } })
          : poolError;
        const upstream = Readable.from([Buffer.from(reply)]);
        upstream.statusCode = 503;
        upstream.headers = { "content-type": "application/json", "content-length": String(Buffer.byteLength(reply)) };
        queueMicrotask(() => callback(upstream));
      };
      return request;
    },
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const response = await fetch(`http://127.0.0.1:${server.address().port}/v1/responses`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ model: "gemini-3.8-flash-high", input: "ping" }),
  });
  const text = await response.text();
  const payload = JSON.parse(text);
  assert.equal(response.status, 400);
  assert.equal(attempts, 1);
  assert.equal(payload.error.code, "antigravity_account_action_required");
  assert.equal(payload.retryable, false);
  assert.equal(payload.handoff_mode, "new_task_with_plain_text_summary");
  assert.equal(text.includes("private-flow-token"), false);
  assert.equal(text.includes("@gmail.com"), false);
  const capacity = await fetch(`http://127.0.0.1:${server.address().port}/v1/responses`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ model: "gemini-3.8-flash-high", input: "capacity" }),
  });
  assert.equal(capacity.status, 503);
  assert.match(await capacity.text(), /No capacity available/);
  const native = await fetch(`http://127.0.0.1:${server.address().port}/v1/responses`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ model: "gpt-6-luna", input: "ping" }),
  });
  assert.equal(native.status, 503);
  assert.equal(attempts, 3);
});
