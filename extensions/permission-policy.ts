// 中央权限门：只放行可完整解析的精确只读能力；所有副作用和未知工具均逐次确认。
// 这里只实现不可绕过的授权不变量，不猜测用户意图，也不承担模式/任务语义决策。
import * as fs from "node:fs";
import * as path from "node:path";
import { createHash, randomUUID } from "node:crypto";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const BASE = process.cwd();
const AUTHORITY_PATH = path.join(BASE, ".task_authority.json");
const MAX_SCOPE_CHARS = 4000;

const READONLY_SHELL_COMMANDS = new Set([
  "get-date", "get-childitem", "get-item", "get-content", "get-location",
  "get-process", "get-service", "get-computerinfo", "get-ciminstance",
  "get-wmiobject", "get-command", "get-help", "get-member", "get-variable",
  "test-path", "test-connection", "resolve-path", "split-path", "join-path",
  "select-string", "measure-object", "compare-object", "format-list",
  "format-table", "format-wide", "out-string", "write-output",
  "ls", "dir", "gci", "cat", "type", "pwd", "echo",
  "whoami", "hostname", "ipconfig", "systeminfo", "ping",
]);

const READONLY_TOOLS = new Set([
  "read", "grep", "find", "ls", "get_time", "summarize_folder", "find_file",
  "get_clipboard", "read_screen_ui", "list_windows", "job_output", "job_list",
  "list_skills", "look_at_screen",
]);

// MCP 只读能力必须逐服务、逐工具列明；名称前缀不构成可信能力声明。
export const TRUSTED_MCP_READONLY = new Set([
  "filesystem__read_file",
  "filesystem__read_multiple_files",
  "filesystem__list_directory",
  "filesystem__list_directory_with_sizes",
  "filesystem__directory_tree",
  "filesystem__search_files",
  "filesystem__get_file_info",
  "filesystem__list_allowed_directories",
]);

type TaskAuthority = {
  task_id: string;
  executor_epoch: number;
  cancel_token: string;
  expires_at: number;
};

type ApprovalEnvelope = {
  question: string;
  scope: string;
  confirmation_id: string;
  task_id: string;
  tool_call_id: string;
  canonical_operation_hash: string;
  expires_at: number;
};

function stableValue(value: any): any {
  if (Array.isArray(value)) return value.map(stableValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value).sort().map((key) => [key, stableValue(value[key])]),
    );
  }
  if (typeof value === "number" && !Number.isFinite(value)) {
    throw new TypeError("non-finite operation parameter");
  }
  return value;
}

export function canonicalOperationHash(toolName: string, input: any): string {
  const canonical = JSON.stringify(stableValue({ tool: toolName, input }));
  return createHash("sha256").update(canonical, "utf8").digest("hex");
}

