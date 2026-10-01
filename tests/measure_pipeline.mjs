import http from "node:http";
import { Readable } from "node:stream";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createRouterServer } from "../scripts/codex-model-router.mjs";

async function runBenchmark() {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-bench-"));
  const helper = path.join(tmpDir, "client-key.py");
  fs.writeFileSync(helper, 'process.stdout.write("test-key");\n');

  // Create an upstream HTTP server simulating OpenAI / third-party SSE streaming
  const upstreamServer = http.createServer((req, res) => {
    res.writeHead(200, {
      "content-type": "text/event-stream",
      "cache-control": "no-cache",
      connection: "keep-alive",
    });

    // Simulate first SSE frame after 40ms (TTFB)
    setTimeout(() => {
      res.write('data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n');
    }, 40);

    // Simulate first text token after 90ms (TTFT)
    setTimeout(() => {
      res.write('data: {"choices":[{"delta":{"content":"Hello, how can I help you today?"}}]}\n\n');
    }, 90);

    // Finish stream after 150ms
    setTimeout(() => {
      res.write("data: [DONE]\n\n");
      res.end();
    }, 150);
  });

  await new Promise((resolve) => upstreamServer.listen(0, "127.0.0.1", resolve));
  const upstreamPort = upstreamServer.address().port;

  // Create router server pointing to upstream
  const routerServer = createRouterServer({
    config: {
      authDir: tmpDir,
      helper,
      helperPython: process.execPath,
      gptNativeBaseUrl: `http://127.0.0.1:${upstreamPort}/backend-api/codex`,
    },
    log: () => {},
  });

  await new Promise((resolve) => routerServer.listen(0, "127.0.0.1", resolve));
  const routerPort = routerServer.address().port;
  const routerBase = `http://127.0.0.1:${routerPort}`;

  console.log("================================================================================");
  console.log(" 全链路耗时监察与连接复用实测 (Router Performance & Latency Telemetry)");
  console.log("================================================================================");

  const testTurns = [
    { name: "Turn 1 (首次冷启动 - 建立首个连接)", waitMs: 0 },
    { name: "Turn 2 (连续调用 - 间隔 500ms，在 Keep-Alive 窗口内)", waitMs: 500 },
    { name: "Turn 3 (多轮交互 - 间隔 2000ms，在 Keep-Alive 窗口内)", waitMs: 2000 },
    { name: "Turn 4 (思考间隔 - 间隔 6000ms，超过默认 5s 空闲窗口)", waitMs: 6000 },
    { name: "Turn 5 (重连后再次复用 - 间隔 300ms)", waitMs: 300 },
  ];

  for (let i = 0; i < testTurns.length; i++) {
    const turn = testTurns[i];
    if (turn.waitMs > 0) {
      process.stdout.write(`\n⏳ 等待真实交互间隔 ${turn.waitMs} ms ... `);
      await new Promise((r) => setTimeout(r, turn.waitMs));
      console.log("发送请求");
    }

    const tStart = performance.now();
    const res = await fetch(`${routerBase}/v1/responses`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        model: "gpt-5.6-sol",
        input: [{ type: "message", role: "user", content: `Turn test ${i + 1}` }],
      }),
    });
    const body = await res.text();
    const tEnd = performance.now();
    const elapsed = tEnd - tStart;

    console.log(`\n▶ [${turn.name}]`);
    console.log(`  HTTP 状态: ${res.status} | 客户端感知总延迟: ${elapsed.toFixed(1)} ms`);
  }

  // Fetch full metrics snapshot from /__codex_bridge_metrics
  const metricsRes = await fetch(`${routerBase}/__codex_bridge_metrics`);
  const metrics = await metricsRes.json();

  console.log("\n================================================================================");
  console.log(" 汇总指标快照 (/__codex_bridge_metrics)");
  console.log("================================================================================");
  console.log(`总请求数: ${metrics.total_requests}`);
  console.log(`活跃请求数: ${metrics.active_requests}`);
  console.log(`连接复用情况: 复用 ${metrics.socket_reuse.reused_count} 次, 新建 ${metrics.socket_reuse.new_count} 次 (复用率: ${metrics.socket_reuse.reuse_rate_pct}%)`);

  console.log("\n全链路各环节耗时拆解 (毫秒):");
  console.log("┌───────────────────────┬──────────┬──────────┬──────────┬──────────┬──────────┐");
  console.log("│ 环节指标              │ 样本数   │ 最小值   │ 平均值   │ P50      │ P95      │");
  console.log("├───────────────────────┼──────────┼──────────┼──────────┼──────────┼──────────┤");

  const lats = metrics.latencies;
  const rows = [
    ["1. 入站解析 (ingress_ms)", lats.ingress_ms],
    ["2. 安全清洗 (sanitize_ms)", lats.sanitize_ms],
    ["3. 排队等待 (queue_ms)", lats.queue_ms],
    ["4. 连接握手 (connect_ms)", lats.connect_ms],
    ["5. TLS握手 (tls_ms)", lats.tls_ms],
    ["6. 握手总计 (handshake_ms)", lats.handshake_total_ms],
    ["7. 首包时间 (ttfb_ms)", lats.ttfb_ms],
    ["8. 首字Token (ttft_ms)", lats.ttft_ms],
    ["9. 端到端总计 (total_ms)", lats.total_ms],
  ];

  for (const [label, s] of rows) {
    const pad = (str, len) => String(str).padEnd(len);
    console.log(
      `│ ${label.padEnd(21)} │ ${pad(s.count, 8)} │ ${pad(s.min, 8)} │ ${pad(s.avg, 8)} │ ${pad(s.p50, 8)} │ ${pad(s.p95, 8)} │`
    );
  }
  console.log("└───────────────────────┴──────────┴──────────┴──────────┴──────────┴──────────┘");

  // Clean up
  await new Promise((resolve) => routerServer.close(resolve));
  await new Promise((resolve) => upstreamServer.close(resolve));
  fs.rmSync(tmpDir, { recursive: true, force: true });
}

runBenchmark().catch((err) => {
  console.error("Benchmark failed:", err);
  process.exit(1);
});
