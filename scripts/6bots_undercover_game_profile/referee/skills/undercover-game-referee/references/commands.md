# 脚本命令参考

`scripts/undercover.py` 是本局事实层的全部实现。所有命令都要 `--session`，都输出 JSON，第一个字段是 `ok`。

```bash
SKILL_DIR="${OPENCLAW_WORKSPACE_DIR:-$PWD}/skills/undercover-game-referee"
uc() { python3 "$SKILL_DIR/scripts/undercover.py" "$@"; }
```

失败时 `ok: false`，带 `error` 和 `message`，退出码 2。**看到失败就停下并向人类说明，不要绕过它继续。**

**命令的输出也是公开的。** 我的每一次工具调用和它的输出都会被转发成群里的事件，
所以默认输出都压成了一行、只留必要字段。要详细的再加 `--full`。同理，能一条命令
做完的就别拆成三条——每多一个来回，人类就多看到一堆跟游戏无关的东西。

## 三条复合命令（日常只用这三条）

| 命令 | 顶掉了什么 | 用在哪 |
| --- | --- | --- |
| `begin` | 三条 `bcs session get \| jq` + 找自己 UUID | S0 |
| `open-round` | `collaborate permission` + `render-speak-run` + `collaborate run` | S1 / S5 / SX |
| `open-vote` | `collaborate permission` + `render-vote-run` + `collaborate run` + `render-vote-watchdog` | S3 / SX |

它们会自己调 `bcs-cli`（认证仍由 CLI 负责，脚本不碰 token）。下面的 `render-*` 是拆开的
底层命令，只在复合命令因为环境问题跑不通时手工用。

## 脱敏约定

| 命令 | 输出里有词吗 |
| --- | --- |
| `init` `status` `speeches-set` `votes-set` `parse-vote` `render-ping` `render-vote-watchdog` | **没有**，可以放心引用 |
| `render-speak-run` `render-vote-run` | 只有文件路径，词在文件里，我不读也不贴 |
| `reveal` | 有，且只有终局才能调 |

## 命令

### `begin`

```bash
uc begin --session S --group G [--difficulty medium] [--undercover 1] [--max-rounds 6]
```

开局前的全部探测一次做完：人类在不在会话里、是不是 Present、有哪些 Bot 玩家、我自己的
`bot_uuid` 是什么。返回 `human_present`、`human_actor_id`、`bots[]`、`referee_uuid`，
以及一条可以原样跑的 `init_command`。

- `human_present: false` → 说"加入提示"那段，结束激活。**人类参与者的 mode 缺省是
  `absent` 不是 `present`**，字段缺失按 absent 处理，这条判断在脚本里。
- `human_present: true` → 说开场白，等人类回一句"开始"。**这一步不 init。**

**不要再去读 `BCN_BOT_UUID`。** 这套部署里它是空的，而 `bot_uuid` 实际上就等于 Bot 名称。
脚本按 `BCN_BOT_UUID` → `$BOT_DATA_DIR/.bcs/session.json` 的顺序自己解析。

### `init`

```bash
uc init --session S --group G --human human_123 \
  --bot "玩家稳健老陈=uuid1" --bot "玩家话痨小满=uuid2" \
  [--referee-uuid X] [--difficulty easy|medium|hard] [--undercover 1] [--max-rounds 6] [--force]
```

抽词对、随机座位、随机身份，一次做完。返回 `seating`（座位号 + 名字 + 是不是人类）、`human_seat`、
`referee_uuid`。同一局重复调会被拒，除非 `--force`。

`--referee-uuid` 可以不给，脚本自己解析；只有解析不出来时才需要显式传。

### `status`

默认返回压成一行的四件事：`phase`、`round`（"1/6"）、`alive`、`pending_ping`、`next_action`。
**每次被唤醒的第一件事。**

`--full` 额外给 `eliminated`、`human_seat`、`renders`、`result`。`renders` 是本轮各运行渲染
过几次，例如 `{"speak": 1, "vote": 2}` 表示投票重开过一次——只在卡住诊断时需要。

### `open-round`

```bash
uc open-round --session S [--retry]
```

开一轮发言：查协作槽位 → 渲染 → 提交，一条命令做完。返回 `round`、`attempt`、
`submitted`、`run_id`。

