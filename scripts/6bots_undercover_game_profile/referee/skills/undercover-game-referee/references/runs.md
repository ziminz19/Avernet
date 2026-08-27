# 两个临时自定义协作的形状

YAML 由 `render-speak-run` / `render-vote-run` 生成，**不要手写**。这份文档解释它们为什么长这样，方便出问题时判断是不是渲染出了偏差。

## 发言：传递闭包，不是链

存活玩家按座位顺序 `s1..sk`，加一个我的汇总节点：

```text
s1 ─┬──────────────────────────┐
    └→ s2 ─┬───────────────────┤
           └→ s3 ─┬────────────┤
                  └→ ... ─┬────┤
                          └→ sk ┤
                                ↓
                            collect（我，final_output）
```

第 i 个发言节点的 targets = **它后面所有发言节点 + collect**。

为什么不是简单的链：节点提示里只包含**直接父节点**的产物。`s1→s2→s3` 会让 s3 看不到 s1 说了什么。把前面所有人都连成父节点，后手才能拿到本轮完整发言，collect 才能一次拿到全部 k 份。

人类玩家的位置是一个 `human_input` 节点，没有 assignee、没有 binding，超时给足 15 分钟。

## 投票：扇出再汇合

```text
vote_open（我）─┬→ vote_s1 ─┐
                ├→ vote_s2 ─┤
                ├→ vote_h  ─┤      ← 人类的票也在这一层，和 Bot 同时进行
                └→ vote_sk ─┘
                             ↓
                          tally（我，final_output）
```

- `vote_open` 存在的唯一理由：运行必须有**唯一零入度入口**。不能让某个玩家当入口，否则它的票会作为上游产物流给其他人。所以入口是我，而且它只输出一句无信息量的话。
- 所有投票节点并行，彼此看不到对方的票。每个人的词写在自己节点的 instruction 里，公开的发言史走 `--input`。
- `tally` 是汇合点，BCS 会等齐全部上游才派给我。全场只有我能同时看到所有票。

## 两个运行为什么不合并

合并成一个运行会让"发言转述"变成中间节点的产物——而中间节点的产物只进副屏，不进群聊。人类就看不到发言转述了，整局在聊天里只剩一条最终结果。

分成两个运行，发言转述才能作为第一个运行的 `final_output` 自动发成群消息。代价是中间需要人类说一声才能开投，这一拍同时也是他读转述、想清楚的时间。

## 硬约束（服务端会拒绝的写法）

- 顶层只允许 `name`、`metadata`、`participants`、`runtime`；不能有 `api_version` / `id` / `version`。
- `runtime.kind: state_machine`、`state_machine.version: 1`、`graph_mode: acyclic`。
- 只能用 `bot_task` 和 `human_input`；不能用 `initial_node`、`variables`、`events`、`guard`、`action`、`output_contract`、`input_schema`。
- 唯一零入度入口、唯一 `final_output` 出口，所有节点从入口可达且能到达出口。
- `human_input` 不设 `assignee` / `max_attempts` / `final_output`，但**必须**显式给 `node_timeout_ms`。
- 一个运行里只能有一个 `human_input` 节点。
- 提交时人类必须处于 Present。
- 真实 Bot UUID 只出现在 `--binding`，不写进 YAML。

`scripts/lib/undercover_run_check.rb`（仓库里的回归测试）会对渲染结果逐条校验以上全部。
