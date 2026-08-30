#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SKILL_DIR="${PROJECT_ROOT}/scripts/6bots_undercover_game_profile/referee/skills/undercover-game-referee"
UC="${SKILL_DIR}/scripts/undercover.py"
CHECK="${SCRIPT_DIR}/lib/undercover_run_check.rb"
FAILS=0

fail() { echo "FAIL: $*" >&2; FAILS=$((FAILS + 1)); }
ok() { echo "  ok: $*"; }

uc() { python3 "$UC" "$@"; }

# 从状态文件里读答案（只有测试可以这么干）
peek() { python3 -c "
import json,sys,os,re
sid=sys.argv[1]
p=os.path.join(os.environ['BOT_DATA_DIR'],'undercover-game',re.sub(r'[^A-Za-z0-9_.-]','_',sid)+'.json')
s=json.load(open(p))
print(json.dumps(s,ensure_ascii=False))
" "$1"; }

jqp() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1],{'d':d}))" "$1"; }

# 本轮的发言座位顺序直接从渲染出来的 YAML 里读。render 的返回不再带 speaking_order
# ——裁判的命令输出会被转发成群事件，所以那些只有测试用得上的字段都收掉了。
speaker_seats() { python3 -c "
import re,sys
print([int(m) for m in re.findall(r'^      speak_(\\d+):', open(sys.argv[1],encoding='utf-8').read(), re.M)])
" "$1"; }

new_game() {
    local tag="$1"; shift
    export BOT_DATA_DIR="${TMP}/${tag}"
    rm -rf "$BOT_DATA_DIR"; mkdir -p "$BOT_DATA_DIR"
    SESSION="grp-${tag}:0000abcd"
    uc init --session "$SESSION" --group "grp-${tag}" --human human_9 --referee-uuid ref-uuid "$@" > "${TMP}/${tag}-init.json"
}

FIVE_BOTS=(--bot "玩家稳健老陈=u1" --bot "玩家话痨小满=u2" --bot "玩家和事佬阿和=u3" --bot "玩家逻辑控林工=u4" --bot "玩家戏精阿浪=u5")
THREE_BOTS=(--bot "玩家稳健老陈=u1" --bot "玩家话痨小满=u2" --bot "玩家和事佬阿和=u3")

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --------------------------------------------------------------------------
echo "== init 与脱敏"
new_game init "${FIVE_BOTS[@]}" --seed 11
state="$(peek "$SESSION")"
civ="$(printf '%s' "$state" | jqp "d['words']['civilian']")"
und="$(printf '%s' "$state" | jqp "d['words']['undercover']")"
[ -n "$civ" ] && [ -n "$und" ] && [ "$civ" != "$und" ] || fail "init 应该抽出两个不同的词"
# init 的输出里只允许出现**人类玩家自己**那个词（human_word）：群聊是裁判和人类的
# 私密双人频道，Bot 一个字都看不到，所以发牌时把它念给人类是安全的。另一个词一个
# 字都不许出现，否则人类看两个词一比就知道自己是不是卧底了。
hw="$(printf '%s' "$state" | jqp "[s['word'] for s in d['seats'] if s['kind']=='human'][0]")"
other="$civ"; [ "$hw" = "$civ" ] && other="$und"
grep -q "$other" "${TMP}/init-init.json" && fail "init 的输出里不应该出现人类那个词之外的词"
python3 -c "
import json,sys
d=json.load(open('${TMP}/init-init.json'))
assert d['players']==6, d
assert len(d['seating'])==6
assert sum(1 for s in d['seating'] if s['kind']=='human')==1
assert d['human_seat']==next(s['seat'] for s in d['seating'] if s['kind']=='human')
assert d['human_word']=='''$hw''', d['human_word']
" || fail "init 的座位摘要不完整，或 human_word 不是人类自己的词"
uc status --session "$SESSION" > "${TMP}/st.json"
grep -q "$civ" "${TMP}/st.json" && fail "status 的输出里不应该出现词"
grep -q "$und" "${TMP}/st.json" && fail "status 的输出里不应该出现词"
uc my-word --session "$SESSION" > "${TMP}/mw.json"
grep -q "$hw" "${TMP}/mw.json" || fail "my-word 应该给出人类自己的词"
grep -q "$other" "${TMP}/mw.json" && fail "my-word 的输出里不应该出现另一个词"
ok "init/my-word 只给人类自己的词，status 输出不含词，座位摘要完整"

echo "== 同一 session 重复 init 应该被拒"
uc init --session "$SESSION" --group grp-init --human human_9 --referee-uuid ref-uuid "${FIVE_BOTS[@]}" >/dev/null 2>&1 \
    && fail "重复 init 应该失败" || ok "重复 init 被拒"

# --------------------------------------------------------------------------
echo "== 渲染出来的 YAML 必须满足 BCS 契约"
if command -v ruby >/dev/null 2>&1; then
    uc render-speak-run --session "$SESSION" > "${TMP}/speak.json"
    yaml="$(python3 -c "import json;print(json.load(open('${TMP}/speak.json'))['yaml_path'])")"
    RUBYOPT="-EUTF-8" ruby -ryaml "$CHECK" "$yaml" speak || fail "发言协作 YAML 不满足契约"
    grep -q "$civ" "${TMP}/speak.json" && fail "render-speak-run 的 stdout 不应该出现词"
    grep -q "$civ" "$yaml" || fail "发言 YAML 里应该注入词语"
    order="$(speaker_seats "$yaml")"
    [ "$order" = "[1, 2, 3, 4, 5, 6]" ] || fail "首轮发言顺序应该是全部六个座位，得到 ${order}"
else
    echo "  SKIP: 没有 ruby，跳过 YAML 契约校验"
fi

echo "== 投票协作 YAML 与后续轮次（有人出局后）也必须满足契约"
new_game contract "${FIVE_BOTS[@]}" --seed 21 >/dev/null
state="$(peek "$SESSION")"
civ2="$(printf '%s' "$state" | jqp "[s['seat'] for s in d['seats'] if s['role']!='undercover'][0]")"
uc render-speak-run --session "$SESSION" >/dev/null
uc speeches-set --session "$SESSION" --json '{"1":"甲","2":"乙","3":"丙","4":"丁","5":"戊","6":"己"}' >/dev/null
uc render-vote-run --session "$SESSION" > "${TMP}/vrun.json"
if command -v ruby >/dev/null 2>&1; then
    vyaml="$(python3 -c "import json;print(json.load(open('${TMP}/vrun.json'))['yaml_path'])")"
    RUBYOPT="-EUTF-8" ruby -ryaml "$CHECK" "$vyaml" vote || fail "投票协作 YAML 不满足契约"
fi
uc votes-set --session "$SESSION" --json "$(python3 -c "
import json,sys
t=int(sys.argv[1])
print(json.dumps({str(s):('我投%d号，理由随便'%(t if s!=t else (1 if t!=1 else 2))) for s in range(1,7)},ensure_ascii=False))" "$civ2")" >/dev/null
uc render-speak-run --session "$SESSION" > "${TMP}/speak2.json"
yaml2="$(python3 -c "import json;print(json.load(open('${TMP}/speak2.json'))['yaml_path'])")"
python3 -c "
import json,re
d=json.load(open('${TMP}/speak2.json'))
seats=[int(m) for m in re.findall(r'^      speak_(\d+):', open('${yaml2}',encoding='utf-8').read(), re.M)]
assert d['round']==2, d
assert len(seats)==5, seats
assert $civ2 not in seats, seats
" || fail "第二轮不应该再包含出局玩家"
if command -v ruby >/dev/null 2>&1; then
    RUBYOPT="-EUTF-8" ruby -ryaml "$CHECK" "$yaml2" speak || fail "第二轮发言协作 YAML 不满足契约"
    uc speeches-set --session "$SESSION" --json "$(python3 -c "
import json,re
alive=[int(m) for m in re.findall(r'^      speak_(\d+):', open('${yaml2}',encoding='utf-8').read(), re.M)]
print(json.dumps({str(s):'第二轮的一句描述' for s in alive},ensure_ascii=False))")" >/dev/null
    uc render-vote-run --session "$SESSION" > "${TMP}/vrun2.json"
    vyaml2="$(python3 -c "import json;print(json.load(open('${TMP}/vrun2.json'))['yaml_path'])")"
    RUBYOPT="-EUTF-8" ruby -ryaml "$CHECK" "$vyaml2" vote || fail "第二轮投票协作 YAML 不满足契约"
fi
ok "投票协作与第二轮（含出局者剔除）都满足契约"

# --------------------------------------------------------------------------
# 2026-08-30 第 2 轮的死锁：裁判在「本轮发言汇总」节点里跑了 open-vote。那个节点是
# 发言运行的出口，运行要等这次激活结束才算完成、协作槽位才释放——于是它等槽位、
# 槽位等它，重试和 sleep 只会把死锁钉死，一路卡到节点超时。当时有三处文案都在往这
# 条路上推（节点指令、speeches-set 的 next_action、status 的 next_action），所以三处
# 一起锁住，并和 tally 那条对称的纪律绑在同一个用例里。
echo "== 末节点里不许开投（S2 和 S3 不能挤进同一次激活）"
new_game collectguard "${FIVE_BOTS[@]}" --seed 31 >/dev/null
uc render-speak-run --session "$SESSION" > "${TMP}/cg-speak.json"
cg_speak="$(python3 -c "import json;print(json.load(open('${TMP}/cg-speak.json'))['yaml_path'])")"
uc speeches-set --session "$SESSION" --json '{"1":"甲","2":"乙","3":"丙","4":"丁","5":"戊","6":"己"}' > "${TMP}/cg-sp.json"
uc status --session "$SESSION" > "${TMP}/cg-status.json"
uc render-vote-run --session "$SESSION" > "${TMP}/cg-vote.json"
cg_vote="$(python3 -c "import json;print(json.load(open('${TMP}/cg-vote.json'))['yaml_path'])")"
python3 - "$cg_speak" "$cg_vote" "${TMP}/cg-sp.json" "${TMP}/cg-status.json" <<'PY' || fail "末节点开投的护栏没了"
import json, sys
speak = open(sys.argv[1], encoding="utf-8").read()
vote = open(sys.argv[2], encoding="utf-8").read()
sp = json.load(open(sys.argv[3], encoding="utf-8"))
st = json.load(open(sys.argv[4], encoding="utf-8"))

collect = speak.split("      collect:", 1)[1]
assert "不要开投" in collect, "collect 节点指令没写死「不要开投」"
assert "open-vote" in collect, "collect 节点指令没点名 open-vote"
assert "IN_COLLECT_NODE" in collect, "collect 节点指令没说清后果"
assert "你这就开投" not in speak, "旧文案「你这就开投」还在，模型会照做"

# 和 tally 那条纪律是同一条，要么都在，要么就是又改歪了一边
tally = vote.split("      tally:", 1)[1]
assert "不要派任何任务" in tally, "tally 节点的对称约束丢了"
assert "不要提交任何运行" in tally, "tally 节点没禁止提交下一个运行"

assert st["phase"] == "AWAIT_VOTE_START", st
assert "结束激活" in sp["next_action"], sp["next_action"]
assert "直接开投" not in sp["next_action"], "speeches-set 仍在祈使开投：%s" % sp["next_action"]
assert "本轮发言汇总" in st["next_action"], "status 的 next_action 缺汇总节点的例外分支：%s" % st["next_action"]
assert "结束激活" in st["next_action"], st["next_action"]
PY
ok "collect 节点与 speeches-set / status 的文案都不再把裁判推去开投"

# 文案是第一道闸，这是第二道：万一模型还是在末节点里跑了 open-vote，脚本必须把它和
# 「上一个运行刚收尾」的普通竞态区分开——普通竞态可以再等，这条自锁再等一万年也不会
# 放行，报错必须换个码、并把「不要重试」说死。桩住 bcs-cli 让槽位永远不放行即可复现。
echo "== 末节点里真去开投：必须报 IN_COLLECT_NODE，不是 RUN_SLOT_BUSY"
new_game slotguard "${FIVE_BOTS[@]}" --seed 32 >/dev/null
uc render-speak-run --session "$SESSION" >/dev/null
uc speeches-set --session "$SESSION" --json '{"1":"甲","2":"乙","3":"丙","4":"丁","5":"戊","6":"己"}' >/dev/null
mkdir -p "${TMP}/stub"
cat > "${TMP}/stub/bcs-cli" <<'STUB'
#!/usr/bin/env bash
# 永远不放行，模拟发言运行还挂在那里的那一刻
printf '%s\n' '{"allowed": false, "reason": "the current session already has an active state-machine run"}'
STUB
chmod +x "${TMP}/stub/bcs-cli"
PATH="${TMP}/stub:$PATH" uc open-vote --session "$SESSION" > "${TMP}/sg.json" 2>&1
python3 - "${TMP}/sg.json" <<'PY' || fail "末节点开投没有被单独识别成 IN_COLLECT_NODE"
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["ok"] is False, d
assert d["error"] == "IN_COLLECT_NODE", "错误码应该和普通的槽位竞态区分开：%s" % d
assert "不要重试" in d["message"], d["message"]
assert "结束激活" in d["message"], d["message"]
PY
ok "末节点里开投被单独识别，并明说不要重试"

# 另一半环：在开票节点里开下一轮是同一条死锁，报错也要单独认出来
uc render-vote-run --session "$SESSION" >/dev/null
uc votes-set --session "$SESSION" --json "$(python3 -c "
import json
print(json.dumps({str(s): '我投1号' if s != 1 else '我投2号' for s in range(1, 7)}, ensure_ascii=False))")" >/dev/null
PATH="${TMP}/stub:$PATH" uc open-round --session "$SESSION" > "${TMP}/sg2.json" 2>&1
python3 - "${TMP}/sg2.json" <<'PY' || fail "开票节点里开下一轮没有被单独识别成 IN_TALLY_NODE"
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["ok"] is False, d
assert d["error"] == "IN_TALLY_NODE", "错误码应该和普通的槽位竞态区分开：%s" % d
assert "不要重试" in d["message"], d["message"]
PY
ok "开票节点里开下一轮也被单独识别"

# --------------------------------------------------------------------------
echo "== 发言泄词遮蔽"
new_game leak "${FIVE_BOTS[@]}" --seed 22 >/dev/null
state="$(peek "$SESSION")"
uc render-speak-run --session "$SESSION" >/dev/null
seat1_word="$(printf '%s' "$state" | jqp "[s['word'] for s in d['seats'] if s['seat']==1][0]")"
uc speeches-set --session "$SESSION" --json "$(python3 -c "
import json,sys
t={str(s):'普通的一句描述' for s in range(1,7)}
t['1']='这就是'+sys.argv[1]+'没跑了'
print(json.dumps(t,ensure_ascii=False))" "$seat1_word")" > "${TMP}/sp.json"
python3 -c "
import json
d=json.load(open('${TMP}/sp.json'))
s1=[s for s in d['speeches'] if s['seat']==1][0]
assert s1['violation']=='说出了自己的词', s1
assert '$seat1_word' not in s1['text'], s1
assert '○' in s1['text'], s1
"
if [ $? -eq 0 ]; then
    grep -q "$seat1_word" "${TMP}/sp.json" && fail "speeches-set 的输出里泄露了词"
    ok "完整词泄露被判违规并遮蔽"
else
    fail "完整词泄露没有被判违规或没有被遮蔽"
fi

echo "== 座位不齐应该被拒"
new_game partial "${FIVE_BOTS[@]}" --seed 12 >/dev/null
uc render-speak-run --session "$SESSION" >/dev/null
uc speeches-set --session "$SESSION" --json '{"1":"a","2":"b"}' >/dev/null 2>&1 \
    && fail "发言不齐应该被拒" || ok "发言不齐被拒"

echo "== 连续两字与拆散说全"
new_game bigram "${FIVE_BOTS[@]}" --seed 13 >/dev/null
state="$(peek "$SESSION")"
uc render-speak-run --session "$SESSION" >/dev/null
w1="$(printf '%s' "$state" | jqp "[s['word'] for s in d['seats'] if s['seat']==1][0]")"
w2="$(printf '%s' "$state" | jqp "[s['word'] for s in d['seats'] if s['seat']==2][0]")"
payload="$(python3 -c "
import json,sys
w1,w2=sys.argv[1],sys.argv[2]
t={str(i):'一句普通的描述' for i in range(1,7)}
t['1']=('说到'+w1[:2]+'就懂了') if len(w1)>2 else ('说到'+w1+'就懂了')
t['2']='我说'+'，再说'.join(w2) if len(w2)>1 else w2
print(json.dumps(t,ensure_ascii=False))" "$w1" "$w2")"
uc speeches-set --session "$SESSION" --json "$payload" > "${TMP}/sp2.json"
python3 -c "
import json
d=json.load(open('${TMP}/sp2.json'))
by={s['seat']:s for s in d['speeches']}
assert by[1]['violation'] is not None, by[1]
assert by[2]['violation'] is not None, by[2]
assert '○' in by[1]['text'] and '○' in by[2]['text'], d
assert by[1]['violation']=='说出了自己词里连续的两个字' or len('$w1')<=2, by[1]
assert by[2]['violation']=='把自己的词拆开说了', by[2]
"
if [ $? -eq 0 ]; then
    grep -q "$w1" "${TMP}/sp2.json" && fail "speeches-set 输出泄露了词"
    ok "连续两字与拆散说全都被判违规并遮蔽"
else
    fail "连续两字或拆散说全没有被判违规"
fi

# --------------------------------------------------------------------------
echo "== 投票解析"
new_game parse "${FIVE_BOTS[@]}" --seed 14 >/dev/null
uc render-speak-run --session "$SESSION" >/dev/null
uc speeches-set --session "$SESSION" --json '{"1":"甲","2":"乙","3":"丙","4":"丁","5":"戊","6":"己"}' >/dev/null
uc render-vote-run --session "$SESSION" >/dev/null
check_parse() {
    local text="$1" voter="$2" want="$3"
    got="$(uc parse-vote --session "$SESSION" --voter "$voter" --text "$text" | python3 -c "import json,sys;print(json.load(sys.stdin)['target_seat'])")"
    [ "$got" = "$want" ] || fail "解析『${text}』应得 ${want}，实得 ${got}"
}
check_parse "我投4号，那句太空了" 1 4
check_parse "我投四号" 1 4
check_parse "我弃权，看不出来" 1 None
check_parse "我投1号" 1 None
check_parse "谁知道呢" 1 None
name3="$(peek "$SESSION" | jqp "[s['display'] for s in d['seats'] if s['seat']==3][0]")"
check_parse "我投${name3}，他前后不一致" 1 3
ok "编号、中文数字、名字、弃权、投自己、读不出 六种情况都正确"

# --------------------------------------------------------------------------
echo "== 计票：唯一最高者出局，卧底出局则平民胜"
new_game civwin "${FIVE_BOTS[@]}" --seed 15 >/dev/null
state="$(peek "$SESSION")"
spy="$(printf '%s' "$state" | jqp "[s['seat'] for s in d['seats'] if s['role']=='undercover'][0]")"
uc render-speak-run --session "$SESSION" >/dev/null
uc speeches-set --session "$SESSION" --json '{"1":"甲","2":"乙","3":"丙","4":"丁","5":"戊","6":"己"}' >/dev/null
uc render-vote-run --session "$SESSION" >/dev/null
votes="$(python3 -c "
import json,sys
spy=int(sys.argv[1])
other=[s for s in range(1,7) if s!=spy][0]
v={}
for s in range(1,7):
    v[str(s)]=('我投%d号，那句话对不上'%other) if s==spy else ('我投%d号，那句话对不上'%spy)
print(json.dumps(v,ensure_ascii=False))" "$spy")"
uc votes-set --session "$SESSION" --json "$votes" > "${TMP}/tally.json"
python3 -c "
import json
d=json.load(open('${TMP}/tally.json'))
assert d['eliminated']['seat']==$spy, d
assert d['tie'] is False
assert d['verdict']=='finished' and d['winner']=='civilian', d
assert d['phase']=='FINISHED'
assert d['ping'] is None
" || fail "卧底被投出局后应该判平民胜并结束"
uc reveal --session "$SESSION" > "${TMP}/rv.json"
python3 -c "
import json
d=json.load(open('${TMP}/rv.json'))
assert d['winner']=='civilian'
assert len(d['seats'])==6
assert all(s['word'] for s in d['seats'])
" || fail "reveal 应该给出完整真相"
ok "唯一最高者出局、平民胜、reveal 正常"

echo "== reveal 只能在终局"
new_game noreveal "${FIVE_BOTS[@]}" --seed 16 >/dev/null
uc reveal --session "$SESSION" >/dev/null 2>&1 && fail "未结束时 reveal 应该失败" || ok "未结束时 reveal 被拒"

echo "== 阶段守卫"
uc votes-set --session "$SESSION" --json '{}' >/dev/null 2>&1 && fail "错阶段调 votes-set 应该失败" || ok "错阶段调用被拒"
uc render-vote-run --session "$SESSION" >/dev/null 2>&1 && fail "错阶段调 render-vote-run 应该失败" || ok "错阶段渲染被拒"

# --------------------------------------------------------------------------
echo "== 平票：无人出局、直接进下一轮，连着平也不随机挑人"
new_game tie "${FIVE_BOTS[@]}" --seed 17 >/dev/null
state="$(peek "$SESSION")"
spy="$(printf '%s' "$state" | jqp "[s['seat'] for s in d['seats'] if s['role']=='undercover'][0]")"
tie_votes() { python3 -c "
import json,sys
alive=json.loads(sys.argv[1])
a,b=alive[0],alive[1]
v={}
for i,s in enumerate(alive):
    v[str(s)]='我投%d号，理由随便'%(b if i%2==0 else a)
print(json.dumps(v,ensure_ascii=False))" "$1"; }
speeches_for() { python3 -c "
import json,sys
alive=json.loads(sys.argv[1])
print(json.dumps({str(s):'一句普通的描述' for s in alive},ensure_ascii=False))" "$1"; }

alive='[1, 2, 3, 4, 5, 6]'
uc render-speak-run --session "$SESSION" >/dev/null
uc speeches-set --session "$SESSION" --json "$(speeches_for "$alive")" >/dev/null
uc render-vote-run --session "$SESSION" >/dev/null
uc votes-set --session "$SESSION" --json "$(tie_votes "$alive")" > "${TMP}/tie1.json"
python3 -c "
import json
d=json.load(open('${TMP}/tie1.json'))
assert d['tie'] is True, d
assert d['eliminated'] is None, d
assert 'forced_by_repeat_tie' not in d, d
assert d['phase']=='AWAIT_NEXT_ROUND'
assert d['ping'] and d['ping']['kind']=='standby', d
" || fail "平票应该无人出局并派预备任务"
uc render-ping --session "$SESSION" > "${TMP}/ping1.json"
python3 -c "
import json
d=json.load(open('${TMP}/ping1.json'))
assert d['kind']=='standby' and d['target_bot'].startswith('玩家'), d
assert '准备' in d['message']
" || fail "预备任务渲染不正确"

uc render-speak-run --session "$SESSION" >/dev/null
uc speeches-set --session "$SESSION" --json "$(speeches_for "$alive")" >/dev/null
uc render-vote-run --session "$SESSION" >/dev/null
uc votes-set --session "$SESSION" --json "$(tie_votes "$alive")" > "${TMP}/tie2.json"
python3 -c "
import json
d=json.load(open('${TMP}/tie2.json'))
assert d['tie'] is True, d
# 平票永远不出局：连着平第二次也不在平票者里随机挑人。平票不减员但照样烧掉一轮，
# 轮数用完判卧底胜，所以平票的代价由轮数承担，不由随机承担。
assert d['eliminated'] is None, d
assert d['phase']=='AWAIT_NEXT_ROUND', d
" || fail "连续平票也不应该有人出局"
ok "平票规则正确：无人出局、不重投、不随机挑人"

# --------------------------------------------------------------------------
echo "== 平票照样烧掉一轮"
python3 -c "
import json
a=json.load(open('${TMP}/tie1.json')); b=json.load(open('${TMP}/tie2.json'))
assert a['round']==1 and b['round']==2, (a['round'], b['round'])
assert len(a['alive'])==6 and len(b['alive'])==6, (a['alive'], b['alive'])
" || fail "平票不减员，但轮次必须照常推进"
ok "平票不减员，轮次照常推进"

# --------------------------------------------------------------------------
echo "== status 默认精简、--full 才给细节"
new_game brief "${FIVE_BOTS[@]}" --seed 23 >/dev/null
uc status --session "$SESSION" > "${TMP}/brief.json"
uc status --session "$SESSION" --full > "${TMP}/full.json"
[ "$(wc -l < "${TMP}/brief.json")" -eq 1 ] || fail "status 默认应该是一行"
python3 -c "
import json
b=json.load(open('${TMP}/brief.json')); f=json.load(open('${TMP}/full.json'))
# 精简版必须保留 S4b 的判据：phase + pending_ping
assert set(b)=={'ok','phase','round','alive','pending_ping','next_action'}, sorted(b)
for k in ('eliminated','human_seat','renders','result'):
    assert k not in b and k in f, k
" || fail "status 的精简/详细两档字段不对"
ok "status 默认精简，--full 才给细节"

# --------------------------------------------------------------------------
echo "== 裁判自己的 bot_uuid 由脚本解析"
export BOT_DATA_DIR="${TMP}/uuid"; rm -rf "$BOT_DATA_DIR"; mkdir -p "$BOT_DATA_DIR"
uc init --session "u:1" --group u --human human_9 "${THREE_BOTS[@]}" > "${TMP}/nouuid.json" 2>&1 &&     fail "既没有 BCN_BOT_UUID 也没有 session.json 时应该报错"
python3 -c "
import json
d=json.load(open('${TMP}/nouuid.json'))
assert d['ok'] is False and d['error']=='NO_REFEREE_UUID', d
" || fail "解析不出 bot_uuid 时应该给 NO_REFEREE_UUID"
mkdir -p "${BOT_DATA_DIR}/.bcs"
printf '{"bot_uuid":"谁是卧底主持人","token":"x"}' > "${BOT_DATA_DIR}/.bcs/session.json"
uc init --session "u:1" --group u --human human_9 "${THREE_BOTS[@]}" > "${TMP}/okuuid.json"
python3 -c "
import json
d=json.load(open('${TMP}/okuuid.json'))
assert d['referee_uuid']=='谁是卧底主持人', d
" || fail "应该从 .bcs/session.json 读出 bot_uuid"
ok "不给 --referee-uuid 时脚本自己解析，解析不出就明确报错"

# --------------------------------------------------------------------------
echo "== 只剩两人时卧底胜"
new_game spywin "${THREE_BOTS[@]}" --seed 18 >/dev/null
state="$(peek "$SESSION")"
spy="$(printf '%s' "$state" | jqp "[s['seat'] for s in d['seats'] if s['role']=='undercover'][0]")"
civs="$(printf '%s' "$state" | jqp "[s['seat'] for s in d['seats'] if s['role']!='undercover']")"
target1="$(python3 -c "import sys;print(eval(sys.argv[1])[0])" "$civs")"
alive4="$(python3 -c "print([1,2,3,4])")"
uc render-speak-run --session "$SESSION" >/dev/null
uc speeches-set --session "$SESSION" --json "$(speeches_for "$alive4")" >/dev/null
uc render-vote-run --session "$SESSION" >/dev/null
uc votes-set --session "$SESSION" --json "$(python3 -c "
import json,sys
t=int(sys.argv[1])
print(json.dumps({str(s):('我投%d号，理由随便'%(t if s!=t else (1 if t!=1 else 2))) for s in [1,2,3,4]},ensure_ascii=False))" "$target1")" > "${TMP}/sw1.json"
python3 -c "
import json
d=json.load(open('${TMP}/sw1.json'))
assert d['eliminated']['seat']==$target1, d
assert d['verdict']=='continue', d
assert d['ping'] is not None, d
" || fail "四人局投出一个平民后应该继续"
kind="$(python3 -c "import json;print(json.load(open('${TMP}/sw1.json'))['ping']['kind'])")"
uc render-ping --session "$SESSION" > "${TMP}/ping2.json"
python3 -c "
import json
d=json.load(open('${TMP}/ping2.json'))
assert d['kind']=='$kind', d
assert d['message'], d
" || fail "遗言/预备任务渲染失败"
[ "$kind" = "eulogy" ] && ok "出局的是 Bot，派的是遗言任务" || ok "出局的是人类，回退成预备任务"

alive3="$(python3 -c "
import sys;print([s for s in [1,2,3,4] if s!=int(sys.argv[1])])" "$target1")"
target2="$(python3 -c "
import sys;print([s for s in eval(sys.argv[1]) if s!=int(sys.argv[2])][0])" "$civs" "$target1")"
uc render-speak-run --session "$SESSION" >/dev/null
uc speeches-set --session "$SESSION" --json "$(speeches_for "$alive3")" >/dev/null
uc render-vote-run --session "$SESSION" >/dev/null
uc votes-set --session "$SESSION" --json "$(python3 -c "
import json,sys
alive=eval(sys.argv[1]); t=int(sys.argv[2])
print(json.dumps({str(s):('我投%d号，理由随便'%(t if s!=t else [x for x in alive if x!=t][0])) for s in alive},ensure_ascii=False))" "$alive3" "$target2")" > "${TMP}/sw2.json"
python3 -c "
import json
d=json.load(open('${TMP}/sw2.json'))
assert d['verdict']=='finished' and d['winner']=='undercover', d
assert '只剩' in d['win_reason'], d
assert d['phase']=='FINISHED'
" || fail "只剩两人时应该判卧底胜"
ok "只剩两人判卧底胜"

# --------------------------------------------------------------------------
echo "== 轮数耗尽卧底胜"
new_game rounds "${FIVE_BOTS[@]}" --seed 19 --max-rounds 1 >/dev/null
state="$(peek "$SESSION")"
spy="$(printf '%s' "$state" | jqp "[s['seat'] for s in d['seats'] if s['role']=='undercover'][0]")"
civ1="$(printf '%s' "$state" | jqp "[s['seat'] for s in d['seats'] if s['role']!='undercover'][0]")"
uc render-speak-run --session "$SESSION" >/dev/null
uc speeches-set --session "$SESSION" --json "$(speeches_for "$alive")" >/dev/null
uc render-vote-run --session "$SESSION" >/dev/null
uc votes-set --session "$SESSION" --json "$(python3 -c "
import json,sys
t=int(sys.argv[1])
print(json.dumps({str(s):('我投%d号，理由随便'%(t if s!=t else (1 if t!=1 else 2))) for s in range(1,7)},ensure_ascii=False))" "$civ1")" > "${TMP}/ro.json"
python3 -c "
import json
d=json.load(open('${TMP}/ro.json'))
assert d['verdict']=='finished' and d['winner']=='undercover', d
assert '轮数' in d['win_reason'], d
" || fail "轮数耗尽应该判卧底胜"
ok "轮数耗尽判卧底胜"

# --------------------------------------------------------------------------
echo "== 投票内容违规则该票作废"
new_game badvote "${FIVE_BOTS[@]}" --seed 20 >/dev/null
state="$(peek "$SESSION")"
w1="$(printf '%s' "$state" | jqp "[s['word'] for s in d['seats'] if s['seat']==1][0]")"
uc render-speak-run --session "$SESSION" >/dev/null
uc speeches-set --session "$SESSION" --json "$(speeches_for "$alive")" >/dev/null
uc render-vote-run --session "$SESSION" >/dev/null
uc votes-set --session "$SESSION" --json "$(python3 -c "
import json,sys
w=sys.argv[1]
v={str(s):'我投3号，理由随便' for s in range(1,7)}
v['1']='我投4号，我的词是'+w
print(json.dumps(v,ensure_ascii=False))" "$w1")" > "${TMP}/bv.json"
python3 -c "
import json
d=json.load(open('${TMP}/bv.json'))
v1=[v for v in d['votes'] if v['seat']==1][0]
assert v1['violation'] is not None, v1
assert v1['target_seat'] is None, v1
assert '$w1' not in v1['text'], v1
" || fail "投票里泄词应该作废并遮蔽"
grep -q "$w1" "${TMP}/bv.json" && fail "votes-set 输出泄露了词"
ok "投票泄词被作废并遮蔽"

# --------------------------------------------------------------------------
echo "== 参数自愈：--session/--group 可以省，没开局的 status 也不报错"
export BOT_DATA_DIR="${TMP}/selfheal"
rm -rf "$BOT_DATA_DIR"; mkdir -p "$BOT_DATA_DIR"
SESSION="grp-selfheal:0000dead"
uc status --session "$SESSION" > "${TMP}/nogame.json" || fail "还没开局时 status 不该失败"
python3 -c "
import json
d=json.load(open('${TMP}/nogame.json'))
assert d['ok'] is True and d['phase']=='NO_GAME', d
assert 'begin' in d['command'], d
" || fail "还没开局时 status 应该给 NO_GAME 和下一条命令"
uc init --session "$SESSION" --human human_9 --referee-uuid ref-uuid "${FIVE_BOTS[@]}" >/dev/null     || fail "init 漏了 --group 应该能从会话 ID 推出来"
[ "$(peek "$SESSION" | jqp "d['group_id']")" = "grp-selfheal" ] || fail "--group 应该推成会话 ID 的冒号前半段"
uc status >/dev/null || fail "只有一局时 status 应该能零参数认出本局"
uc speeches-set --session "$SESSION" > "${TMP}/badargs.json" 2>/dev/null && fail "缺 --json 应该失败"
python3 -c "
import json
d=json.load(open('${TMP}/badargs.json'))
assert d['ok'] is False and d['error']=='BAD_ARGS', d
" || fail "参数错误应该是脚本自己的 JSON，不是 argparse 的 usage 转储"
ok "session/group 能自解析，没开局的 status 不报错，参数错误也是 JSON"

# --------------------------------------------------------------------------
echo "== 遗言：任务里给素材、要求点名，回执经 mask 兜底"
new_game eulogy "${FIVE_BOTS[@]}" --seed 11
state="$(peek "$SESSION")"
victim="$(printf '%s' "$state" | jqp "[s['seat'] for s in d['seats'] if s['role']!='undercover' and s['kind']=='bot'][0]")"
vword="$(printf '%s' "$state" | jqp "[s['word'] for s in d['seats'] if s['seat']==$victim][0]")"
uc render-speak-run --session "$SESSION" >/dev/null
uc speeches-set --session "$SESSION" --json '{"1":"甲","2":"乙","3":"丙","4":"丁","5":"戊","6":"己"}' >/dev/null
uc render-vote-run --session "$SESSION" >/dev/null
uc votes-set --session "$SESSION" --json "$(python3 -c "
import json
v = {str(s): '我投${victim}号' for s in range(1, 7)}
v['${victim}'] = '我投%d号' % (1 if ${victim} != 1 else 2)
print(json.dumps(v, ensure_ascii=False))")" > "${TMP}/eu-votes.json"
uc render-ping --session "$SESSION" > "${TMP}/eu-ping.json" || fail "出局之后应该能渲染遗言任务"
python3 -c "
import json
d=json.load(open('${TMP}/eu-ping.json'))
m=d['message']
assert d['kind']=='eulogy' and d['seat']==${victim}, d
assert '「甲」' in m and '「己」' in m, '遗言任务里要带上全场发言当素材'
assert '最怀疑的座位号' in m, '遗言任务要求点名'
assert '不许说自己是不是卧底' in m
assert '$vword' not in json.dumps(d, ensure_ascii=False), '遗言任务正文不许出现出局者自己的词'
assert 'mask --seat ${victim}' in d['note'], '要提醒裁判先过一道遮蔽'
" || fail "遗言任务的形状不对"
uc mask --session "$SESSION" --seat "$victim" --text "我的${vword}明明不是那样" > "${TMP}/eu-mask.json"
python3 -c "
import json
d=json.load(open('${TMP}/eu-mask.json'))
assert d['violation'] is not None, d
assert '$vword' not in d['text'], d
" || fail "遗言泄词应该被 mask 判违规并遮蔽"
ok "遗言任务带素材要点名，泄词有 mask 兜底"

echo
if [ "$FAILS" -eq 0 ]; then
    echo "ALL PASS"
else
    echo "${FAILS} FAILURE(S)"
    exit 1
fi
