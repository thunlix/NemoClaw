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
export interface TetherConfig {
    endpoint: string;
    agentId: string;
    mode: "enforce" | "monitor";
}
/**
 * Initialize Tether integration. Call once during plugin registration.
 */
export declare function initTether(cfg: TetherConfig, log: PluginLogger): void;
/**
 * Hook: message_received — user sent a message. Start a new Tether task.
 */
export declare function onMessageReceived(event: {
    content: string;
}): Promise<void>;
/**
 * Hook: before_tool_call — agent is about to use a tool. Log it and check drift.
 * Returns { block: true } if Tether says the action drifts too far from intent.
 */
export declare function onBeforeToolCall(event: {
    toolName: string;
    params: Record<string, unknown>;
}): Promise<{
    block?: boolean;
    blockReason?: string;
} | void>;
/**
 * Hook: after_tool_call — tool call completed. Log the result.
 */
export declare function onAfterToolCall(event: {
    toolName: string;
    params: Record<string, unknown>;
    result?: unknown;
    error?: string;
    durationMs?: number;
}): Promise<void>;
/**
 * Hook: message_sent — agent sent its reply. Complete the Tether task.
 */
export declare function onMessageSent(event: {
    content: string;
    success: boolean;
}): Promise<void>;
//# sourceMappingURL=tether.d.ts.map