export function isSingleReadOnlyShell(command: unknown): boolean {
  if (typeof command !== "string") return false;
  const text = command.trim();
  if (!text || text.length > 8000) return false;
  // 组合符、脚本块、调用/子表达式、重定向、换行、注释和转义一律拒绝。
  if (/[;|&><{}()[\]`#\r\n]/.test(text)) return false;
  const match = /^([A-Za-z][A-Za-z0-9-]*)(?:\s|$)/.exec(text);
  if (!match) return false;
  return READONLY_SHELL_COMMANDS.has(match[1].toLowerCase());
}

export function isTrustedReadOnlyTool(toolName: string, input: any): boolean {
  if (toolName === "bash") return isSingleReadOnlyShell(input?.command);
  if (READONLY_TOOLS.has(toolName)) return true;
  return TRUSTED_MCP_READONLY.has(toolName);
}

function validAuthority(value: any, now = Date.now()): value is TaskAuthority {
  return Boolean(value && typeof value === "object"
    && typeof value.task_id === "string" && value.task_id.length > 0
    && Number.isInteger(value.executor_epoch) && value.executor_epoch >= 1
    && typeof value.cancel_token === "string" && value.cancel_token.length >= 16
    && Number.isFinite(value.expires_at) && now < value.expires_at);
}

function sameAuthority(left: TaskAuthority | null, right: TaskAuthority | null): boolean {
  return Boolean(left && right
    && left.task_id === right.task_id
    && left.executor_epoch === right.executor_epoch
    && left.cancel_token === right.cancel_token);
}

function loadTaskAuthority(input: any): TaskAuthority | null {
  let fileAuthority: any = null;
  let fileExists = false;
  try {
    fileExists = fs.existsSync(AUTHORITY_PATH);
    if (fileExists) fileAuthority = JSON.parse(fs.readFileSync(AUTHORITY_PATH, "utf8"));
  } catch {
    return null;
  }
  const envTask = process.env.ASSISTANT_TASK_ID;
  const envEpoch = Number(process.env.ASSISTANT_EXECUTOR_EPOCH);
  const envToken = process.env.ASSISTANT_CANCEL_TOKEN;
  let envAuthority: TaskAuthority | null = null;
  if (envTask && Number.isInteger(envEpoch) && envToken) {
    envAuthority = {
      task_id: envTask,
      executor_epoch: envEpoch,
      cancel_token: envToken,
      expires_at: Date.now() + 300_000,
    };
  }
  // 发布文件存在时，它是可撤销租约的权威来源；空对象表示已撤销，
  // 不能退回到子进程启动时继承的不可变环境变量。
  const authority = fileExists ? fileAuthority : envAuthority;
  if (!validAuthority(authority)) return null;
  if (envAuthority && !sameAuthority(authority, envAuthority)) return null;
  if (input?.task_id != null && String(input.task_id) !== authority.task_id) return null;
  return authority;
}

function describeOperation(toolName: string, input: any): { question: string; scope: string } | null {
  let params: string;
  try {
    params = JSON.stringify(stableValue(input ?? {}), null, 2);
  } catch {
    return null;
  }
  const scope = `工具：${toolName}\n完整参数：${params}`;
  if (scope.length > MAX_SCOPE_CHARS) return null;
  return { question: `是否允许本次工具“${toolName}”操作？`, scope };
}

function confirmHeaders(): Record<string, string> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  try {
    const token = fs.readFileSync(path.join(BASE, ".confirm_token"), "utf8").trim();
    if (token) headers["X-Confirm-Token"] = token;
  } catch { /* 无令牌时请求会 fail-closed */ }
  return headers;
}

export function responseMatches(data: any, envelope: ApprovalEnvelope): boolean {
  return data?.outcome === "allowed-once"
    && data.confirmation_id === envelope.confirmation_id
    && data.task_id === envelope.task_id
    && data.tool_call_id === envelope.tool_call_id
    && data.canonical_operation_hash === envelope.canonical_operation_hash
    && data.expires_at === envelope.expires_at;
}

async function confirmOnce(envelope: ApprovalEnvelope): Promise<boolean> {
  try {
    const resp = await fetch("http://127.0.0.1:17893/confirm", {
      method: "POST", headers: confirmHeaders(), body: JSON.stringify(envelope),
      signal: AbortSignal.timeout(35_000),
    });
    if (!resp.ok) return false;
    return responseMatches(await resp.json(), envelope);
  } catch {
    return false;
  }
}

function logEvent(type: string, data: Record<string, any>) {
  try {
    fs.mkdirSync(path.join(BASE, "events"), { recursive: true });
    fs.appendFileSync(path.join(BASE, "events", "events.jsonl"),
      JSON.stringify({ ts: new Date().toISOString(), type, ...data }) + "\n", "utf8");
  } catch { /* 审计失败不扩大权限 */ }
}

const consumedToolCalls = new Map<string, number>();

function claimToolCall(taskId: string, toolCallId: string): boolean {
  const now = Date.now();
  for (const [key, expires] of consumedToolCalls) {
    if (expires <= now) consumedToolCalls.delete(key);
  }
  const key = `${taskId}\u0000${toolCallId}`;
  if (consumedToolCalls.has(key)) return false;
  consumedToolCalls.set(key, now + 300_000);
  return true;
}

export function installPermissionGate(
  pi: ExtensionAPI,
  hooks: { audit?: typeof logEvent } = {},
) {
  const audit = hooks.audit ?? logEvent;
  pi.on("tool_call", async (event, _ctx) => {
    const toolName = String(event.toolName ?? "");
    const toolCallId = String(event.toolCallId ?? "").trim();
    const input = event.input ?? {};
    if (isTrustedReadOnlyTool(toolName, input)) return undefined;

    const authority = loadTaskAuthority(input);
    if (!authority || !toolCallId) {
      audit("gate/deny-no-lease", { tool: toolName, tool_call_id: toolCallId });
      return { block: true, reason: "缺少当前任务绑定的执行租约，已拒绝工具调用" };
    }
    if (!claimToolCall(authority.task_id, toolCallId)) {
      return { block: true, reason: "同一工具调用编号已消费，禁止重放" };
    }
    const description = describeOperation(toolName, input);
    if (!description) return { block: true, reason: "操作参数无法完整说明，已拒绝确认" };
    let operationHash: string;
    try {
      operationHash = canonicalOperationHash(toolName, input);
    } catch {
      return { block: true, reason: "操作参数无法规范化，已拒绝确认" };
    }
    const envelope: ApprovalEnvelope = {
      ...description, confirmation_id: randomUUID(), task_id: authority.task_id,
      tool_call_id: toolCallId, canonical_operation_hash: operationHash,
      expires_at: Date.now() + 35_000,
    };
    if (!(await confirmOnce(envelope))) {
      audit("gate/deny", { tool: toolName, task_id: authority.task_id,
        tool_call_id: toolCallId, operation_hash: operationHash });
      return { block: true, reason: "本次操作未获得强绑定的一次性授权" };
    }
    // 用户作答期间任务可能已取消、迁移或换了执行器。批准不是租约；
    // 只有同一个仍存活的 task/epoch/token 才能消费这次授权。
    const currentAuthority = loadTaskAuthority(input);
    if (!sameAuthority(authority, currentAuthority)) {
      audit("gate/deny-stale-lease", { tool: toolName, task_id: authority.task_id,
        tool_call_id: toolCallId, operation_hash: operationHash });
      return { block: true, reason: "任务执行租约已撤销或变更，已拒绝过期授权" };
    }
    audit("gate/allow-once", { tool: toolName, task_id: authority.task_id,
      tool_call_id: toolCallId, operation_hash: operationHash });
    return undefined;
  });
}
