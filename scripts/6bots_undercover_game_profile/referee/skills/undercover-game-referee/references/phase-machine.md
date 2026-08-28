# 阶段机

`uc status` 给出 `phase`，唤醒类型给出这一次是谁把我叫醒的。两者一查就知道该做什么。

| phase | 唤醒类型 | 执行 |
| --- | --- | --- |
| 还没有状态文件 | `SESSION_START` | [S0 开局](#s0-开局) |
| 还没有状态文件 | `HUMAN_MSG` | 先做 S0；如果人类这句话就是要开始，紧接着做 S1 |
| `AWAIT_START` | `HUMAN_MSG` | [S1 发牌并开第一轮](#s1-发牌并开第一轮) |
| `SPEAK_RUNNING` | `NODE_TASK`（回合开场） | 按节点指令说一句开场，**不调任何脚本**，结束激活 |
| `SPEAK_RUNNING` | `NODE_TASK`（本轮发言汇总） | [S2 念发言并请人类准备投票](#s2-念发言并请人类准备投票) |
| `SPEAK_RUNNING` | `HUMAN_MSG` | 人类在催或在闲聊：回一句现在等谁；说"卡住了"就走 [SX](#sx-卡住诊断) |
| `AWAIT_VOTE_START` | `HUMAN_MSG` | [S3 开投](#s3-开投) |
| `VOTE_RUNNING` | `NODE_TASK`（开投） | 按节点指令说一句开投，**不调任何脚本**，结束激活 |
| `VOTE_RUNNING` | `NODE_TASK`（开票） | [S4 开票](#s4-开票) |
| `VOTE_RUNNING` | `WORKER_MSG`（看门狗回执） | 走 [SX](#sx-卡住诊断) 查一遍；没卡住就回一句还在等谁 |
| `VOTE_RUNNING` | `HUMAN_MSG` | 回一句还在等谁投；说"卡住了"就走 [SX](#sx-卡住诊断) |
| `AWAIT_NEXT_ROUND` **且 `pending_ping` 非空** | 任意 | [S4b 派下一轮的唤醒源](#s4b-派下一轮的唤醒源) |
| `AWAIT_NEXT_ROUND` | `WORKER_MSG` | [S5 念遗言并开下一轮](#s5-念遗言并开下一轮) |
| `AWAIT_NEXT_ROUND` | `HUMAN_MSG` | 人类说"继续"就跳过遗言，直接做 S5 的第 2 步 |
| `FINISHED` | 任意 | 说明本局已结束；想再来一局在这个协作群里新建一个会话 |

任何格子里没写的组合：说清当前进行到哪、人类可以做什么，**不推进**。

`SPEAK_RUNNING` / `VOTE_RUNNING` 下的 `WORKER_MSG` 一律是迟到的回执，回一句无关紧要的话即可，**绝不推进阶段、绝不调用任何脚本命令**。唯一的例外是「看门狗回执」——那条是我自己派出去当闹钟的，见 SX。

**被节点唤醒时，先用 `uc status` 核对这个节点属不属于当前阶段。** 运行失败之后，失败前派出去的节点仍可能迟到几十秒才把产物送回来。对不上就只回一句"这条是迟到的回执，已忽略"，不调脚本、不念稿——照着它往下走会让人类以为一切正常。

**`ECHO`（发送者是我自己）也先查 `uc status`：** 只有「`AWAIT_NEXT_ROUND` 且 `pending_ping` 非空」这一种要做事（S4b），其余一律直接结束激活，不输出、不调脚本。

---

## 每一步只有一条命令

这一节的每个阶段都被压成**一条 `uc` 命令 + 一段话**，不是为了好看：

- 我的每一次工具调用和它的输出都会被转发成群里的事件。多一个来回，人类就多看到一堆跟游戏无关的东西。
- 两个运行的入口节点都是**我自己的**。运行是在我这次激活里同步提交的，入口节点在提交那一刻就排进我自己的通道，等我让路。**提交之后我每多花一个来回，开场就晚一个来回。**

所以：**提交类命令必须是本次激活的最后一个工具调用。** 提交完不要再查状态、不要再念稿——开场稿由入口节点自己产出。

---

## S0 开局

一条命令查完人类在不在、有哪些 Bot、我自己的 `bot_uuid` 是什么：

```bash
uc begin --session "$session_id" --group "$group_id"
```

配置项从 session 输入 / 群组协作目标里解析，用 `--difficulty` / `--undercover` / `--max-rounds` 传给它；缺项用默认值，不要为配置追问玩家。

- `human_present: false` → 说"加入提示"那段，结束激活。人类点完"加入当前会话"会发消息过来，届时重走 S0。
- `human_present: true` → 记下返回里的 `init_command`，说开场白，请人类回一句"开始"。**这一步不 init**——人类可能还想先问问规则。

自己的 `bot_uuid` 不用查、不用猜，也不要去读 `BCN_BOT_UUID`（这套部署里它是空的）。脚本自己认得出来。

## S1 发牌并开第一轮

一次 exec 里两条命令，中间不说话：

```bash
uc init --session "$session_id" --group "$group_id" --human "$human_actor_id" \
  --bot "玩家稳健老陈=$u1" --bot "玩家话痨小满=$u2" \
  --difficulty medium --undercover 1 --max-rounds 6

uc open-round --session "$session_id"
```

第一条抽词、排座位、分身份，输出只有座位表，没有词也没有身份。第二条查协作槽位、渲染 YAML、提交运行，一步做完。

**不要打开任何 YAML，不要把它的内容贴进任何地方。** 里面有全部玩家的词。

**提交完什么都不用说。** 本轮的"回合开场"是运行入口节点的产物，我马上会被那个节点叫醒，到时候再说。收尾越短越好，立刻结束激活。

## S2 念发言并请人类准备投票

本次激活是被"本轮发言汇总"节点唤醒的，`[Upstream Outputs]` 里是本轮全部发言。

1. 把每个座位的原话整理成 JSON，交给事实层：

   ```bash
   uc speeches-set --session "$session_id" --json '{"1":"...","2":"...","4":"..."}'
   ```

   座位号必须齐，少一个会被拒。语义违规（谈身份、点评他人）脚本判不了，用 `--flag 座位=原因` 补上。
2. **只用返回里的 `text` 念稿，永远不要用原始文本。** `text` 是遮蔽过的版本；`violation` 非空的那条要点出来。
3. 说"发言转述 + 请准备投票"那段，结束激活。下一次唤醒来自人类。

## S3 开投

人类说了任意一句表示可以开始，就一条命令开投：

```bash
uc open-vote --session "$session_id"
```

它查槽位、渲染、提交，并把看门狗任务的文案一起备好。提交完只剩一件事：

- `watchdog.available: true` → 用 `bcs_assign_task` 把 `watchdog.message` 原样发给 `watchdog.target_bot`。
  这条回执是投票期间唯一不依赖人类的唤醒源。派任务只有这一个办法，**不要去查 `bcs --help`，不要用 `bcs chat`**。
- `watchdog.available: false` → 第一轮还没人出局，没有安全人选。**不要改派给存活玩家。**
  这一轮的兜底是人类，开场白里已经说过时限了。

**开投稿是运行入口节点的产物，不用我在这里说。** 派完看门狗立刻结束激活。

## S4 开票

被"开票"节点唤醒，`[Upstream Outputs]` 里是全部玩家的投票，每条只有票号。

1. ```bash
   uc votes-set --session "$session_id" --json '{"1":"...","2":"...","4":"..."}'
   ```

   解析、计票、平票规则、出局判定、胜负判定一次做完。**不要自己数票，不要自己判胜负。**
2. 看返回的 `verdict`：
   - `continue` → 说"开票结果"那段（逐条报票向、报票数、宣布出局、身份暂不公布、报剩下谁）。
     **玩家只交了票号，没有理由，不许替他们编。**
     `tie` 为真就是平票：**本轮没有人出局，直接进下一轮，没有重投。**
   - `finished` → 先 `uc reveal --session "$session_id"`，再说"终局"那段，然后调 `bcs_task_complete(summary)` 结束会话；如果因为有未回执的任务被阻塞，改用 `bcs session complete "$session_id"`。
3. **这个节点里只做上面两件事。** 不要在这里派任务、不要调 `bcs_assign_task`。下一轮的唤醒源在 S4b 安排。
4. 以上作为这个节点的产物输出，结束激活。

## S4b 派下一轮的唤醒源

开票稿会以我的身份发成群消息，再回灌成一次针对我的唤醒——**那次激活就是安排唤醒源的地方**。
判据不是"这次是不是回灌"，而是 `status` 里 **`phase = AWAIT_NEXT_ROUND` 且 `pending_ping` 非空**。
不管这次是谁把我叫醒的，只要满足这个条件，第一件事就是：

```bash
uc render-ping --session "$session_id"
```

用 `bcs_assign_task` 把 `message` 原样发给 `target_bot`。**这条回执就是把我叫醒开下一轮的东西**，不能省。
派完结束激活，**不要念稿**——开票结果上一条消息已经说过了，再说一遍就是重复播报。

## S5 念遗言并开下一轮

被遗言/预备回执唤醒。

1. 把回执原话作为遗言念出来，一句话带过。
2. 开下一轮：

   ```bash
   uc open-round --session "$session_id"
   ```

   返回 `RUN_SLOT_BUSY` 且 reason 是 `state_machine_run_active`，说明上一轮投票协作刚好还没收尾。
   **等几秒再跑一次，最多三次**；三次仍被占用就告诉人类当前情况并停下。
3. 和 S1 一样：提交完不说话，回合开场由入口节点产出。

## SX 卡住诊断

人类说"卡住了"，或者看门狗回执到了，都走这一节。先看现在在哪：

```bash
uc status --session "$session_id"
```

然后按 `phase` 直接尝试重开当前这一轮——`open-*` 自己会先查协作槽位，所以这一条命令同时是诊断和恢复：

```bash
# phase = SPEAK_RUNNING
uc open-round --session "$session_id" --retry
# phase = VOTE_RUNNING
uc open-vote  --session "$session_id" --retry
```

**`RUN_SLOT_BUSY` / `state_machine_run_active`** → 运行还活着，没卡住，而且这一条什么都没改。
说一句还在等谁，结束激活。看门狗把我叫醒但运行还活着，就是这种情况，属正常。

**返回 `submitted: true`** → 上一次的运行确实已经失败了（失败不会唤醒我），这一次重开成功。
跟人类说实话：上一次没跑起来、这一次重开、**之前投过的票作废，以这一次为准**。
`status --full` 的 `renders` 里能看到这是本轮第几次。

**`--retry` 不能省。** 不带它的话阶段卫兵会拒绝——`open-vote` 只在 `AWAIT_VOTE_START` 放行，而这时 phase 早就是 `VOTE_RUNNING` 了。带上 `--retry` 时轮次不推进、本轮记录不重建，只是把同一份 YAML 重新渲染并重新提交。

同一轮重开**最多两次**。第三次还是起不来就停下，如实说本局只能作废。

**重开也提交不上，或者状态长时间不变** → 目前没有取消运行的命令。如实说本局只能作废，请新建一个会话重开。**不要假装还能救。**
