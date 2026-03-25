// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Tether integration — per-message intent tracking and tool-call drift enforcement.
 *
 * Flow per user message:
 *   1. message_received → commit intent (user message = goal)
 *   2. before_tool_call → log action + check drift → block if drifting
 *   3. after_tool_call  → log result for audit trail
 *   4. message_sent     → complete task + settle tokens
 *
 * Each user message starts a new Tether task. Tool calls within that message
 * are actions measured against the user's request.
 */

import type { PluginLogger } from "./index.js";
import http from "node:http";

export interface TetherConfig {
  endpoint: string;
  agentId: string;
  mode: "enforce" | "monitor";
}

interface TetherState {
  taskId: string | null;
  actionCount: number;
  registered: boolean;
}

const state: TetherState = {
  taskId: null,
  actionCount: 0,
  registered: false,
};

let config: TetherConfig | null = null;
let logger: PluginLogger | null = null;

function tetherPost(apiPath: string, body: Record<string, unknown>): Promise<Record<string, unknown> | null> {
  return new Promise((resolve) => {
    if (!config) { resolve(null); return; }
    const url = new URL(apiPath, config.endpoint);
    const data = JSON.stringify(body);
    const req = http.request(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) },
      timeout: 5000,
    }, (res) => {
      const chunks: Buffer[] = [];
      res.on("data", (c: Buffer) => chunks.push(c));
      res.on("end", () => {
        try { resolve(JSON.parse(Buffer.concat(chunks).toString())); }
        catch { resolve(null); }
      });
    });
    req.on("error", () => resolve(null));
    req.on("timeout", () => { req.destroy(); resolve(null); });
    req.end(data);
  });
}

async function ensureRegistered(): Promise<boolean> {
  if (state.registered || !config) return state.registered;
  const result = await tetherPost("/api/agents/register", {
    agentId: config.agentId,
    metadata: { source: "nemoclaw-plugin" },
  });
  if (result) {
    state.registered = true;
    logger?.info(`[tether] Agent '${config.agentId}' registered`);
  } else {
    logger?.warn("[tether] Failed to register agent — Tether may be unreachable");
  }
  return state.registered;
}

/**
 * Initialize Tether integration. Call once during plugin registration.
 */
export function initTether(cfg: TetherConfig, log: PluginLogger): void {
  config = cfg;
  logger = log;
  logger.info(`[tether] Initialized (endpoint=${cfg.endpoint}, mode=${cfg.mode})`);
}

/**
 * Hook: message_received — user sent a message. Start a new Tether task.
 */
export async function onMessageReceived(event: { content: string }): Promise<void> {
  if (!config) return;

  // Complete previous task if one exists
  if (state.taskId) {
    await completeCurrentTask("New message received");
  }

  if (!(await ensureRegistered())) return;

  // Create a new task for this message
  const taskId = `msg-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const userMessage = event.content.slice(0, 1000); // cap for intent

  const result = await tetherPost("/api/intent/commit", {
    agentId: config.agentId,
    taskId,
    intent: {
      goal: userMessage,
      constraints: [],
      expectedOutputs: [],
      driftPolicy: { mode: "block", threshold: 0.7 },
    },
  });

  if (result && !result.error) {
    state.taskId = taskId;
    state.actionCount = 0;
    logger?.info(`[tether] Intent committed: task=${taskId}`);
  } else {
    logger?.warn(`[tether] Intent commit failed: ${result?.error || "unreachable"}`);
    state.taskId = null;
  }
}

/**
 * Hook: before_tool_call — agent is about to use a tool. Log it and check drift.
 * Returns { block: true } if Tether says the action drifts too far from intent.
 */
export async function onBeforeToolCall(
  event: { toolName: string; params: Record<string, unknown> },
): Promise<{ block?: boolean; blockReason?: string } | void> {
  if (!config || !state.taskId) return;

  // Build a description of what the tool call is doing
  const paramSummary = Object.entries(event.params)
    .slice(0, 5) // cap params for readability
    .map(([k, v]) => `${k}=${typeof v === "string" ? v.slice(0, 100) : JSON.stringify(v).slice(0, 100)}`)
    .join(", ");
  const description = `Tool: ${event.toolName}(${paramSummary})`;

  const result = await tetherPost("/api/action/log", {
    agentId: config.agentId,
    taskId: state.taskId,
    action: { description },
  });

  if (!result) return; // Tether unreachable — fail open

  state.actionCount++;

  if (result.allowed === false) {
    const driftScore = result.driftScore ?? result.driftViolation;
    const message = (result as Record<string, unknown>).message as string || "Action blocked by Tether drift detection";
    logger?.warn(`[tether] DRIFT BLOCKED: ${event.toolName} (score=${driftScore})`);
    logger?.warn(`[tether] ${message}`);

    if (config.mode === "enforce") {
      return {
        block: true,
        blockReason: `Tether drift enforcement: ${message}`,
      };
    }
    // Monitor mode: log but don't block
    logger?.info(`[tether] Monitor mode — allowing despite drift`);
  }
}

/**
 * Hook: after_tool_call — tool call completed. Log the result.
 */
export async function onAfterToolCall(
  event: { toolName: string; params: Record<string, unknown>; result?: unknown; error?: string; durationMs?: number },
): Promise<void> {
  if (!config || !state.taskId) return;

  const description = event.error
    ? `Tool result: ${event.toolName} FAILED — ${event.error}`
    : `Tool result: ${event.toolName} completed (${event.durationMs ?? "?"}ms)`;

  // Log as action (non-blocking — just for audit)
  await tetherPost("/api/action/log", {
    agentId: config.agentId,
    taskId: state.taskId,
    action: { description },
  });
}

/**
 * Hook: message_sent — agent sent its reply. Complete the Tether task.
 */
export async function onMessageSent(event: { content: string; success: boolean }): Promise<void> {
  if (!config || !state.taskId) return;
  await completeCurrentTask(event.success ? "Reply sent" : "Reply failed");
}

async function completeCurrentTask(summary: string): Promise<void> {
  if (!config || !state.taskId) return;

  const result = await tetherPost("/api/task/complete", {
    agentId: config.agentId,
    taskId: state.taskId,
    result: { summary, actionCount: state.actionCount },
  });

  if (result) {
    const settlement = result.tokenSettlement as Record<string, number> | undefined;
    const drift = result.driftScore;
    logger?.info(
      `[tether] Task ${state.taskId} complete (drift=${drift ?? "n/a"}, ` +
      `staked=${settlement?.staked ?? "?"}, returned=${settlement?.returned ?? "?"})`
    );
  }

  state.taskId = null;
  state.actionCount = 0;
}
