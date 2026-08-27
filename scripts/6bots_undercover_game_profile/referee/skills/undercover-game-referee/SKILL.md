---
name: undercover-game-referee
description: 在 Avernet 主从协作群里主持一整局"谁是卧底"。覆盖随机发牌、按轮编排发言与投票的临时自定义协作、计票判定出局与胜负、以及面向人类玩家的主持稿。当自己是本群 manager 且协作目标是谁是卧底时使用。
allowed-tools:
  - exec
---

# 谁是卧底 · 主持人技能

## 适用条件

同时满足才使用本技能：

- 当前群是 manager-worker 群，且我是 manager。
- 群组的协作目标 / session 输入是"谁是卧底"这类游戏。

不满足时不要套用本技能的任何内容。

---

## 最重要的一条：事实层和表演层是分开的

| | 事实层 | 表演层 |
| --- | --- | --- |
| 是什么 | 状态文件、协作节点产物、脚本的判定结果 | 人类玩家在群聊里看到的每一句话 |
| 谁负责 | `scripts/undercover.py` | 我，用主持人的口吻 |

**两条铁律：**

1. **我不手写任何玩家的词，也不手算任何游戏状态。** 抽词、排座位、分身份、生成协作 YAML、检查泄词、解析投票、计票、判胜负，全部由脚本完成。我只负责把渲染好的东西交给工具，以及把脚本返回的结果说成人话。
2. **脚本跑不通时，绝不凭记忆推演游戏。** 向人类说明当前状况并停在原地。宁可卡住，也不能编造票数、发言或身份。

脚本本身也在保护我：除 `render-*` 和 `reveal` 外，**任何子命令的输出都不含词语和身份**，所以即使把命令输出原样贴进群聊也不会泄密。而 `render-speak-run` / `render-vote-run` 把含词的 YAML 写进文件、只打印路径——发言和投票的词一次都不经过我的输出通道。

---

## 运行前准备

沿用 `bcs-coordination` 的环境约定，不要改写它：

```bash
export BOT_DATA_DIR="${BOT_DATA_DIR:-${OPENCLAW_DATA_DIR:-$HOME/.openclaw}}"
export BCS_API_BASE_URL="${BCS_API_BASE_URL:-http://127.0.0.1:21000}"

bcs() {
  BOT_DATA_DIR="$BOT_DATA_DIR" bcs-cli --url "$BCS_API_BASE_URL" "$@"
}

SKILL_DIR="${OPENCLAW_WORKSPACE_DIR:-$PWD}/skills/undercover-game-referee"
uc() { python3 "$SKILL_DIR/scripts/undercover.py" "$@"; }
```

认证由 CLI 负责，不读取、不拼接、不打印 token。第一次使用时确认 `python3 --version` 可用；不可用就直接告诉人类，不要退化成手工主持。

---

## 一次唤醒 = 一步

每次被唤醒，固定四步：

1. `uc status --session "$session_id"` 拿到 `phase` 和 `next_action`。
2. 判断这次唤醒属于哪一类（见下表）。
3. 查 [references/phase-machine.md](references/phase-machine.md) 执行对应的那一步。
4. 说一段主持稿，结束激活。

**不要在一次激活里连做两步。** 自定义协作运行结束**不会**唤醒我，所以每一步都必须把"下一次谁来唤醒我"安排好。这是整局能自己跑起来的唯一原因。

### 唤醒类型

| 特征 | 类型 |
| --- | --- |
| 带 session 启动 / GroupContext 上下文，且还没有状态文件 | `SESSION_START` |
| 发送者是 `human_*` 的群聊消息 | `HUMAN_MSG` |
| 状态机下发的节点任务（正文以`【裁判节点 · …】`开头） | `NODE_TASK` |
| 某个 Bot 玩家的任务回执 | `WORKER_MSG` |

判不出来时按 `HUMAN_MSG` 处理，并在主持稿里说清当前进行到哪、人类可以做什么。

---

## 参考文档

| 场景 | 文档 |
| --- | --- |
| 每个阶段的精确动作 | [references/phase-machine.md](references/phase-machine.md) |
| 脚本命令与返回字段 | [references/commands.md](references/commands.md) |
| 两个协作的形状与为什么是这个形状 | [references/runs.md](references/runs.md) |
| 主持稿要说清什么、什么口吻、不许说什么 | [references/boards.md](references/boards.md) |

---

## 每次开口前自检

1. 这段话里有没有出现任何未出局玩家的词或身份？
2. 我这次激活只推进了一步吗？
3. 脚本已经把这一步落盘了吗（`status` 的 phase 变了没有）？
4. 下一次唤醒有明确来源吗——人类的下一条消息、玩家的任务回执，或我自己被派节点任务？
5. 这段话最后有没有说清"接下来该谁做什么"？

---

## 已知平台限制

- 一个 session 同时只能有一个自定义协作运行；提交前必须 `bcs collaborate permission`，只认服务端返回的 `allowed`。
- 运行中任一节点最终失败会让整个运行失败，而我作为末节点**不会被唤醒**。所以开场时要告诉人类："超过 5 分钟没动静，回我一句『卡住了』。"
- 目前没有取消运行的 CLI 命令。运行卡死时本局只能作废，要如实说。
- 同一时刻只支持一个待处理的人类输入节点，所以一轮里人类只有一个输入位。发言和投票分成两个运行，正是因为这个限制。
- 玩家的任务回执是公开的，人类看得到。遗言里如果说漏嘴，我拦不住——所以遗言任务里明确要求不许提词。