合成这一条是为了把「提交」和「激活结束」之间的距离压到零：入口节点是我自己的，
提交之后我每多花一个来回，本轮开场就晚一个来回。**跑完它不要再说话，立刻结束激活**
——回合开场是入口节点的产物，我马上会被它叫醒。

槽位被占时返回 `RUN_SLOT_BUSY`，**这时状态没有任何改动**，可以安全重试。

### `open-vote`

```bash
uc open-vote --session S [--retry]
```

同上，外加一个 `watchdog` 字段（内容同 `render-vote-watchdog`）。提交完只剩一件事：
`watchdog.available` 为真就用 `bcs_assign_task` 把 `watchdog.message` 发给
`watchdog.target_bot`，然后结束激活。开投稿同样是入口节点的产物，不用我说。

### `render-speak-run`

```bash
uc render-speak-run --session S [--retry]
```

只能在 `AWAIT_START` 或 `AWAIT_NEXT_ROUND` 调。**渲染即推进**：它自己把轮次加一、建好本轮记录、把 phase 推到 `SPEAK_RUNNING`。

返回 `yaml_path`、`input_path`、`binding_args`、`attempt`，以及一条拼好的 `run_command`。
**只在 `open-round` 跑不通、需要手工提交时才用它。**

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

只能在 `AWAIT_VOTE_START` 调，同样渲染即推进，phase 到 `VOTE_RUNNING`。返回字段同
`render-speak-run`。**只在 `open-vote` 跑不通、需要手工提交时才用它。**

`--retry` 用来重开当前这一轮的投票，只能在 `VOTE_RUNNING` 调。**没有它，卡住诊断走不通**——不带 `--retry` 时阶段卫兵只认 `AWAIT_VOTE_START`，而这时 phase 早就是 `VOTE_RUNNING` 了。重开后之前投过的票作废，要向人类说明。

### `votes-set`

```bash
uc votes-set --session S --json '{"1":"我投3号","2":"我弃权"}'
```

只能在 `VOTE_RUNNING` 调。一次做完解析、计票、平票规则、出局、胜负判定。返回：

- `votes[]`：`seat` / `player` / `text`（**规范化后的票面**）/ `target_seat` / `target_player` / `violation` / `note`
- `counts[]`：按票数降序的 `seat` / `player` / `votes`
- `tie`、`eliminated`、`alive[]`
- `verdict`：`continue` 或 `finished`；`winner`、`win_reason`
- `ping`：继续时给出该派谁、派什么类型
- phase 推到 `AWAIT_NEXT_ROUND` 或 `FINISHED`

`text` 一律是规范化的票面——`我投N号`、`我弃权` 或 `无效票`，**永远不是玩家的原话**。
玩家这一轮只被要求交票号，但指令是软的：万一有谁多写了半句理由，这里也会被抹掉，
所以主持稿里不可能出现投票理由。我拿不到原话，也不许去猜他们为什么这么投。

投票内容如果泄词，该票直接作废（`target_seat` 为 null，`text` 是 `无效票`）。
超字不作废，只在 `violation` 里记一笔。

**`tie` 为真就是平票：本轮没有人出局，直接进下一轮。** 没有重投，也不会在平票者里
随机挑人——`eliminated` 一定是 null。这是有意的：平票不减员但照样烧掉一轮，而轮数
用完判卧底赢，所以平票是纯粹的平民损失。要调平衡就调 `--max-rounds`。

### `render-ping`

只能在有 `pending_ping` 时调。返回 `kind`（`eulogy` 遗言 / `standby` 预备）、`target_bot`、`message`。
用 `bcs_assign_task` 把 `message` 原样发给 `target_bot`。**这条回执是下一轮的唤醒源。**

**不要在开票节点里做这件事。** 开票节点只负责念稿；派唤醒源在开票稿发出去之后的那次
激活里做（S4b）。判据是 `status` 里 `phase = AWAIT_NEXT_ROUND` 且 `pending_ping` 非空。

派任务只有 `bcs_assign_task` 这一个办法。**不要去查 `bcs --help`，不要用 `bcs chat`**
——那是一对一会话，会脱离本局，而且它的回执不是任务回执。

### `render-vote-watchdog`

```bash
uc render-vote-watchdog --session S
```

只能在 `VOTE_RUNNING` 调；正常流程里由 `open-vote` 顺带返回，不用单独跑。
渲染一条投票期间的兜底唤醒任务——投票运行失败不会唤醒我，
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
