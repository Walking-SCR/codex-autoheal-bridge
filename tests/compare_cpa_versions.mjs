import http from "node:http";
import fs from "node:fs";
import { performance } from "node:perf_hooks";

const API_KEY = "sk-codex-local-bridge-20260906-4d91a1b8f0e74c5d8a2f6b3e1c9d7a5f";

// Realistic Codex tools schema sample (complex nested JSON schemas)
function generateCodexTools(count = 15) {
  const tools = [];
  for (let i = 0; i < count; i++) {
    tools.push({
      type: "function",
      function: {
        name: `tool_codex_action_${i}`,
        description: `Perform an automated workspace action #${i} including file search, AST analysis, refactoring, and code validation.`,
        parameters: {
          type: "object",
          properties: {
            command: { type: "string", description: "The shell command to execute" },
            working_directory: { type: "string", description: "Target directory" },
            timeout_ms: { type: "number", description: "Execution timeout in milliseconds" },
            options: {
              type: "object",
              properties: {
                env: { type: "object", additionalProperties: { type: "string" } },
                retry: { type: "boolean" },
                max_lines: { type: "integer", minimum: 1, maximum: 1000 },
                filters: {
                  type: "array",
                  items: {
                    type: "object",
                    properties: {
                      pattern: { type: "string" },
                      case_sensitive: { type: "boolean" }
                    },
                    required: ["pattern"]
                  }
                }
              },
              required: ["retry"]
            }
          },
          required: ["command"]
        }
      }
    });
  }
  return tools;
}

const TOOLS = generateCodexTools(18);

async function requestStream(port, payload) {
  const tStart = performance.now();
  let tFirstByte = null;
  let tFirstToken = null;
  let totalBytes = 0;
  let chunkCount = 0;

  return new Promise((resolve, reject) => {
    const postData = JSON.stringify(payload);
    const req = http.request(
      {
        hostname: "127.0.0.1",
        port,
        path: "/v1/responses",
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": `Bearer ${API_KEY}`,
          "Accept": "text/event-stream"
        }
      },
      (res) => {
        res.on("data", (chunk) => {
          const now = performance.now();
          if (tFirstByte === null) {
            tFirstByte = now - tStart;
          }
          totalBytes += chunk.length;
          chunkCount++;
          const str = chunk.toString();
          if (tFirstToken === null && (str.includes("text") || str.includes("delta") || str.includes("content") || str.includes("reasoning"))) {
            tFirstToken = now - tStart;
          }
        });

        res.on("end", () => {
          const tEnd = performance.now();
          resolve({
            statusCode: res.statusCode,
            ttfb: tFirstByte ?? (tEnd - tStart),
            ttft: tFirstToken ?? (tEnd - tStart),
            totalTime: tEnd - tStart,
            totalBytes,
            chunkCount
          });
        });
      }
    );

    req.on("error", reject);
    req.write(postData);
    req.end();
  });
}

