# devtools/hooks —— DSH 回合结束自动提交

本目录让「每回合结束自动 git commit」由**外部机制**保证，而不是靠 Agent 记得。

## 为什么不能用约定代替机制

Agent 会忘记、会在长任务里把提交推迟到「全做完」——于是中间状态全部丢失，
一个改坏的步骤无法回退。写进 `AGENTS.md` 的约定是**软约束**，
而 hook 是**硬约束**：回合一停就执行，与 Agent 的记性无关。

## 机制

DSH 内置 Claude Code hooks 桥接（`@deepseek-ai/dsh-hooks-claude-code`），
其 `Stop` 事件在「运行即将停止时」触发，正好等于「回合结束」。

```
回合结束 → DSH Stop 事件 → 读 ~/.dsh/hooks/claude-hooks.json
        → 以 stdin 传入 JSON 载荷、在会话工作区目录执行命令
        → python devtools/hooks/auto_commit.py
        → git add -A && git commit
```

## 安装（一次性）

1. 把 `claude-hooks.json` 复制到 `~/.dsh/hooks/claude-hooks.json`：

   ```powershell
   New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.dsh\hooks" | Out-Null
   Copy-Item devtools\hooks\claude-hooks.json "$env:USERPROFILE\.dsh\hooks\claude-hooks.json" -Force
   ```

2. 在 `~/.dsh/profiles/web/cordis.patch.yml` 末尾的 `insert:` 列表里加上：

   ```yaml
     - id: hooks-claude-code
       name: "@deepseek-ai/dsh-hooks-claude-code"
       config:
         configPath: !!js process.env.USERPROFILE + '/.dsh/hooks/claude-hooks.json'
   ```

3. **重启 DSH**。配置在进程启动时只读一次，改完不重启不生效。

改动 `~/.dsh` 下的文件属于 DSH 家目录，需按 `dsh-home-git-commit` skill 单独提交。

## 三个必须具备的守卫

hook 是**进程级全局**配置——它会在**任何项目**里触发。所以脚本里的守卫不是锦上添花，
是防止事故的关键：

| 守卫 | 防止的后果 |
|---|---|
| 项目根必须有哨兵文件 `.dsh-autocommit.json` | 把**别的仓库**一起提交 |
| 无变化时不提交 | 制造大量空提交，污染历史 |
| `.git` 下排他锁（陈旧锁可抢占） | 多会话并发时重复提交 / 死锁导致静默失效 |

哨兵文件是 **opt-in 开关**：`touch .dsh-autocommit.json` 启用，删掉即停用。

## 指定提交信息（可选）

自动生成的提交信息只如实描述「动了哪些区域、多少个文件」，不猜意图。
若想写明确的信息，在项目根放 `.dsh-autocommit-msg`，脚本会采用它并删除该文件：

```powershell
'修复(runtime): ensure_runtime 早退导致指定 pid 被忽略' |
  Set-Content .dsh-autocommit-msg -Encoding utf8
```

## 验证

```powershell
python devtools\verify_autocommit.py
```

覆盖 7 类场景（含「无哨兵不得提交」「并发不得重复提交」），
全部在 `%TEMP%` 的一次性仓库里跑，**不碰真实仓库**。

## 已知限制

- DSH **重启后**才加载 hook 配置；重启前请手动 `git add -A && git commit`。
- Windows 上 Python 默认按 ANSI 代码页编码 stderr（本机实测 cp936），
  而 DSH 按 UTF-8 读，中文会乱码。脚本内已用
  `sys.stderr.reconfigure(encoding="utf-8")` 自持修正，**不依赖 `PYTHONUTF8` 环境变量**。
- `git` 命令统一带 `-c core.quotepath=false`，否则中文文件名会以八进制转义进入提交信息。
- 仅支持普通仓库，不支持 `git worktree`（`.git` 是文件而非目录的场景）。
