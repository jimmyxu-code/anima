// 电脑智能体 - 系统技能 v2
// 内置工具 + 动态技能（skills/*.json）+ 记忆（memory/MEMORY.md）+ 提醒（reminders/queue.jsonl）
// 约定：Pi 进程以项目根目录为 cwd 启动，所有路径基于此。
import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const BASE = process.cwd();
const SKILLS_DIR = path.join(BASE, "skills");
const MEMORY_FILE = path.join(BASE, "memory", "MEMORY.md");
const REMINDERS_FILE = path.join(BASE, "reminders", "queue.jsonl");
const KIMI_EXE = "C:\\Users\\tester\\.kimi-code\\bin\\kimi.exe";

// Kimi API key：只读用户本地 config.json，源码不留 key
const KIMI_KEY = (() => {
  try {
    const cfg = JSON.parse(fs.readFileSync(path.join(BASE, "config.json"), "utf-8"));
    return cfg.moonshot_api_key ?? "";
  } catch {
    return "";
  }
})();

// ---------- 事件日志（append-only JSONL，审计/恢复/重放的地基） ----------
const EVENTS_FILE = path.join(BASE, "events", "events.jsonl");
fs.mkdirSync(path.dirname(EVENTS_FILE), { recursive: true });

function logEvent(type: string, data: Record<string, any>) {
  try {
    fs.appendFileSync(
      EVENTS_FILE,
      JSON.stringify({ ts: new Date().toISOString(), type, ...data }) + "\n",
      "utf-8",
    );
  } catch { /* 日志失败不阻断业务 */ }
}

function runPowerShell(script: string, timeoutMs = 30000, extraEnv?: Record<string, string>): Promise<string> {
  return new Promise((resolve, reject) => {
    const ps = spawn(
      "powershell.exe",
      ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
      { windowsHide: true, env: extraEnv ? { ...process.env, ...extraEnv } : process.env },
    );
    let out = "";
    let err = "";
    const timer = setTimeout(() => {
      ps.kill();
      reject(new Error("PowerShell timeout"));
    }, timeoutMs);
    ps.stdout.on("data", (d) => (out += d.toString()));
    ps.stderr.on("data", (d) => (err += d.toString()));
    ps.on("close", (code) => {
      clearTimeout(timer);
      if (code === 0) resolve(out.trim());
      else reject(new Error(err.trim() || `exit code ${code}`));
    });
    ps.on("error", (e) => {
      clearTimeout(timer);
      reject(e);
    });
  });
}

function runProcess(
  exe: string,
  args: string[],
  timeoutMs: number,
  cwd?: string,
): Promise<string> {
  return new Promise((resolve, reject) => {
    // Windows 下 .cmd/.bat 必须经 shell 拉起（否则 EINVAL）
    const needsShell = /\.(cmd|bat)$/i.test(exe);
    const p = spawn(exe, args, { windowsHide: true, cwd, shell: needsShell });
    let out = "";
    let err = "";
    const timer = setTimeout(() => {
      p.kill();
      reject(new Error("timeout"));
    }, timeoutMs);
    p.stdout.on("data", (d) => (out += d.toString()));
    p.stderr.on("data", (d) => (err += d.toString()));
    p.on("close", () => {
      clearTimeout(timer);
      resolve((out + (err ? "\n" + err : "")).trim());
    });
    p.on("error", (e) => {
      clearTimeout(timer);
      reject(e);
    });
  });
}

