# TOOLS.md

## 技能

- `skills/undercover-game-referee`：本局主持的全部内容。`scripts/undercover.py` 是事实层，`references/` 是阶段机、命令参考、协作形状和主持稿要求。**每次被唤醒都按它执行，不即兴发挥。**
- `skills/bcs-coordination`：BCS 命令行的用法。认证、`collaborate permission`、`collaborate run`、`session` 系列命令一律以它为准。

## 事实层脚本

```bash
SKILL_DIR="${OPENCLAW_WORKSPACE_DIR:-$PWD}/skills/undercover-game-referee"
uc() { python3 "$SKILL_DIR/scripts/undercover.py" "$@"; }
```

`init` 开局、`status` 看阶段、`render-speak-run` / `render-vote-run` 渲染协作、`speeches-set` / `votes-set` 收产物并判定、`render-ping` 渲染遗言任务、`reveal` 终局公布。详见 `references/commands.md`。

第一次使用前确认 `python3 --version` 可用；不可用就直接告诉人类，不要退化成手工主持。

## 协同工具

- `bcs_assign_task(target_bot, message)`：派遗言或预备任务。目标用 Bot 名称。
- `bcs_task_complete(summary)`：**只在整局结束时调用一次**，它会结束当前 session。中途绝不调用。
- 具体调用面（`mcporter call`、原生 MCP 还是原生 tool）以 BCS 注入的本群协同上下文为准。

## 工具边界

- 不读取、不打印、不传递 token；不设置 `BCN_BOT_TOKEN`，不传 `--token`。
- 不覆盖已有的 `BOT_DATA_DIR`。
- 不用 `curl` 直接打 BCS HTTP 接口绕过 CLI。
- 不用 `cat` / `head` 打开渲染出来的 YAML 和状态文件。
- 不用 `bcs chat` 给 Bot 玩家发消息（那是一对一会话，会脱离本局）。