async function runBenchmark() {
  console.log("================================================================================");
  console.log(" CLIProxyAPI v7.3.8 vs v7.3.20 实测性能与响应速度对比基准");
  console.log("================================================================================");
  console.log("对比目标:");
  console.log("  - v7.3.8 (升级前旧版本) : 运行于 http://127.0.0.1:8327");
  console.log("  - v7.3.20 (升级后最新版): 运行于 http://127.0.0.1:8317");
  console.log("测试场景: 模拟 Codex 真实请求（携带 18 个复杂 Tool Schema + 系统上下文）\n");

  const versions = [
    { name: "v7.3.8 (更新前)", port: 8327 },
    { name: "v7.3.20 (更新后)", port: 8317 }
  ];

  // 1. 预热连接与模型缓存
  console.log("正在预热双方实例与模型上下文...");
  for (const v of versions) {
    try {
      await requestStream(v.port, {
        model: "gemini-3.8-flash-high",
        stream: true,
        input: [{ type: "message", role: "user", content: "hi" }]
      });
      console.log(`  ✓ ${v.name} 预热就绪`);
    } catch (e) {
      console.error(`  ✗ ${v.name} 预热失败:`, e.message);
    }
  }

  // 2. 多轮并发与连续请求基准实测
  const ROUNDS = 5;
  const results = {
    8327: [],
    8317: []
  };

  console.log(`\n开始执行 ${ROUNDS} 轮对照测试 (Codex 复杂多工具长提示)...`);

  for (let r = 1; r <= ROUNDS; r++) {
    console.log(`\n▶ [Round ${r}/${ROUNDS}]`);
    for (const v of versions) {
      const payload = {
        model: "gemini-3.8-flash-high",
        stream: true,
        tools: TOOLS,
        input: [
          {
            type: "message",
            role: "user",
            content: `Round ${r}: Please answer with exactly one word: 'READY'`
          }
        ]
      };

      const res = await requestStream(v.port, payload);
      results[v.port].push(res);
      console.log(`  ${v.name}: TTFB=${res.ttfb.toFixed(1)}ms | 首字/首Token(TTFT)=${res.ttft.toFixed(1)}ms | 总耗时=${res.totalTime.toFixed(1)}ms | 吞吐分片=${res.chunkCount}`);
      // 短暂休眠 600ms 模拟连续交互
      await new Promise(res => setTimeout(res, 600));
    }
  }

  // 3. 统计汇总
  function calcStats(arr, key) {
    const vals = arr.map(x => x[key]).sort((a, b) => a - b);
    const avg = vals.reduce((s, x) => s + x, 0) / vals.length;
    const p50 = vals[Math.floor(vals.length * 0.5)];
    const p95 = vals[Math.floor(vals.length * 0.95)];
    return { avg, p50, p95, min: vals[0], max: vals[vals.length - 1] };
  }

  const statsOld = {
    ttfb: calcStats(results[8327], "ttfb"),
    ttft: calcStats(results[8327], "ttft"),
    total: calcStats(results[8327], "totalTime")
  };

  const statsNew = {
    ttfb: calcStats(results[8317], "ttfb"),
    ttft: calcStats(results[8317], "ttft"),
    total: calcStats(results[8317], "totalTime")
  };

  console.log("\n================================================================================");
  console.log(" 实测数据对比汇总 (A/B Test Benchmark Summary)");
  console.log("================================================================================");
  console.log("┌──────────────────────┬────────────────┬────────────────┬────────────────┐");
  console.log("│ 性能指标 (毫秒)      │ v7.3.8 (更新前)│ v7.3.20(更新后)│ 提速幅度       │");
  console.log("├──────────────────────┼────────────────┼────────────────┼────────────────┤");

  const metrics = [
    ["首包响应 (TTFB - 平均)", statsOld.ttfb.avg, statsNew.ttfb.avg],
    ["首包响应 (TTFB - P50)",  statsOld.ttfb.p50, statsNew.ttfb.p50],
    ["首字Token (TTFT - 平均)", statsOld.ttft.avg, statsNew.ttft.avg],
    ["首字Token (TTFT - P50)",  statsOld.ttft.p50, statsNew.ttft.p50],
    ["端到端完成 (Total - 平均)", statsOld.total.avg, statsNew.total.avg],
    ["端到端完成 (Total - P50)",  statsOld.total.p50, statsNew.total.p50],
  ];

  for (const [name, oldVal, newVal] of metrics) {
    const diff = oldVal - newVal;
    const pct = ((diff / oldVal) * 100).toFixed(1);
    const speedup = diff > 0 ? `快 ${diff.toFixed(1)}ms (${pct}%) 🚀` : `平齐 (${pct}%)`;
    console.log(`│ ${name.padEnd(20)} │ ${oldVal.toFixed(1).padStart(12)}ms │ ${newVal.toFixed(1).padStart(12)}ms │ ${speedup.padEnd(14)} │`);
  }
  console.log("└──────────────────────┴────────────────┴────────────────┴────────────────┘");
}

runBenchmark().catch(console.error);