// 转义单引号，供嵌入 PowerShell 单引号字符串
function psq(s: string): string {
  return String(s).replace(/'/g, "''");
}

interface SkillDef {
  name: string;
  description: string;
  when_to_use?: string;   // 路由用：什么时候该调用它（catalog 进 prompt 的关键）
  script: string; // PowerShell 脚本，可用 $env:SKILL_INPUT 读取 input 参数
}

export default function (pi: ExtensionAPI) {
  fs.mkdirSync(SKILLS_DIR, { recursive: true });
  fs.mkdirSync(path.dirname(MEMORY_FILE), { recursive: true });
  fs.mkdirSync(path.dirname(REMINDERS_FILE), { recursive: true });

  // ---------- 动态技能：一个 SkillDef 注册一个工具 ----------
  const SKILL_EVICT_DAYS = 30;   // 超过 30 天没用且用得少 → 自动归档

  function skillPath(name: string) {
    return path.join(SKILLS_DIR, `${name}.json`);
  }

  function trackSkillUse(name: string) {
    try {
      const p = skillPath(name);
      const def = JSON.parse(fs.readFileSync(p, "utf-8"));
      def.last_used = new Date().toISOString().slice(0, 10);
      def.use_count = (def.use_count ?? 0) + 1;
      fs.writeFileSync(p, JSON.stringify(def, null, 2), "utf-8");
    } catch { /* 不阻断 */ }
  }

  function registerDynamicSkill(def: SkillDef) {
    pi.registerTool({
      name: def.name,
      label: def.name,
      description: def.when_to_use
        ? `${def.description}（何时使用：${def.when_to_use}）`
        : def.description,
      parameters: Type.Object({
        input: Type.Optional(Type.String({ description: "技能需要的输入文本，可为空" })),
      }),
      async execute(_id, params) {
        trackSkillUse(def.name);
        const input = params.input == null ? "" : String(params.input);
        const text = await runPowerShell(def.script, 60000, { SKILL_INPUT: input })
          .catch((e) => `ERROR: ${e.message}`);
        return { content: [{ type: "text", text }], details: {} };
      },
    });
  }

  // 启动时加载已固化的技能
  try {
    for (const f of fs.readdirSync(SKILLS_DIR)) {
      if (!f.endsWith(".json")) continue;
      try {
        const def = JSON.parse(fs.readFileSync(path.join(SKILLS_DIR, f), "utf-8")) as SkillDef;
        if (def.name && def.script) registerDynamicSkill(def);
      } catch { /* 跳过坏文件 */ }
    }
  } catch { /* skills 目录不存在也没关系 */ }

  // ---------- 技能生命周期管理：长期不用自动归档（不硬删，可恢复） ----------
  function evictStaleSkills() {
    const archiveDir = path.join(SKILLS_DIR, "_archive");
    const cutoff = Date.now() - SKILL_EVICT_DAYS * 86400000;
    try {
      for (const f of fs.readdirSync(SKILLS_DIR)) {
        if (!f.endsWith(".json")) continue;
        try {
          const p = path.join(SKILLS_DIR, f);
          const def = JSON.parse(fs.readFileSync(p, "utf-8"));
          const last = def.last_used ? Date.parse(def.last_used) : Date.parse(def.created ?? 0);
          const uses = def.use_count ?? 0;
          if (last < cutoff && uses < 3) {
            fs.mkdirSync(archiveDir, { recursive: true });
            fs.renameSync(p, path.join(archiveDir, f));
            logEvent("skill/evicted", { name: def.name, uses, last_used: def.last_used });
          }
        } catch { /* 跳过坏文件 */ }
      }
    } catch { /* skills 目录不存在也没关系 */ }
  }
  evictStaleSkills();
  setInterval(evictStaleSkills, 24 * 3600 * 1000);  // 每天扫一次

  // ---------- 内置工具 ----------

  pi.registerTool({
    name: "get_time",
    label: "Get Time",
    description: "获取当前日期和时间",
    promptGuidelines: ["Use get_time when the user asks what time or date it is."],
    parameters: Type.Object({}),
    async execute() {
      const text = await runPowerShell("Get-Date -Format 'yyyy-MM-dd HH:mm:ss dddd'");
      return { content: [{ type: "text", text }], details: {} };
    },
  });

  pi.registerTool({
    name: "set_wallpaper",
    label: "Set Wallpaper",
    description: "把桌面壁纸换成指定的图片文件",
    promptGuidelines: ["Use set_wallpaper when the user asks to change the desktop wallpaper."],
    parameters: Type.Object({
      path: Type.String({ description: "图片文件的完整路径" }),
    }),
    async execute(_id, params) {
      const script = `
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class WP {
  [DllImport("user32.dll", CharSet = CharSet.Unicode)]
  public static extern int SystemParametersInfo(int a, int b, string c, int d);
}
'@
if (-not (Test-Path '${psq(params.path)}')) { Write-Error "file not found"; exit 1 }
[WP]::SystemParametersInfo(20, 0, '${psq(params.path)}', 3) | Out-Null
Write-Output "wallpaper set"`;
      const text = await runPowerShell(script);
      return { content: [{ type: "text", text }], details: {} };
    },
  });

  pi.registerTool({
    name: "set_volume",
    label: "Set Volume",
    description: "设置系统音量（0-100）",
    promptGuidelines: ["Use set_volume when the user asks to change or adjust the volume."],
    parameters: Type.Object({
      level: Type.Number({ description: "目标音量 0-100" }),
    }),
    async execute(_id, params) {
      const level = Math.max(0, Math.min(100, Math.round(Number(params.level) || 0)));
      // SendKeys 媒体键：每按一下走 2 格。先按 50 次减到 0，再加到目标。
      const ups = Math.round(level / 2);
      const script = `
$w = New-Object -ComObject WScript.Shell
1..50 | ForEach-Object { $w.SendKeys([char]174) }
if (${ups} -gt 0) { 1..${ups} | ForEach-Object { $w.SendKeys([char]175) } }
Write-Output "volume ~ ${level}"`;
      const text = await runPowerShell(script);
      return { content: [{ type: "text", text }], details: {} };
    },
  });

  pi.registerTool({
    name: "set_theme",
    label: "Set Theme",
    description: "切换 Windows 深色/浅色主题",
    promptGuidelines: ["Use set_theme when the user asks to switch dark mode or light mode."],
    parameters: Type.Object({
      mode: Type.Union([Type.Literal("dark"), Type.Literal("light")]),
    }),
    async execute(_id, params) {
      const v = params.mode === "dark" ? 0 : 1;
      const script = `
Set-ItemProperty 'HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize' -Name AppsUseLightTheme -Value ${v}
Set-ItemProperty 'HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize' -Name SystemUsesLightTheme -Value ${v}
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class BC {
  [DllImport("user32.dll", CharSet = CharSet.Unicode)]
  public static extern IntPtr SendMessageTimeout(IntPtr h, uint m, UIntPtr w, string l, uint f, uint t, out IntPtr r);
}
'@
$r = [IntPtr]::Zero
[BC]::SendMessageTimeout([IntPtr]0xffff, 0x001A, [UIntPtr]::Zero, 'ImmersiveColorSet', 2, 1000, [ref]$r) | Out-Null
Write-Output "theme ${params.mode}"`;
      const text = await runPowerShell(script);
      return { content: [{ type: "text", text }], details: {} };
    },
  });

  pi.registerTool({
    name: "toggle_desktop_icons",
    label: "Toggle Desktop Icons",
    description: "显示或隐藏桌面图标（在两种状态间切换）",
    promptGuidelines: [
      "Use toggle_desktop_icons when the user asks to hide or show desktop icons.",
    ],
    parameters: Type.Object({}),
    async execute() {
      const script = `
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class DI {
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr FindWindow(string c, string n);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr FindWindowEx(IntPtr p, IntPtr after, string c, string n);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
}
'@
$defview = [DI]::FindWindowEx([DI]::FindWindow('Progman', 'Program Manager'), [IntPtr]::Zero, 'SHELLDLL_DefView', '')
if ($defview -eq [IntPtr]::Zero) {
  $h = [IntPtr]::Zero
  while ($true) {
    $h = [DI]::FindWindowEx([IntPtr]::Zero, $h, 'WorkerW', $null)
    if ($h -eq [IntPtr]::Zero) { break }
    $defview = [DI]::FindWindowEx($h, [IntPtr]::Zero, 'SHELLDLL_DefView', '')
    if ($defview -ne [IntPtr]::Zero) { break }
  }
}
if ($defview -eq [IntPtr]::Zero) { Write-Error 'SHELLDLL_DefView not found'; exit 1 }
[DI]::SendMessage($defview, 0x0111, [IntPtr]0x7402, [IntPtr]::Zero) | Out-Null
Write-Output 'desktop icons toggled'`;
      const text = await runPowerShell(script);
      return { content: [{ type: "text", text }], details: {} };
    },
  });

  pi.registerTool({
    name: "empty_recycle_bin",
    label: "Empty Recycle Bin",
    description: "清空回收站",
    promptGuidelines: ["Use empty_recycle_bin when the user asks to empty the recycle bin."],
    parameters: Type.Object({}),
    async execute() {
      const text = await runPowerShell(
        "Clear-RecycleBin -Force -ErrorAction SilentlyContinue; Write-Output 'recycle bin emptied'",
      );
      return { content: [{ type: "text", text }], details: {} };
    },
  });

  pi.registerTool({
    name: "organize_folder",
    label: "Organize Folder",
    description: "把指定文件夹里的散文件按类型（图片/文档/视频/音频/压缩包/其他）归档到子文件夹",
    promptGuidelines: [
      "Use organize_folder when the user asks to tidy up a folder such as Downloads or the desktop.",
    ],
    parameters: Type.Object({
      path: Type.String({ description: "要整理的文件夹完整路径" }),
    }),
    async execute(_id, params) {
      const script = `
$folder = '${psq(params.path)}'
if (-not (Test-Path $folder)) { Write-Error 'folder not found'; exit 1 }
$map = @{
  '图片' = @('.jpg','.jpeg','.png','.gif','.bmp','.webp','.ico','.svg')
  '文档' = @('.txt','.md','.doc','.docx','.pdf','.xls','.xlsx','.ppt','.pptx','.csv')
  '视频' = @('.mp4','.mkv','.avi','.mov','.flv','.wmv')
  '音频' = @('.mp3','.wav','.flac','.aac','.ogg','.m4a')
  '压缩包' = @('.zip','.rar','.7z','.tar','.gz')
}
$moved = 0
Get-ChildItem -LiteralPath $folder -File | Where-Object { -not $_.Attributes.HasFlag([IO.FileAttributes]::Hidden) } | ForEach-Object {
  $ext = $_.Extension.ToLower()
  $cat = '其他'
  foreach ($k in $map.Keys) { if ($map[$k] -contains $ext) { $cat = $k; break } }
  $dest = Join-Path $folder $cat
  if (-not (Test-Path $dest)) { New-Item -ItemType Directory -Path $dest | Out-Null }
  Move-Item -LiteralPath $_.FullName -Destination $dest -ErrorAction SilentlyContinue
  $moved++
}
Write-Output "moved $moved files"`;
      const text = await runPowerShell(script, 120000);
      return { content: [{ type: "text", text }], details: {} };
    },
  });

  pi.registerTool({
    name: "summarize_folder",
    label: "Summarize Folder",
    description: "列出一个文件夹的内容概况（数量、大小、最近文件），供你向用户总结",
    promptGuidelines: [
      "Use summarize_folder when the user asks what is in a folder or for a folder summary.",
    ],
    parameters: Type.Object({
      path: Type.String({ description: "文件夹完整路径" }),
    }),
    async execute(_id, params) {
      const script = `
$folder = '${psq(params.path)}'
if (-not (Test-Path $folder)) { Write-Error 'folder not found'; exit 1 }
$items = Get-ChildItem -LiteralPath $folder -Force -ErrorAction SilentlyContinue
$files = $items | Where-Object { -not $_.PSIsContainer }
$dirs = $items | Where-Object { $_.PSIsContainer }
$size = ($files | Measure-Object Length -Sum).Sum
Write-Output ("文件夹: " + $folder)
Write-Output ("子文件夹 " + $dirs.Count + " 个, 文件 " + $files.Count + " 个, 总大小 " + [math]::Round($size/1MB,1) + " MB")
Write-Output '按扩展名:'
$files | Group-Object Extension | Sort-Object Count -Descending | Select-Object -First 10 | ForEach-Object { Write-Output ("  " + $_.Name + " x" + $_.Count) }
Write-Output '最近修改的文件:'
$files | Sort-Object LastWriteTime -Descending | Select-Object -First 10 | ForEach-Object { Write-Output ("  " + $_.LastWriteTime.ToString('MM-dd HH:mm') + "  " + $_.Name) }`;
      const text = await runPowerShell(script, 60000);
      return { content: [{ type: "text", text }], details: {} };
    },
  });

  pi.registerTool({
    name: "find_file",
    label: "Find File",
    description: "找文件：先在热区（桌面/文档/下载/项目）按文件名快搜，找不到再对文本文件做内容扩搜",
    promptGuidelines: ["Use find_file when the user asks to find or locate a file or its content."],
    parameters: Type.Object({
      keyword: Type.String({ description: "文件名或内容关键词" }),
    }),
    async execute(_id, params) {
      const script = `
$kw = '${psq(params.keyword)}'
$roots = @('D:\\Desktop', [Environment]::GetFolderPath('MyDocuments'), (Join-Path $env:USERPROFILE 'Downloads'))
Write-Output '== 按文件名 =='
$hits = @()
foreach ($r in $roots) {
  if (Test-Path $r) {
    $hits += Get-ChildItem -LiteralPath $r -Recurse -Depth 5 -Filter ('*' + $kw + '*') -ErrorAction SilentlyContinue |
      Where-Object { $_.FullName -notmatch '\\\\(node_modules|\\.venv|__pycache__|\\.git)\\\\' } |
      Select-Object -First 10
  }
}
if ($hits.Count -gt 0) {
  $hits | Select-Object -First 10 | ForEach-Object { Write-Output $_.FullName }
} else {
  Write-Output '(文件名无命中)'
  Write-Output '== 按内容（文本类文件） =='
  $content = @()
  foreach ($r in $roots) {
    if (Test-Path $r) {
      $content += Get-ChildItem -LiteralPath $r -Recurse -Depth 3 -Include *.txt,*.md,*.json,*.csv,*.ps1,*.py,*.ts,*.log -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch '\\\\(node_modules|\\.venv|__pycache__|\\.git)\\\\' -and $_.Length -lt 2MB } |
        Select-String -Pattern $kw -SimpleMatch -List -ErrorAction SilentlyContinue |
        Select-Object -First 5
    }
  }
  if ($content.Count -gt 0) {
    $content | Select-Object -First 5 | ForEach-Object { Write-Output ($_.Path + '  行' + $_.LineNumber + ': ' + $_.Line.Trim().Substring(0, [Math]::Min(60, $_.Line.Trim().Length))) }
  } else {
    Write-Output '(内容也无命中)'
  }
}`;
      const text = await runPowerShell(script, 90000);
      return { content: [{ type: "text", text }], details: {} };
    },
  });

  pi.registerTool({
    name: "get_clipboard",
    label: "Get Clipboard",
    description: "读取剪贴板里的文本内容",
    promptGuidelines: [
      "Use get_clipboard when the user asks to read, summarize, or use the clipboard content.",
    ],
    parameters: Type.Object({}),
    async execute() {
      const text = await runPowerShell(
        "$c = Get-Clipboard -ErrorAction SilentlyContinue; if ($c) { if ($c.Length -gt 2000) { $c.Substring(0,2000) + '…(截断)' } else { $c } } else { Write-Output '(剪贴板为空)' }",
      );
      return { content: [{ type: "text", text }], details: {} };
    },
  });

  // ---------- UIA 感知与 GUI 操作 ----------

  pi.registerTool({
    name: "read_screen_ui",
    label: "Read Screen UI",
    description: "读取当前前台窗口的控件树（按钮、输入框、菜单等），比截屏更结构化",
    promptGuidelines: [
      "Use read_screen_ui when the user asks what can be clicked in the current window, or before operating UI controls.",
    ],
    parameters: Type.Object({}),
    async execute() {
      const script = `
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class FG {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
}
'@
$el = [System.Windows.Automation.AutomationElement]::FromHandle([FG]::GetForegroundWindow())
Write-Output ('前台窗口: ' + $el.Current.Name)
$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
$script:count = 0
function Dump($e, $depth) {
  if ($depth -gt 5 -or $script:count -gt 80) { return }
  $script:count++
  $c = $e.Current
  $name = $c.Name
  if ($name.Length -gt 40) { $name = $name.Substring(0,40) }
  $ct = $c.ControlType.ProgrammaticName -replace 'ControlType\\.',''
  $r = $c.BoundingRectangle
  $indent = '  ' * $depth
  Write-Output ('{0}{1} [{2}] ({3},{4})' -f $indent, $ct, $name, [math]::Round($r.X), [math]::Round($r.Y))
  $child = $walker.GetFirstChild($e)
  while ($null -ne $child) {
    Dump $child ($depth+1)
    $child = $walker.GetNextSibling($child)
  }
}
Dump $el 0`;
      const text = await runPowerShell(script, 30000);
      return { content: [{ type: "text", text }], details: {} };
    },
  });

  pi.registerTool({
    name: "ui_click",
    label: "UI Click",
    description: "在当前前台窗口里点击名字包含指定文字的控件（按钮/菜单/链接等）",
    promptGuidelines: [
      "Use ui_click when the user asks to click or press something in the current window, e.g. 点一下保存按钮。",
    ],
    parameters: Type.Object({
      name: Type.String({ description: "控件名字包含的文字，如 保存 / 发送 / 关闭" }),
    }),
    async execute(_id, params) {
      const script = `
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class MS {
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, UIntPtr e);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
}
'@
$kw = '${psq(params.name)}'
$root = [System.Windows.Automation.AutomationElement]::FromHandle([MS]::GetForegroundWindow())
$all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
$target = $null
$n = 0
foreach ($e in $all) {
  $n++; if ($n -gt 800) { break }
  try { $nm = $e.Current.Name } catch { continue }
  if ($nm -and $nm.Contains($kw)) { $target = $e; break }
}
if ($null -eq $target) { Write-Error ('找不到控件: ' + $kw); exit 1 }
$tn = $target.Current.Name
try {
  $ip = $target.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
  if ($ip) { $ip.Invoke(); Write-Output ('invoked: ' + $tn); exit 0 }
} catch {}
try {
  $pt = $target.GetClickablePoint()
  [MS]::SetCursorPos([int]$pt.X, [int]$pt.Y) | Out-Null
  Start-Sleep -Milliseconds 80
  [MS]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
  [MS]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
  Write-Output ('clicked: ' + $tn)
} catch { Write-Error ('无法点击: ' + $tn); exit 1 }`;
      const text = await runPowerShell(script, 45000);
      return { content: [{ type: "text", text }], details: {} };
    },
  });

  pi.registerTool({
    name: "ui_type",
    label: "UI Type",
    description: "往当前获得焦点的输入框里输入文字",
    promptGuidelines: ["Use ui_type when the user asks to type text into the focused field."],
    parameters: Type.Object({
      text: Type.String({ description: "要输入的文字" }),
    }),
    async execute(_id, params) {
      const script = `
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$txt = @'
${psq(params.text)}
'@.Trim()
$focused = [System.Windows.Automation.AutomationElement]::FocusedElement
$done = $false
if ($focused) {
  try {
    $vp = $focused.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
    if ($vp) { $vp.SetValue($txt); $done = $true }
  } catch {}
}
if ($done) { Write-Output 'typed via UIA' } else {
  Set-Clipboard -Value $txt
  $w = New-Object -ComObject WScript.Shell
  $w.SendKeys('^v')
  Write-Output 'typed via clipboard paste'
}`;
      const text = await runPowerShell(script, 30000);
      return { content: [{ type: "text", text }], details: {} };
    },
  });

  pi.registerTool({
    name: "press_key",
    label: "Press Key",
    description: "按一个键：enter/esc/tab/backspace/delete/space/up/down/left/right",
    promptGuidelines: ["Use press_key when the user asks to press a keyboard key."],
    parameters: Type.Object({
      key: Type.String({ description: "键名，如 enter / esc / tab" }),
    }),
    async execute(_id, params) {
      const script = `
$m = @{ enter='{ENTER}'; esc='{ESC}'; tab='{TAB}'; backspace='{BS}'; delete='{DEL}'; del='{DEL}'; space=' '; up='{UP}'; down='{DOWN}'; left='{LEFT}'; right='{RIGHT}'; home='{HOME}'; end='{END}' }
$k = '${psq(params.key)}'.ToLower()
$seq = if ($m.ContainsKey($k)) { $m[$k] } else { $k }
$w = New-Object -ComObject WScript.Shell
$w.SendKeys($seq)
Write-Output ('pressed: ' + $k)`;
      const text = await runPowerShell(script, 15000);
      return { content: [{ type: "text", text }], details: {} };
    },
  });

  pi.registerTool({
    name: "open_app",
    label: "Open App",
    description: "按名字打开应用/软件（商店应用和传统桌面软件都支持）",
    promptGuidelines: [
      "Use open_app when the user asks to open or launch an app, e.g. 打开微信、启动记事本。",
    ],
    parameters: Type.Object({
      name: Type.String({ description: "应用名字，如 微信 / notepad / Chrome" }),
    }),
    async execute(_id, params) {
      const script = `
$name = '${psq(params.name)}'
# 1) 系统注册的应用（UWP + Win32 都在）
$app = Get-StartApps | Where-Object { $_.Name -like ('*' + $name + '*') } | Select-Object -First 1
if ($app) {
  Start-Process ('shell:AppsFolder\\' + $app.AppID)
  Write-Output ('opened: ' + $app.Name)
  exit 0
}
# 2) PATH 里的可执行文件
$candidates = @($name, $name + '.exe')
foreach ($c in $candidates) {
  $exe = Get-Command $c -ErrorAction SilentlyContinue
  if ($exe) { Start-Process $exe.Source; Write-Output ('opened: ' + $exe.Source); exit 0 }
}
# 3) 开始菜单快捷方式
$dirs = @(
  "$env:ProgramData\\Microsoft\\Windows\\Start Menu\\Programs",
  "$env:APPDATA\\Microsoft\\Windows\\Start Menu\\Programs"
)
$lnk = Get-ChildItem $dirs -Recurse -Filter '*.lnk' -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -like ('*' + $name + '*') } | Select-Object -First 1
if ($lnk) { Start-Process $lnk.FullName; Write-Output ('opened: ' + $lnk.Name); exit 0 }
Write-Error ('找不到应用: ' + $name)
exit 1`;
      const text = await runPowerShell(script, 30000);
      return { content: [{ type: "text", text }], details: {} };
    },
  });

  pi.registerTool({
    name: "mouse_move",
    label: "Mouse Move",
    description: "把鼠标光标移动到屏幕指定坐标",
    promptGuidelines: ["Use mouse_move when the user asks to move the cursor to a position."],
    parameters: Type.Object({
      x: Type.Number(), y: Type.Number(),
    }),
    async execute(_id, params) {
      const script = `
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class MV { [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y); }
'@
[MV]::SetCursorPos(${Number(params.x) | 0}, ${Number(params.y) | 0}) | Out-Null
Write-Output 'moved'`;
      const text = await runPowerShell(script, 10000);
      return { content: [{ type: "text", text }], details: {} };
    },
  });

  pi.registerTool({
    name: "mouse_click",
    label: "Mouse Click",
    description: "在屏幕指定坐标点击鼠标（配合视觉模型使用）",
    promptGuidelines: [
      "Use mouse_click to click at screen coordinates; use look_at_screen first to find the coordinates.",
    ],
    parameters: Type.Object({
      x: Type.Number(), y: Type.Number(),
      button: Type.Optional(Type.Union([Type.Literal("left"), Type.Literal("right"), Type.Literal("middle")])),
    }),
    async execute(_id, params) {
      const btn = params.button ?? "left";
      const flags: Record<string, [number, number]> = {
        left: [0x0002, 0x0004], right: [0x0008, 0x0010], middle: [0x0020, 0x0040],
      };
      const [down, up] = flags[btn];
      const script = `
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class MC {
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, UIntPtr e);
}
'@
[MC]::SetCursorPos(${Number(params.x) | 0}, ${Number(params.y) | 0}) | Out-Null
Start-Sleep -Milliseconds 60
[MC]::mouse_event(${down}, 0, 0, 0, [UIntPtr]::Zero)
[MC]::mouse_event(${up}, 0, 0, 0, [UIntPtr]::Zero)
Write-Output 'clicked ${btn}'`;
      const text = await runPowerShell(script, 10000);
      return { content: [{ type: "text", text }], details: {} };
    },
  });

  pi.registerTool({
    name: "mouse_scroll",
    label: "Mouse Scroll",
    description: "在当前鼠标位置滚动滚轮",
    promptGuidelines: ["Use mouse_scroll when the user asks to scroll up or down."],
    parameters: Type.Object({
      direction: Type.Union([Type.Literal("up"), Type.Literal("down")]),
      clicks: Type.Optional(Type.Number({ description: "滚动格数，默认 3" })),
    }),
    async execute(_id, params) {
      const n = Number(params.clicks ?? 3) | 0;
      const delta = (params.direction === "up" ? 120 : -120) * n;
      const script = `
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class SC { [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, UIntPtr e); }
'@
[SC]::mouse_event(0x0800, 0, 0, ${delta}, [UIntPtr]::Zero)
Write-Output 'scrolled'`;
      const text = await runPowerShell(script, 10000);
      return { content: [{ type: "text", text }], details: {} };
    },
  });

  pi.registerTool({
    name: "web_search",
    label: "Web Search",
    description: "联网搜索最新信息（遇到不知道、不会做的事先查）",
    promptGuidelines: [
      "Use web_search when you lack information or don't know how to do something, before saying you can't.",
    ],
    parameters: Type.Object({
      query: Type.String({ description: "搜索关键词" }),
    }),
    async execute(_id, params) {
      const url = "https://api.kimi.com/coding/v1/chat/completions";
      const headers = {
        "Content-Type": "application/json",
        Authorization: "Bearer " + KIMI_KEY + "",
      };
      const messages: any[] = [
        {
          role: "system",
          content: "你是搜索助手。用联网搜索回答问题，答案里附上关键信息来源。用简洁中文。",
        },
        { role: "user", content: params.query },
      ];
      const tools = [{ type: "builtin_function", function: { name: "$web_search" } }];

      for (let round = 0; round < 4; round++) {
        const resp = await fetch(url, {
          method: "POST",
          headers,
          body: JSON.stringify({
            model: "kimi-for-coding-highspeed",
            messages,
            tools,
            max_tokens: 1500,
          }),
        });
        const data: any = await resp.json();
        const msg = data?.choices?.[0]?.message;
        if (!msg) return { content: [{ type: "text", text: `搜索失败: HTTP ${resp.status}` }], details: {} };
        messages.push(msg);
        const calls = msg.tool_calls ?? [];
        if (calls.length === 0) {
          const text = (msg.content ?? "").slice(0, 1500) || "(空结果)";
          return { content: [{ type: "text", text }], details: {} };
        }
        // Kimi 约定：客户端原样回传工具参数，服务端在下一轮真正执行搜索
        for (const call of calls) {
          messages.push({
            role: "tool",
            tool_call_id: call.id,
            content: call.function?.arguments ?? "{}",
          });
        }
      }
      return { content: [{ type: "text", text: "搜索轮次超限" }], details: {} };
    },
  });

  pi.registerTool({
    name: "watch_screen",
    label: "Watch Screen",
    description: "连拍几秒屏幕帧，用 Kimi 视觉模型理解屏幕上正在发生什么（适合动态过程、弹窗、操作反馈）",
    promptGuidelines: [
      "Use watch_screen when a single screenshot is not enough, e.g. 正在发生什么、刚才弹了什么、动态过程。",
    ],
    parameters: Type.Object({
      question: Type.String({ description: "关于屏幕动态过程的问题" }),
      seconds: Type.Optional(Type.Number({ description: "连拍秒数 0.5-5，默认 1.5" })),
    }),
    async execute(_id, params) {
      const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "burst-"));
      const seconds = Math.min(Math.max(Number(params.seconds ?? 1.5), 0.5), 5);
      const py = "D:\\Desktop\\数字生命\\智能输入项目\\智能语音输入\\.venv\\Scripts\\python.exe";
      const script = "D:\\Desktop\\数字生命\\助手级\\vision_burst.py";
      await runProcess(py, ["-X", "utf8", script, tmp, String(seconds)], 30000);

      const files = fs.readdirSync(tmp).filter((f) => f.endsWith(".jpg")).sort();
      const content: any[] = files.map((f) => ({
        type: "image_url",
        image_url: { url: `data:image/jpeg;base64,${fs.readFileSync(path.join(tmp, f)).toString("base64")}` },
      }));
      content.push({
        type: "text",
        text: `这是连续 ${seconds} 秒的屏幕连拍（按时间顺序）。${params.question}（用简短口语化的中文回答，一两句话）`,
      });

      const resp = await fetch("https://api.kimi.com/coding/v1/chat/completions", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: "Bearer " + KIMI_KEY + "",
        },
        body: JSON.stringify({
          model: "kimi-for-coding-highspeed",
          thinking: { type: "disabled" },
          max_tokens: 400,
          messages: [{ role: "user", content }],
        }),
      });
      const data: any = await resp.json();
      const text = data?.choices?.[0]?.message?.content ?? `视觉调用失败: HTTP ${resp.status}`;
      for (const f of files) fs.unlinkSync(path.join(tmp, f));
      fs.rmdirSync(tmp);
      return { content: [{ type: "text", text }], details: {} };
    },
  });

  pi.registerTool({
    name: "fetch_url",
    label: "Fetch URL",
    description: "抓取一个网页的正文内容（用于读链接、看文章、查资料）",
    promptGuidelines: ["Use fetch_url when the user shares a link or asks to read a web page."],
    parameters: Type.Object({
      url: Type.String({ description: "网页地址" }),
    }),
    async execute(_id, params) {
      const resp = await fetch(String(params.url), {
        headers: { "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)" },
        signal: AbortSignal.timeout(20000),
      });
      const html = await resp.text();
      // 粗提取正文：去脚本/样式/标签，压缩空白
      const text = html
        .replace(/<script[\s\S]*?<\/script>/gi, " ")
        .replace(/<style[\s\S]*?<\/style>/gi, " ")
        .replace(/<[^>]+>/g, " ")
        .replace(/&nbsp;/g, " ")
        .replace(/&amp;/g, "&")
        .replace(/&lt;/g, "<")
        .replace(/&gt;/g, ">")
        .replace(/&quot;/g, '"')
        .replace(/\s+/g, " ")
        .trim()
        .slice(0, 2500);
      return { content: [{ type: "text", text: text || "(空页面)" }], details: {} };
    },
  });

  pi.registerTool({
    name: "list_windows",
    label: "List Windows",
    description: "列出当前打开的窗口标题",
    promptGuidelines: ["Use list_windows when the user asks what windows or apps are open."],
    parameters: Type.Object({}),
    async execute() {
      const script = `
Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public class WE {
  public delegate bool EnumCb(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumCb cb, IntPtr l);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
}
'@
$titles = New-Object System.Collections.Generic.List[string]
$cb = [WE+EnumCb]{
  param($h, $l)
  if ([WE]::IsWindowVisible($h)) {
    $sb = New-Object System.Text.StringBuilder 256
    [void][WE]::GetWindowText($h, $sb, 256)
    if ($sb.Length -gt 0) { $titles.Add($sb.ToString()) }
  }
  return $true
}
[void][WE]::EnumWindows($cb, [IntPtr]::Zero)
$titles | Select-Object -First 25 | ForEach-Object { Write-Output $_ }`;
      const text = await runPowerShell(script, 20000);
      return { content: [{ type: "text", text: text || "(没有可见窗口)" }], details: {} };
    },
  });

  pi.registerTool({
    name: "switch_window",
    label: "Switch Window",
    description: "把标题包含指定文字的窗口切到前台",
    promptGuidelines: ["Use switch_window when the user asks to switch to a window or app."],
    parameters: Type.Object({
      name: Type.String({ description: "窗口标题包含的文字" }),
    }),
    async execute(_id, params) {
      const script = `
Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public class WF {
  public delegate bool EnumCb(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumCb cb, IntPtr l);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
}
'@
$kw = '${psq(params.name)}'
$script:found = [IntPtr]::Zero
$cb = [WF+EnumCb]{
  param($h, $l)
  if ($script:found -ne [IntPtr]::Zero) { return $false }
  if ([WF]::IsWindowVisible($h)) {
    $sb = New-Object System.Text.StringBuilder 256
    [void][WF]::GetWindowText($h, $sb, 256)
    if ($sb.ToString().Contains($kw)) { $script:found = $h; return $false }
  }
  return $true
}
[void][WF]::EnumWindows($cb, [IntPtr]::Zero)
if ($script:found -eq [IntPtr]::Zero) { Write-Error ('找不到窗口: ' + $kw); exit 1 }
[void][WF]::ShowWindow($script:found, 9)
[void][WF]::SetForegroundWindow($script:found)
Write-Output 'switched'`;
      const text = await runPowerShell(script, 20000);
      return { content: [{ type: "text", text }], details: {} };
    },
  });

  pi.registerTool({
    name: "close_window",
    label: "Close Window",
    description: "关闭标题包含指定文字的窗口",
    promptGuidelines: ["Use close_window when the user asks to close a window or app."],
    parameters: Type.Object({
      name: Type.String({ description: "窗口标题包含的文字" }),
    }),
    async execute(_id, params) {
      const script = `
Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public class WC {
  public delegate bool EnumCb(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumCb cb, IntPtr l);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
}
'@
$kw = '${psq(params.name)}'
$script:found = [IntPtr]::Zero
$cb = [WC+EnumCb]{
  param($h, $l)
  if ($script:found -ne [IntPtr]::Zero) { return $false }
  if ([WC]::IsWindowVisible($h)) {
    $sb = New-Object System.Text.StringBuilder 256
    [void][WC]::GetWindowText($h, $sb, 256)
    if ($sb.ToString().Contains($kw)) { $script:found = $h; return $false }
  }
  return $true
}
[void][WC]::EnumWindows($cb, [IntPtr]::Zero)
if ($script:found -eq [IntPtr]::Zero) { Write-Error ('找不到窗口: ' + $kw); exit 1 }
[void][WC]::SendMessage($script:found, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero)
Write-Output 'closed'`;
      const text = await runPowerShell(script, 20000);
      return { content: [{ type: "text", text }], details: {} };
    },
  });

  pi.registerTool({
    name: "set_config",
    label: "Set Config",
    description: "修改伙伴自己的可调参数（换唤醒词、换音色、开关唤醒、设日报时间），写入 config.json 立即生效，不改任何源代码",
    promptGuidelines: [
      "Use set_config when the user asks to change the wake word, voice, or other assistant settings, e.g. 以后叫你小K、换个声音、关掉唤醒。",
    ],
    parameters: Type.Object({
      key: Type.Union([
        Type.Literal("wake_word"),
        Type.Literal("wake_enabled"),
        Type.Literal("tts_voice"),
        Type.Literal("daily_report_time"),
        Type.Literal("provider"),
        Type.Literal("model"),
        Type.Literal("tts_rate"),
        Type.Literal("permission_mode"),
      ], { description: "参数名" }),
      value: Type.String({ description: "参数值，如 小K / zh-CN-YunjianNeural / true / 21:30 / deepseek / kimi-for-coding-highspeed / +30%" }),
    }),
    async execute(_id, params) {
      const cfgPath = path.join(BASE, "config.json");
      let cfg: any = {};
      try {
        cfg = JSON.parse(fs.readFileSync(cfgPath, "utf-8"));
      } catch { /* 用空配置 */ }
      let v: any = params.value;
      if (params.key === "wake_enabled") v = /^(true|1|开|是)$/i.test(String(v));
      cfg[params.key] = v;
      fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2), "utf-8");
      const label: Record<string, string> = {
        wake_word: "唤醒词", wake_enabled: "语音唤醒",
        tts_voice: "音色", daily_report_time: "日报时间",
        provider: "模型提供商", model: "主模型", tts_rate: "语速",
        permission_mode: "权限模式",
      };
      return {
        content: [{ type: "text", text: `${label[params.key]}已改为 ${v}，几秒后生效` }],
        details: {},
      };
    },
  });

  pi.registerTool({
    name: "mcp_add",
    label: "MCP Add",
    description: "安装并热加载一个 MCP 服务（npm 包），立刻可用。需要用户语音确认，且仅限官方 scope 包",
    promptGuidelines: [
      "Use mcp_add when a needed capability is missing and an MCP package could provide it, e.g. @modelcontextprotocol/server-brave-search。安装前必须经用户确认。",
    ],
    parameters: Type.Object({
      package: Type.String({ description: "npm 包名，仅限 @modelcontextprotocol/* 官方包" }),
      args: Type.Optional(Type.Array(Type.String(), { description: "传给服务的参数" })),
      name: Type.Optional(Type.String({ description: "服务别名（默认取包名最后一段）" })),
    }),
    async execute(_id, params) {
      const pkg = String(params.package).trim();
      // 包来源白名单：仅官方 scope（防自我扩权装任意包，如泄 env 的 server-everything）
      if (!pkg.startsWith("@modelcontextprotocol/")) {
        return { content: [{ type: "text", text: `已拒绝：只允许安装 @modelcontextprotocol/* 官方包，${pkg} 不在白名单` }], details: {} };
      }
      if (!/^@modelcontextprotocol\/[a-z0-9-]+$/.test(pkg)) {
        return { content: [{ type: "text", text: `包名不合法: ${pkg}` }], details: {} };
      }
      // 1) 安装到项目本地
      try {
        await runProcess("npm.cmd", ["install", pkg, "--registry=https://registry.npmmirror.com"], 180000, BASE);
      } catch (e: any) {
        return { content: [{ type: "text", text: `npm 安装失败: ${e.message}` }], details: {} };
      }
      // 2) 定位入口
      const pkgDir = path.join(BASE, "node_modules", ...pkg.split("/"));
      let entry = "";
      try {
        const pj = JSON.parse(fs.readFileSync(path.join(pkgDir, "package.json"), "utf-8"));
        const bin = typeof pj.bin === "string" ? pj.bin : (pj.bin ? Object.values(pj.bin)[0] : null);
        entry = bin || pj.main || "dist/index.js";
      } catch {
        entry = "dist/index.js";
      }
      const entryPath = path.join(pkgDir, String(entry));
      if (!fs.existsSync(entryPath)) {
        return { content: [{ type: "text", text: `装好了但找不到入口 ${entry}` }], details: {} };
      }
      // 3) 写入 mcp.json 并热加载
      const name = params.name ?? pkg.split("/").pop()!;
      const nodeExe = process.execPath;
      const mcpCfgPath = path.join(BASE, "mcp.json");
      let mcpCfg: any = { servers: {} };
      try { mcpCfg = JSON.parse(fs.readFileSync(mcpCfgPath, "utf-8")); } catch { /* 用空配置 */ }
      mcpCfg.servers = mcpCfg.servers ?? {};
      mcpCfg.servers[name] = { command: nodeExe, args: [entryPath, ...(params.args ?? [])] };
      fs.writeFileSync(mcpCfgPath, JSON.stringify(mcpCfg, null, 2), "utf-8");
      try {
        const loader = (globalThis as any).__mcpLoadServer;
        const count = await loader(pi, name, mcpCfg.servers[name]);
        logEvent("mcp/added", { name, package: pkg, tools: count });
        return { content: [{ type: "text", text: `MCP 服务 ${name} 已装好并热加载，注册了 ${count} 个工具，立即可用` }], details: { tools: count } };
      } catch (e: any) {
        return { content: [{ type: "text", text: `安装成功但热加载失败: ${e.message}（重启后可用）` }], details: {} };
      }
    },
  });

  pi.registerTool({
    name: "list_skills",
    label: "List Skills",
    description: "列出已固化的技能（含使用次数、最近使用、归档状态）",
    promptGuidelines: ["Use list_skills when the user asks what skills exist or about skill lifecycle."],
    parameters: Type.Object({}),
    async execute() {
      const readDir = (dir: string) => {
        try {
          return fs.readdirSync(dir).filter((f) => f.endsWith(".json"));
        } catch {
          return [] as string[];
        }
      };
      const lines: string[] = [];
      for (const f of readDir(SKILLS_DIR)) {
        try {
          const d = JSON.parse(fs.readFileSync(path.join(SKILLS_DIR, f), "utf-8"));
          lines.push(`[在用] ${d.name}（用 ${d.use_count ?? 0} 次，最近 ${d.last_used ?? "未用"}）— ${d.description}`);
        } catch { /* 跳过 */ }
      }
      const archiveDir = path.join(SKILLS_DIR, "_archive");
      for (const f of readDir(archiveDir)) {
        try {
          const d = JSON.parse(fs.readFileSync(path.join(archiveDir, f), "utf-8"));
          lines.push(`[归档] ${d.name}（用 ${d.use_count ?? 0} 次，最近 ${d.last_used ?? "未用"}）— ${d.description}`);
        } catch { /* 跳过 */ }
      }
      return { content: [{ type: "text", text: lines.join("\n") || "还没有固化技能" }], details: {} };
    },
  });

  pi.registerTool({
    name: "restore_skill",
    label: "Restore Skill",
    description: "把归档的技能恢复到在用状态",
    promptGuidelines: ["Use restore_skill when the user wants an archived skill back."],
    parameters: Type.Object({
      name: Type.String({ description: "技能名" }),
    }),
    async execute(_id, params) {
      const src = path.join(SKILLS_DIR, "_archive", `${params.name}.json`);
      if (!fs.existsSync(src)) {
        return { content: [{ type: "text", text: `归档里没有 ${params.name}` }], details: {} };
      }
      const def = JSON.parse(fs.readFileSync(src, "utf-8"));
      fs.renameSync(src, skillPath(params.name));
      registerDynamicSkill(def);
      logEvent("skill/restored", { name: params.name });
      return { content: [{ type: "text", text: `技能 ${params.name} 已恢复并立即可用` }], details: {} };
    },
  });

  pi.registerTool({
    name: "remember",
    label: "Remember",
    description: "把用户让你记住的事、偏好、习惯写进长期记忆文件",
    promptGuidelines: [
      "Use remember when the user says 记住/以后/我喜欢 or asks you to remember something.",
    ],
    parameters: Type.Object({
      fact: Type.String({ description: "要记住的内容，一句话" }),
    }),
    async execute(_id, params) {
      const line = `- ${params.fact}（${new Date().toISOString().slice(0, 10)}）\n`;
      fs.appendFileSync(MEMORY_FILE, line, "utf-8");
      return { content: [{ type: "text", text: "已记住" }], details: {} };
    },
  });

  pi.registerTool({
    name: "teach_skill",
    label: "Teach Skill",
    description: "把一种常用做法固化成新技能：一段 PowerShell 脚本，以后说出对应的话就能用",
    promptGuidelines: [
      "Use teach_skill when the user asks to 固化/记住这个做法 or wants a reusable skill created.",
    ],
    parameters: Type.Object({
      name: Type.String({ description: "技能英文名，小写下划线，如 empty_downloads" }),
      description: Type.String({ description: "一句话说明这个技能是什么" }),
      when_to_use: Type.String({ description: "路由用：什么情况该调用它，如 用户让清理下载文件夹时" }),
      script: Type.String({ description: "PowerShell 脚本；如需用户输入用 $env:SKILL_INPUT 读取" }),
    }),
    async execute(_id, params) {
      const def: SkillDef = {
        name: params.name,
        description: params.description,
        when_to_use: params.when_to_use,
        script: params.script,
      } as SkillDef & { created: string; use_count: number };
      (def as any).created = new Date().toISOString().slice(0, 10);
      (def as any).use_count = 0;
      fs.writeFileSync(
        path.join(SKILLS_DIR, `${def.name}.json`),
        JSON.stringify(def, null, 2),
        "utf-8",
      );
      registerDynamicSkill(def); // 立即生效，不用重启
      return { content: [{ type: "text", text: `技能 ${def.name} 已固化并立即可用` }], details: {} };
    },
  });

  pi.registerTool({
    name: "set_reminder",
    label: "Set Reminder",
    description: "定时提醒：N 分钟后用语音提醒用户一件事",
    promptGuidelines: ["Use set_reminder when the user asks to be reminded of something."],
    parameters: Type.Object({
      minutes: Type.Number({ description: "多少分钟后提醒" }),
      text: Type.String({ description: "提醒内容" }),
    }),
    async execute(_id, params) {
      const dueTs = Date.now() + Number(params.minutes) * 60000;
      fs.appendFileSync(
        REMINDERS_FILE,
        JSON.stringify({ due_ts: dueTs, text: params.text }) + "\n",
        "utf-8",
      );
      return { content: [{ type: "text", text: `将于 ${new Date(dueTs).toLocaleTimeString()} 提醒` }], details: {} };
    },
  });

  pi.registerTool({
    name: "look_at_screen",
    label: "Look At Screen",
    description: "截屏并用 Kimi 视觉大模型看屏幕：回答关于当前屏幕内容的问题",
    promptGuidelines: [
      "Use look_at_screen when the user asks about what is on the screen, e.g. 屏幕上、这个窗口、这个页面、看看这是什么。",
    ],
    parameters: Type.Object({
      question: Type.String({ description: "关于屏幕内容的问题" }),
    }),
    async execute(_id, params) {
      // 1) 截屏（缩到 1280 宽，省流量省时间）
      const imgPath = path.join(os.tmpdir(), "agent_screen.jpg");
      const shotScript = `
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
$b = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size)
$scale = 1024.0 / $b.Width
if ($scale -lt 1) {
  $small = New-Object System.Drawing.Bitmap 1024, ([int]($b.Height * $scale))
  $g2 = [System.Drawing.Graphics]::FromImage($small)
  $g2.InterpolationMode = 'HighQualityBicubic'
  $g2.DrawImage($bmp, 0, 0, 1024, ([int]($b.Height * $scale)))
  $small.Save('${psq(imgPath)}', [System.Drawing.Imaging.ImageFormat]::Jpeg)
  $g2.Dispose(); $small.Dispose()
} else {
  $bmp.Save('${psq(imgPath)}', [System.Drawing.Imaging.ImageFormat]::Jpeg)
}
$g.Dispose(); $bmp.Dispose()
Write-Output 'ok'`;
      await runPowerShell(shotScript, 30000);

      // 2) 调 Kimi 视觉模型
      const b64 = fs.readFileSync(imgPath).toString("base64");
      const resp = await fetch("https://api.kimi.com/coding/v1/chat/completions", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: "Bearer " + KIMI_KEY + "",
        },
        body: JSON.stringify({
          model: "kimi-for-coding-highspeed",
          thinking: { type: "disabled" },
          max_tokens: 400,
          messages: [
            {
              role: "user",
              content: [
                { type: "image_url", image_url: { url: `data:image/jpeg;base64,${b64}` } },
                { type: "text", text: params.question + "（用简短口语化的中文回答，一两句话）" },
              ],
            },
          ],
        }),
      });
      const data: any = await resp.json();
      const text = data?.choices?.[0]?.message?.content ?? `视觉调用失败: HTTP ${resp.status}`;
      return { content: [{ type: "text", text }], details: {} };
    },
  });

  // ---------- 后台任务注册表（委托 kimi code 不再阻塞对话） ----------
  interface Job {
    id: string;
    task: string;
    proc: any;
    output: string;
    status: "running" | "done" | "failed" | "killed";
    startedAt: number;
    finishedAt?: number;
  }
  const jobs = new Map<string, Job>();
  let jobSeq = 0;

  function startJob(task: string): Job {
    jobSeq += 1;
    const id = `kimi-${jobSeq}`;
    const proc = spawn(KIMI_EXE, ["-p", task, "--output-format", "text"], {
      windowsHide: true,
      cwd: "D:\\Desktop",
    });
    const job: Job = { id, task, proc, output: "", status: "running", startedAt: Date.now() };
    jobs.set(id, job);
    proc.stdout.on("data", (d: any) => { job.output += d.toString(); });
    proc.stderr.on("data", (d: any) => { job.output += d.toString(); });
    proc.on("close", (code: number) => {
      job.status = code === 0 ? "done" : "failed";
      job.finishedAt = Date.now();
      logEvent("job/done", { id, task: task.slice(0, 60), status: job.status });
      // 完成通知：闲时唤醒一个后续轮次，由模型亲口播报（不打断当前对话）
      try {
        const tail = job.output.trim().slice(-600) || "(无输出)";
        pi.sendMessage(
          {
            customType: "job-done",
            content: `（系统消息）你刚才启动的后台任务 ${id}（${task.slice(0, 40)}）已结束，` +
              `状态 ${job.status}。输出摘要：${tail}。请用一两句话向用户播报结果。`,
            display: true,
          },
          { triggerTurn: true, deliverAs: "followUp" },
        );
      } catch { /* 进程已退出也没关系 */ }
    });
    logEvent("job/start", { id, task: task.slice(0, 60) });
    return job;
  }

  pi.registerTool({
    name: "delegate_to_kimi",
    label: "Delegate to Kimi",
    description: "把成规模的任务（多步操作/写代码/批量处理/系统改造）交给 kimi code 在后台全自动执行，立即返回任务号，完成后自动播报结果",
    promptGuidelines: [
      "Use delegate_to_kimi for complex multi-step tasks; it runs in background and notifies on completion. 启动后告诉用户任务已在后台跑。",
    ],
    parameters: Type.Object({
      task: Type.String({ description: "要执行的任务，描述清楚目标和验收标准" }),
    }),
    async execute(_id, params) {
      try {
        const job = startJob(params.task);
        return {
          content: [{
            type: "text",
            text: `后台任务 ${job.id} 已启动，完成后会自动播报结果。期间可以继续聊天；` +
              `想查进度用 job_output，想中止用 job_kill。`,
          }],
          details: { jobId: job.id },
        };
      } catch (e: any) {
        return { content: [{ type: "text", text: `启动失败: ${e.message}` }], details: {} };
      }
    },
  });

  pi.registerTool({
    name: "job_output",
    label: "Job Output",
    description: "查询后台任务的进度或结果（可等待最多 60 秒）",
    promptGuidelines: ["Use job_output when the user asks about a background task's progress or result."],
    parameters: Type.Object({
      job_id: Type.String({ description: "任务号，如 kimi-1" }),
      wait: Type.Optional(Type.Boolean({ description: "是否等待完成（最多60秒）" })),
    }),
    async execute(_id, params) {
      const job = jobs.get(params.job_id);
      if (!job) {
        return { content: [{ type: "text", text: `没有任务 ${params.job_id}` }], details: {} };
      }
      if (params.wait && job.status === "running") {
        const deadline = Date.now() + 60000;
        while (job.status === "running" && Date.now() < deadline) {
          await new Promise((r) => setTimeout(r, 1000));
        }
      }
      const tail = job.output.trim().slice(-1500) || "(暂无输出)";
      return {
        content: [{
          type: "text",
          text: `任务 ${job.id} 状态 ${job.status}（已运行 ${Math.round(((job.finishedAt ?? Date.now()) - job.startedAt) / 1000)}s）\n${tail}`,
        }],
        details: { status: job.status },
      };
    },
  });

  pi.registerTool({
    name: "job_kill",
    label: "Job Kill",
    description: "中止一个正在运行的后台任务",
    promptGuidelines: ["Use job_kill when the user asks to stop or cancel a background task."],
    parameters: Type.Object({
      job_id: Type.String({ description: "任务号，如 kimi-1" }),
    }),
    async execute(_id, params) {
      const job = jobs.get(params.job_id);
      if (!job) {
        return { content: [{ type: "text", text: `没有任务 ${params.job_id}` }], details: {} };
      }
      if (job.status === "running") {
        try {
          job.proc.kill();
          job.status = "killed";
          job.finishedAt = Date.now();
          logEvent("job/kill", { id: job.id });
        } catch { /* ignore */ }
      }
      return { content: [{ type: "text", text: `任务 ${job.id} 已中止` }], details: {} };
    },
  });

  pi.registerTool({
    name: "job_list",
    label: "Job List",
    description: "列出所有后台任务及状态",
    promptGuidelines: ["Use job_list when the user asks what background tasks are running."],
    parameters: Type.Object({}),
    async execute() {
      if (jobs.size === 0) {
        return { content: [{ type: "text", text: "当前没有后台任务" }], details: {} };
      }
      const lines = [...jobs.values()].map((j) =>
        `${j.id} [${j.status}] ${j.task.slice(0, 40)}（${Math.round(((j.finishedAt ?? Date.now()) - j.startedAt) / 1000)}s）`,
      );
      return { content: [{ type: "text", text: lines.join("\n") }], details: {} };
    },
  });

}
