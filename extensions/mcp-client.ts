// MCP 客户端：把 mcp.json 里配置的 MCP 服务进程拉起，把它们的所有工具
// 注册成 Pi 工具（stdio 传输，换行分隔 JSON-RPC）。
//
// mcp.json 形如：
//   { "servers": { "filesystem": { "command": "npx.cmd", "args": ["-y", "..."] } } }
import { spawn, ChildProcess } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const BASE = process.cwd();
const CONFIG = path.join(BASE, "mcp.json");
interface Pending {
  resolve: (v: any) => void;
  reject: (e: Error) => void;
  timer: NodeJS.Timeout;
}

class McpServer {
  private proc: ChildProcess;
  private buf = "";
  private seq = 0;
  private pending = new Map<number, Pending>();
  private dead = false;
  readonly name: string;
  tools: any[] = [];

  constructor(name: string, command: string, args: string[]) {
    this.name = name;
    this._command = command;
    this._args = args;
    this.proc = this.spawnProc(command, args);
  }

  private spawnProc(command: string, args: string[]): ChildProcess {
    // Windows 下 .cmd/.bat 必须经 shell 拉起
    const needsShell = /\.(cmd|bat)$/i.test(command);
    const proc = spawn(command, args, { windowsHide: true, shell: needsShell });
    proc.on("error", () => { this.dead = true; });
    proc.on("exit", () => { this.dead = true; });
    proc.stdout!.on("data", (d) => {
      this.buf += d.toString("utf-8");
      let i: number;
      while ((i = this.buf.indexOf("\n")) >= 0) {
        const line = this.buf.slice(0, i).trim();
        this.buf = this.buf.slice(i + 1);
        if (!line) continue;
        try {
          this.handleMessage(JSON.parse(line));
        } catch { /* 忽略坏行 */ }
      }
    });
    proc.stderr!.on("data", () => {});
    return proc;
  }

  /** 断线惰性重连：进程死了先重拉并重新握手，再发请求 */
  private async ensureAlive(command: string, args: string[]) {
    if (!this.dead) return;
    this.dead = false;
    try { this.proc.kill(); } catch { /* ignore */ }
    this.proc = this.spawnProc(command, args);
    this.buf = "";
    this.pending.forEach((p) => { clearTimeout(p.timer); p.reject(new Error("mcp reconnect")); });
    this.pending.clear();
    await this.init();
  }

  private handleMessage(msg: any) {
    if (msg.id != null && (msg.result !== undefined || msg.error !== undefined)) {
      const p = this.pending.get(msg.id);
      if (p) {
        this.pending.delete(msg.id);
        clearTimeout(p.timer);
        if (msg.error) p.reject(new Error(JSON.stringify(msg.error)));
        else p.resolve(msg.result);
      }
    }
  }

  request(method: string, params?: any, timeoutMs = 30000): Promise<any> {
    const id = ++this.seq;
    const payload = JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n";
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`mcp ${method} timeout`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      try {
        this.proc.stdin!.write(payload);
      } catch (e: any) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(e);
      }
    });
  }

  notify(method: string) {
    try {
      this.proc.stdin!.write(JSON.stringify({ jsonrpc: "2.0", method }) + "\n");
    } catch { /* ignore */ }
  }

  async init() {
    await this.request("initialize", {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: { name: "desktop-agent", version: "0.1" },
    });
    this.notify("notifications/initialized");
    const res = await this.request("tools/list");
    this.tools = res?.tools ?? [];
  }

  async callTool(name: string, args: any): Promise<string> {
    // 断线惰性重连后再调用
    await this.ensureAlive(this._command, this._args);
    const res = await this.request("tools/call", { name, arguments: args }, 120000);
    const parts = (res?.content ?? [])
      .filter((c: any) => c?.type === "text")
      .map((c: any) => c.text);
    return parts.join("\n") || JSON.stringify(res ?? {});
  }

  private _command = "";
  private _args: string[] = [];
}

// ---------- 可热加载的服务注册表（mcp_add 工具用） ----------
const _servers = new Map<string, McpServer>();

async function loadServer(pi: ExtensionAPI, name: string, def: { command: string; args?: string[] }): Promise<number> {
  if (_servers.has(name)) return _servers.get(name)!.tools.length;
  const server = new McpServer(name, def.command, def.args ?? []);
  await server.init();
  for (const tool of server.tools) {
    const fq = `${name}__${tool.name}`;
    pi.registerTool({
      name: fq,
      label: `${name}: ${tool.name}`,
      description: `[MCP ${name}] ${tool.description ?? tool.name}`,
      parameters: (tool.inputSchema ?? { type: "object", properties: {} }) as any,
      async execute(_id, params) {
        try {
          const text = await server.callTool(tool.name, params ?? {});
          return { content: [{ type: "text", text }], details: {} };
        } catch (e: any) {
          return { content: [{ type: "text", text: `MCP 调用失败: ${e.message}` }], details: {} };
        }
      },
    });
  }
  _servers.set(name, server);
  console.error(`[mcp] ${name}: ${server.tools.length} tools registered`);
  return server.tools.length;
}

// 暴露给 system-skills.ts 的 mcp_add 工具
(globalThis as any).__mcpLoadServer = loadServer;

export default async function (pi: ExtensionAPI) {
  let cfg: any = {};
  try {
    cfg = JSON.parse(fs.readFileSync(CONFIG, "utf-8"));
  } catch {
    return; // 没有 mcp.json 就不启用
  }
  const servers = cfg.servers ?? {};
  for (const [name, def] of Object.entries<any>(servers)) {
    try {
      await loadServer(pi, name, def);
    } catch (e: any) {
      console.error(`[mcp] ${name} init failed: ${e.message}`);
    }
  }
}
