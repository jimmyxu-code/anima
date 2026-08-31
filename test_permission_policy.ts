import assert from "node:assert/strict";
import {
  installPermissionGate,
  canonicalOperationHash,
  isSingleReadOnlyShell,
  isTrustedReadOnlyTool,
  responseMatches,
} from "./extensions/permission-policy.ts";

function testSingleReadOnlyCommandParser() {
  assert.equal(isSingleReadOnlyShell("Get-Date"), true);
  assert.equal(isSingleReadOnlyShell("Get-Content 'D:\\Desktop\\note.txt'"), true);
  for (const command of [
    "Get-Date; Remove-Item TARGET",
    "Write-Output ok | Remove-Item TARGET",
    "[System.IO.File]::Delete('TARGET')",
    "Get-Date > TARGET",
    "& Remove-Item TARGET",
    "Get-Date $(Remove-Item TARGET)",
    "Get-Date { Remove-Item TARGET }",
    "Get-Date`nRemove-Item TARGET",
  ]) {
    assert.equal(isSingleReadOnlyShell(command), false, command);
  }
}

function testUnknownAndSideEffectToolsAreNotImplicitlyAllowed() {
  for (const name of [
    "ui_click", "ui_type", "press_key", "mouse_click", "delegate_to_kimi",
    "write", "edit", "unknown_tool", "filesystem__read_everything",
  ]) {
    assert.equal(isTrustedReadOnlyTool(name, {}), false, name);
  }
  assert.equal(isTrustedReadOnlyTool("filesystem__read_file", {}), true);
  assert.equal(isTrustedReadOnlyTool("read", { path: "README.md" }), true);
}

function testCanonicalDigestBindsCompleteParameters() {
  const first = canonicalOperationHash("write", { path: "a", content: "one" });
  const reordered = canonicalOperationHash("write", { content: "one", path: "a" });
  const changed = canonicalOperationHash("write", { path: "a", content: "two" });
  assert.equal(first, reordered);
  assert.notEqual(first, changed);
  assert.match(first, /^[0-9a-f]{64}$/);
}

function testLegacyApprovedAndMismatchedResponsesFailClosed() {
  const envelope = {
    question: "是否允许本次工具操作？",
    scope: "完整范围",
    confirmation_id: "confirm-1",
    task_id: "task-1",
    tool_call_id: "call-1",
    canonical_operation_hash: "a".repeat(64),
    expires_at: 12345,
  };
  assert.equal(responseMatches({ approved: true }, envelope), false);
  assert.equal(responseMatches({ ...envelope, outcome: "allowed-once",
    tool_call_id: "call-other" }, envelope), false);
  assert.equal(responseMatches({ ...envelope, outcome: "allowed-once" }, envelope), true);
}

async function testBoundToolCallCanExecuteOnlyOnce() {
  let handler: any;
  const pi = { on: (_name: string, fn: any) => { handler = fn; } };
  installPermissionGate(pi as any, { audit: () => {} });
  const oldFetch = globalThis.fetch;
  const oldEnv = {
    task: process.env.ASSISTANT_TASK_ID,
    epoch: process.env.ASSISTANT_EXECUTOR_EPOCH,
    token: process.env.ASSISTANT_CANCEL_TOKEN,
  };
  try {
    process.env.ASSISTANT_TASK_ID = "task-once";
    process.env.ASSISTANT_EXECUTOR_EPOCH = "7";
    process.env.ASSISTANT_CANCEL_TOKEN = "c".repeat(32);
    globalThis.fetch = (async (_url: any, options: any) => {
      const envelope = JSON.parse(options.body);
      return { ok: true, json: async () => ({ ...envelope, outcome: "allowed-once" }) } as any;
    }) as any;
    const event = {
      toolName: "ui_click", toolCallId: "call-once",
      input: { x: 10, y: 20 },
    };
    assert.equal(await handler(event, {}), undefined);
    const replay = await handler(event, {});
    assert.equal(replay.block, true);

    globalThis.fetch = (async (_url: any, options: any) => {
      const envelope = JSON.parse(options.body);
      process.env.ASSISTANT_EXECUTOR_EPOCH = "8";
      return { ok: true, json: async () => ({ ...envelope, outcome: "allowed-once" }) } as any;
    }) as any;
    const stale = await handler({
      toolName: "ui_click", toolCallId: "call-revoked",
      input: { x: 30, y: 40 },
    }, {});
    assert.equal(stale.block, true);
    assert.match(stale.reason, /租约已撤销或变更/);
  } finally {
    globalThis.fetch = oldFetch;
    if (oldEnv.task === undefined) delete process.env.ASSISTANT_TASK_ID;
    else process.env.ASSISTANT_TASK_ID = oldEnv.task;
    if (oldEnv.epoch === undefined) delete process.env.ASSISTANT_EXECUTOR_EPOCH;
    else process.env.ASSISTANT_EXECUTOR_EPOCH = oldEnv.epoch;
    if (oldEnv.token === undefined) delete process.env.ASSISTANT_CANCEL_TOKEN;
    else process.env.ASSISTANT_CANCEL_TOKEN = oldEnv.token;
  }
}

testSingleReadOnlyCommandParser();
testUnknownAndSideEffectToolsAreNotImplicitlyAllowed();
testCanonicalDigestBindsCompleteParameters();
testLegacyApprovedAndMismatchedResponsesFailClosed();
await testBoundToolCallCanExecuteOnlyOnce();
console.log("PERMISSION_POLICY_TEST PASS");
