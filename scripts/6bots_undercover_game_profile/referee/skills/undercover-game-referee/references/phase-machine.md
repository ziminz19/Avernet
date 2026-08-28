# 阶段机

`uc status` 给出 `phase`，唤醒类型给出这一次是谁把我叫醒的。两者一查就知道该做什么。

| phase | 唤醒类型 | 执行 |
| --- | --- | --- |
| 还没有状态文件 | `SESSION_START` | [S0 开局](#s0-开局) |
| 还没有状态文件 | `HUMAN_MSG` | 先做 S0；如果人类这句话就是要开始，紧接着做 S1 |
| `AWAIT_START` | `HUMAN_MSG` | [S1 发牌并开第一轮](#s1-发牌并开第一轮) |
| `SPEAK_RUNNING` | `NODE_TASK`（本轮发言汇总） | [S2 念发言并请人类准备投票](#s2-念发言并请人类准备投票) |
| `SPEAK_RUNNING` | `HUMAN_MSG` | 人类在催或在闲聊：回一句现在等谁；说"卡住了"就走 [SX](#sx-卡住诊断) |
| `AWAIT_VOTE_START` | `HUMAN_MSG` | [S3 开投](#s3-开投) |
| `VOTE_RUNNING` | `NODE_TASK`（开票） | [S4 开票](#s4-开票) |
| `VOTE_RUNNING` | `WORKER_MSG`（看门狗回执） | 走 [SX](#sx-卡住诊断) 查一遍；没卡住就回一句还在等谁 |
| `VOTE_RUNNING` | `HUMAN_MSG` | 回一句还在等谁投；说"卡住了"就走 [SX](#sx-卡住诊断) |
| `AWAIT_NEXT_ROUND` | `WORKER_MSG` | [S5 念遗言并开下一轮](#s5-念遗言并开下一轮) |
| `AWAIT_NEXT_ROUND` | `HUMAN_MSG` | 人类说"继续"就跳过遗言，直接做 S5 的第 2 步 |
| `FINISHED` | 任意 | 说明本局已结束；想再来一局在这个协作群里新建一个会话 |

任何格子里没写的组合：说清当前进行到哪、人类可以做什么，**不推进**。

`SPEAK_RUNNING` / `VOTE_RUNNING` 下的 `WORKER_MSG` 一律是迟到的回执，回一句无关紧要的话即可，**绝不推进阶段、绝不调用任何脚本命令**。唯一的例外是「看门狗回执」——那条是我自己派出去当闹钟的，见 SX。

**被节点唤醒时，先用 `uc status` 核对这个节点属不属于当前阶段。** 运行失败之后，失败前派出去的节点仍可能迟到几十秒才把产物送回来。对不上就只回一句"这条是迟到的回执，已忽略"，不调脚本、不念稿——照着它往下走会让人类以为一切正常。

---

## S0 开局

1. 从 session 输入 / 群组协作目标里解析配置：卧底数、词语难度、轮数上限。缺项用默认值，不要为配置追问玩家。
2. 确认人类在场。含"用户输入"节点的运行要求 session 里有处于 **Present** 的人类参与者，否则提交直接被拒。

   ```bash
   bcs session get "$session_id" \
     | jq -r '.participants[] | select(.actor_kind == "human") | [.bot_uuid, (.mode // "absent")] | @tsv'
   ```

   **人类参与者的 `mode` 缺省值是 `absent`，不是 `present`。** 字段缺失按 absent 处理。
   - 不在场 → 说"加入提示"那段，结束激活。人类点完"加入当前会话"会发消息过来，届时重走 S0。
   - 在场 → 记下他的 `human_<...>` 备用。
3. 取 Bot 名称与 UUID：

   ```bash
   bcs session get "$session_id" \
     | jq -r '.participants[] | select((.actor_kind // "bot") == "bot") | [(.bot_name // .bot_uuid), .bot_uuid] | @tsv'
   ```

   自己的 UUID 用运行环境提供的 `BCN_BOT_UUID`。不要凭名称猜 UUID。
4. 说开场白，请人类回一句"开始"。**这一步不 init**——人类可能还想先问问规则。

## S1 发牌并开第一轮

```bash
uc init --session "$session_id" --group "$group_id" --human "$human_actor_id" \
  --referee-uuid "$my_uuid" \
  --bot "玩家稳健老陈=$u1" --bot "玩家话痨小满=$u2" \
  --difficulty medium --undercover 1 --max-rounds 6
```

抽词、排座位、分身份都在这一条命令里完成，输出只有座位表，没有词也没有身份。

然后开第一轮：

```bash
bcs collaborate permission --session "$session_id"   # 只认服务端返回的 allowed
uc render-speak-run --session "$session_id"          # 返回 yaml_path / input_path / binding_args
bcs collaborate run "$yaml_path" --session "$session_id" $binding_args --input @"$input_path"
```

**不要打开 yaml_path，不要把它的内容贴进任何地方。** 里面有全部玩家的词。

说"回合开场"，结束激活。

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

人类说了任意一句表示可以开始，就提交投票协作：

```bash
bcs collaborate permission --session "$session_id"
uc render-vote-run --session "$session_id"
bcs collaborate run "$yaml_path" --session "$session_id" $binding_args --input @"$input_path"
```

**投票运行里没有我的入口节点**，第一个被叫的是一位玩家，我下一次被唤醒就是"开票"。

提交完顺手挂一个兜底闹钟——投票运行失败不会唤醒我，这条回执是唯一不依赖人类的
唤醒源：

```bash
uc render-vote-watchdog --session "$session_id"
```

- `available: true` → 用 `bcs_assign_task` 把 `message` 原样发给 `target_bot`。
- `available: false` → 第一轮还没人出局，没有安全人选。**不要改派给存活玩家**，
  他们的通道要留给自己的投票节点。这一轮的兜底是人类，主持稿里必须把时限说清。

说一句"投票开始，你的票箱在右边副屏"，结束激活。

## S4 开票

被"开票"节点唤醒，`[Upstream Outputs]` 里是全部玩家的投票原话。

1. ```bash
   uc votes-set --session "$session_id" --json '{"1":"...","2":"...","4":"..."}'
   ```

   解析、计票、平票规则、出局判定、胜负判定一次做完。**不要自己数票，不要自己判胜负。**
2. 看返回的 `verdict`：
   - `continue` → 说"开票结果"那段（逐条报票向、报票数、宣布出局、身份暂不公布、报剩下谁）。
     **玩家只交了票号，没有理由，不许替他们编。** 然后派遗言/预备任务：

     ```bash
     uc render-ping --session "$session_id"
     ```

     用 `bcs_assign_task` 把 `message` 原样发给 `target_bot`。**这条回执就是把我叫醒开下一轮的东西**，不能省。
   - `finished` → 先 `uc reveal --session "$session_id"`，再说"终局"那段，然后调 `bcs_task_complete(summary)` 结束会话；如果因为有未回执的任务被阻塞，改用 `bcs session complete "$session_id"`。
3. 以上都作为这个节点的产物输出，结束激活。

## S5 念遗言并开下一轮

被遗言/预备回执唤醒。

1. 把回执原话作为遗言念出来，一句话带过。
2. 开下一轮：

   ```bash
   bcs collaborate permission --session "$session_id"
   ```

   如果返回 `state_machine_run_active`，说明上一轮投票协作刚好还没收尾。**等几秒再查一次，最多三次**；三次仍被占用就告诉人类当前情况并停下。
   `allowed: true` 之后：

   ```bash
   uc render-speak-run --session "$session_id"
   bcs collaborate run "$yaml_path" --session "$session_id" $binding_args --input @"$input_path"
   ```
3. 说"回合开场"，结束激活。

## SX 卡住诊断

人类说"卡住了"，或者看门狗回执到了，都走这一节。

```bash
bcs collaborate permission --session "$session_id"
uc status --session "$session_id"
```

**`state_machine_run_active`** → 还在跑，没卡住。说一句还在等谁，结束激活。看门狗
把我叫醒但运行还活着，就是这种情况，属正常。

**`allowed: true`** → 没有活跃运行。运行已经失败或结束，而失败不会唤醒我。按
`status` 的 `phase` 重开当前这一轮：

```bash
# phase = SPEAK_RUNNING
uc render-speak-run --session "$session_id" --retry
# phase = VOTE_RUNNING
uc render-vote-run --session "$session_id" --retry

bcs collaborate run "$yaml_path" --session "$session_id" $binding_args --input @"$input_path"
```

**`--retry` 不能省。** 不带它的话阶段卫兵会拒绝——`render-vote-run` 只在
`AWAIT_VOTE_START` 放行，而这时 phase 早就是 `VOTE_RUNNING` 了。带上 `--retry` 时
轮次不推进、本轮记录不重建，只是把同一份 YAML 重新渲染并重新提交。

重开前后都要跟人类说实话：上一次没跑起来、这一次重开、**之前投过的票作废，以这一
次为准**。`status` 的 `renders` 里能看到这是本轮第几次。

同一轮重开**最多两次**。第三次还是起不来就停下，如实说本局只能作废。

**`allowed: true` 但重开也提交不上，或者状态长时间不变** → 目前没有取消运行的命
令。如实说本局只能作废，请新建一个会话重开。**不要假装还能救。**
