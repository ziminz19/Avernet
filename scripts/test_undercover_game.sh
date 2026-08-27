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
grep -q "$civ" "${TMP}/init-init.json" && fail "init 的输出里不应该出现平民词"
grep -q "$und" "${TMP}/init-init.json" && fail "init 的输出里不应该出现卧底词"
grep -q "undercover\"" "${TMP}/init-init.json" || true
python3 -c "
import json,sys
d=json.load(open('${TMP}/init-init.json'))
assert d['players']==6, d
assert len(d['seating'])==6
assert sum(1 for s in d['seating'] if s['kind']=='human')==1
assert d['human_seat']==next(s['seat'] for s in d['seating'] if s['kind']=='human')
" || fail "init 的座位摘要不完整"
uc status --session "$SESSION" > "${TMP}/st.json"
grep -q "$civ" "${TMP}/st.json" && fail "status 的输出里不应该出现词"
ok "init/status 输出不含词，座位摘要完整"

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
    order="$(python3 -c "import json;print([s['seat'] for s in json.load(open('${TMP}/speak.json'))['speaking_order']])")"
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
python3 -c "
import json
d=json.load(open('${TMP}/speak2.json'))
assert d['round']==2, d
assert len(d['speaking_order'])==5, d
assert $civ2 not in [s['seat'] for s in d['speaking_order']], d
" || fail "第二轮不应该再包含出局玩家"
if command -v ruby >/dev/null 2>&1; then
    yaml2="$(python3 -c "import json;print(json.load(open('${TMP}/speak2.json'))['yaml_path'])")"
    RUBYOPT="-EUTF-8" ruby -ryaml "$CHECK" "$yaml2" speak || fail "第二轮发言协作 YAML 不满足契约"
    uc speeches-set --session "$SESSION" --json "$(python3 -c "
import json,sys
alive=[s['seat'] for s in json.load(open('${TMP}/speak2.json'))['speaking_order']]
print(json.dumps({str(s):'第二轮的一句描述' for s in alive},ensure_ascii=False))")" >/dev/null
    uc render-vote-run --session "$SESSION" > "${TMP}/vrun2.json"
    vyaml2="$(python3 -c "import json;print(json.load(open('${TMP}/vrun2.json'))['yaml_path'])")"
    RUBYOPT="-EUTF-8" ruby -ryaml "$CHECK" "$vyaml2" vote || fail "第二轮投票协作 YAML 不满足契约"
fi
ok "投票协作与第二轮（含出局者剔除）都满足契约"

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
echo "== 平票：首次无人出局，连续平票强制出局"
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
assert d['forced_by_repeat_tie'] is False
assert d['phase']=='AWAIT_NEXT_ROUND'
assert d['ping'] and d['ping']['kind']=='standby', d
" || fail "首次平票应该无人出局并派预备任务"
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
assert d['forced_by_repeat_tie'] is True, d
assert d['eliminated'] is not None and d['eliminated']['seat'] in (1,2), d
" || fail "连续平票应该在平票者中强制出局"
ok "平票与连续平票规则正确"

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

echo
if [ "$FAILS" -eq 0 ]; then
    echo "ALL PASS"
else
    echo "${FAILS} FAILURE(S)"
    exit 1
fi
