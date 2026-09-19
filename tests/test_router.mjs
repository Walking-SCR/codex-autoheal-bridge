import assert from "node:assert/strict";
import test from "node:test";

import {
  classifyCompactionCapsule,
  createConfig,
  inspectCompactionForRoute,
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
