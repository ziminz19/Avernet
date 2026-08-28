# 脚本命令参考

`scripts/undercover.py` 是本局事实层的全部实现。所有命令都要 `--session`，都输出 JSON，第一个字段是 `ok`。

```bash
SKILL_DIR="${OPENCLAW_WORKSPACE_DIR:-$PWD}/skills/undercover-game-referee"
uc() { python3 "$SKILL_DIR/scripts/undercover.py" "$@"; }
```

失败时 `ok: false`，带 `error` 和 `message`，退出码 2。**看到失败就停下并向人类说明，不要绕过它继续。**

## 脱敏约定

| 命令 | 输出里有词吗 |
| --- | --- |
| `init` `status` `speeches-set` `votes-set` `parse-vote` `render-ping` `render-vote-watchdog` | **没有**，可以放心引用 |
| `render-speak-run` `render-vote-run` | 只有文件路径，词在文件里，我不读也不贴 |
| `reveal` | 有，且只有终局才能调 |

## 命令

### `init`

```bash
uc init --session S --group G --human human_123 --referee-uuid $BCN_BOT_UUID \
  --bot "玩家稳健老陈=uuid1" --bot "玩家话痨小满=uuid2" \
  [--difficulty easy|medium|hard] [--undercover 1] [--max-rounds 6] [--force]
```

抽词对、随机座位、随机身份，一次做完。返回 `seating`（座位号 + 名字 + 是不是人类）、`human_seat`。
同一局重复调会被拒，除非 `--force`。

### `status`

返回 `phase`、`round`、`alive`、`eliminated`、`pending_ping`、`renders`、`result`、`next_action`。**每次被唤醒的第一件事。**

`renders` 是本轮各运行渲染过几次，例如 `{"speak": 1, "vote": 2}` 表示投票重开过一次。

### `render-speak-run`

```bash
uc render-speak-run --session S [--retry]
```

只能在 `AWAIT_START` 或 `AWAIT_NEXT_ROUND` 调。**渲染即推进**：它自己把轮次加一、建好本轮记录、把 phase 推到 `SPEAK_RUNNING`。

返回 `yaml_path`、`input_path`、`bindings`、`binding_args`、`speaking_order`、`attempt`。

`--retry` 用来重开当前这一轮：只能在 `SPEAK_RUNNING` 且本轮发言还没收齐时调，**轮次不推进、本轮记录不重建**，只是把同一份 YAML 重渲染一次。上一次提交的运行失败了才用它——运行失败不会唤醒我，所以这条路只会从 SX 卡住诊断走进来。

### `speeches-set`

```bash
uc speeches-set --session S --json '{"1":"...","2":"..."}' [--flag 3=谈论了身份]
```

只能在 `SPEAK_RUNNING` 调。座位号必须和本轮存活名单完全一致。

对每条发言做泄词判定并遮蔽，返回 `speeches[]`：`seat` / `player` / `kind` / `text`（**遮蔽后的可展示文本**）/ `violation`。phase 推到 `AWAIT_VOTE_START`。

泄词判定三种，和告诉玩家的规则一字不差：说出完整词、说出词里连续两个字、把词拆散了说全。单个常用字命中不算违规。

`--flag` 用来补脚本判不了的语义违规（谈身份、点评他人）。

### `render-vote-run`

```bash
uc render-vote-run --session S [--retry]
```

只能在 `AWAIT_VOTE_START` 调，同样渲染即推进，phase 到 `VOTE_RUNNING`。返回字段同 `render-speak-run`，外加 `voters`。

`--retry` 用来重开当前这一轮的投票，只能在 `VOTE_RUNNING` 调。**没有它，卡住诊断走不通**——不带 `--retry` 时阶段卫兵只认 `AWAIT_VOTE_START`，而这时 phase 早就是 `VOTE_RUNNING` 了。重开后之前投过的票作废，要向人类说明。

### `votes-set`

```bash
uc votes-set --session S --json '{"1":"我投3号，...","2":"我弃权"}'
```

只能在 `VOTE_RUNNING` 调。一次做完解析、计票、平票规则、出局、胜负判定。返回：

- `votes[]`：`seat` / `player` / `text`（遮蔽后）/ `target_seat` / `target_player` / `violation` / `note`
- `counts[]`：按票数降序的 `seat` / `player` / `votes`
- `tie`、`forced_by_repeat_tie`、`eliminated`、`alive[]`
- `verdict`：`continue` 或 `finished`；`winner`、`win_reason`
- `ping`：继续时给出该派谁、派什么类型
- phase 推到 `AWAIT_NEXT_ROUND` 或 `FINISHED`

投票内容如果泄词，该票直接作废（`target_seat` 为 null）。

### `render-ping`

只能在有 `pending_ping` 时调。返回 `kind`（`eulogy` 遗言 / `standby` 预备）、`target_bot`、`message`。
用 `bcs_assign_task` 把 `message` 原样发给 `target_bot`。**这条回执是下一轮的唤醒源。**

### `render-vote-watchdog`

```bash
uc render-vote-watchdog --session S
```

只能在 `VOTE_RUNNING` 调。渲染一条投票期间的兜底唤醒任务——投票运行失败不会唤醒我，
这条回执是唯一不依赖人类的唤醒源。

- `available: true` → 带 `target_bot` 和 `message`，用 `bcs_assign_task` 原样发过去。
  目标一定是**已出局**的 Bot：它在本轮投票运行里没有任何节点，通道整场空着。
- `available: false` → 第一轮还没人出局，没有安全人选。**绝不改派给存活玩家**，
  那等于在别人的通道里塞第二件事，正是要避免的那个毛病。这一轮的兜底只能是人类。

收到「看门狗回执」时不要当成普通迟到回执：走一遍 SX 卡住诊断。

### `reveal`

只能在 `FINISHED` 调。返回 `winner`、`win_reason`、`words`、每个座位的身份与词、每轮的发言与投票流向。

### `parse-vote`

```bash
uc parse-vote --session S --voter 1 --text "我投4号"
```

单独解析一句投票，调试和兜底用。正常流程里不需要——`votes-set` 内部用的就是同一套解析。

## 状态文件

路径是 `$BOT_DATA_DIR/undercover-game/<session_id>.json`，一个 session 一局。
渲染出来的 YAML 和输入放在 `$BOT_DATA_DIR/undercover-game/work/<session_id>/`。

**永远不要手工编辑状态文件，也不要用 `cat` 把它贴出来** —— 里面有全部答案。需要什么信息就调对应命令。
