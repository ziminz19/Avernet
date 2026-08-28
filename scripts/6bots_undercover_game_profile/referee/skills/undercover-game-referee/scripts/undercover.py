#!/usr/bin/env python3
"""谁是卧底 · 裁判事实层。

裁判 Bot 的所有游戏状态读写、随机、渲染和判定都通过本脚本完成。
模型只负责把渲染好的内容交给工具，以及用主持人的口吻说话。

两条不变量：
  1. 除 render-* 和 reveal 外，任何子命令的 stdout 都不包含词语和身份。
     这样即使把命令输出原样贴进群聊也不会泄密。
  2. render-speak-run / render-vote-run 把含词的 YAML 写进文件、只打印路径，
     词一次都不经过模型的输出通道。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

STATE_VERSION = 3

PHASES = (
    "AWAIT_START",
    "SPEAK_RUNNING",
    "AWAIT_VOTE_START",
    "VOTE_RUNNING",
    "AWAIT_NEXT_ROUND",
    "FINISHED",
)

NEXT_ACTION = {
    "AWAIT_START": "等人类回一句就开局：先 render-speak-run，再提交发言协作。",
    "SPEAK_RUNNING": "等发言协作把汇总节点派给你；拿到全部发言后调 speeches-set。",
    "AWAIT_VOTE_START": "等人类说一声开投；然后 render-vote-run 并提交投票协作。",
    "VOTE_RUNNING": "等投票协作把计票节点派给你；计票节点上调 votes-set。运行超时未回就走卡住诊断。",
    "AWAIT_NEXT_ROUND": "等出局者的遗言回执；收到后 render-speak-run 开下一轮。",
    "FINISHED": "本局已结束，只能 reveal。想再来一局请新建会话。",
}

SPEECH_MAX_CHARS = 25
# 投票只交票号：「我投4号」5 字、「我弃权」3 字。理由是泄露渠道——一条理由本质上
# 是「拿我的词去比对他的话」的结果，念出来就等于广播自己那个词的一个属性。
VOTE_MAX_CHARS = 10
MASK = "○"

# 节点超时与重试。
#
# Bot 的会话通道是串行的：同一时刻只跑一个激活，其余排队。通道偶尔会在一次激活
# 结束后没有及时释放，节点任务就会一直排在队里不动，直到 Bot 侧的自愈把通道抢
# 回来——那个自愈窗口是分钟级的。所以节点超时必须明显大于它，否则 BCS 先判超时、
# 自愈后到，节点产物变成没人认领的孤儿。
#
# max_attempts 固定为 1：重试对「通道堵住」这类故障毫无作用，只会让同一个节点排
# 两份，排空时还可能逆序执行，在群里留下重复播报和空回复。真正的瞬时失败由 Bot
# 侧自己重试。
BOT_NODE_TIMEOUT_MS = 420_000
# 裁判的通道最挤：人类的自由聊天、节点任务、以及它自己 final_output 的回灌都走
# 这一条，所以派给裁判的节点额外放宽。入口节点撞车时被牵连的那个投票节点同理。
CONTENDED_NODE_TIMEOUT_MS = 600_000
HUMAN_NODE_TIMEOUT_MS = 900_000
NODE_MAX_ATTEMPTS = 1

DEFAULT_CONFIG = {
    "players": 6,
    "undercover": 1,
    "difficulty": "medium",
    "max_rounds": 6,
    "reveal_role": False,
}


# --------------------------------------------------------------------------
# 路径与原子读写
# --------------------------------------------------------------------------

def game_dir() -> Path:
    base = os.environ.get("BOT_DATA_DIR") or os.environ.get("OPENCLAW_DATA_DIR")
    if not base:
        base = str(Path.home() / ".openclaw")
    d = Path(base) / "undercover-game"
    d.mkdir(parents=True, exist_ok=True)
    return d


def skill_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def state_path(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)
    return game_dir() / f"{safe}.json"


def work_dir(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)
    d = game_dir() / "work" / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


@contextmanager
def locked(session_id: str):
    """整个读-改-写序列持锁，避免并发激活互相覆盖。"""
    lock_file = state_path(session_id).with_suffix(".lock")
    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield
    finally:
        os.close(fd)


def load_state(session_id: str) -> dict[str, Any]:
    p = state_path(session_id)
    if not p.exists():
        die("NO_STATE", f"没有找到这一局的状态文件：{p}。如果是新的一局，先跑 init。")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        die("BAD_STATE", f"状态文件无法解析：{exc}。不要凭记忆继续，向人类说明并停下。")
    raise AssertionError("unreachable")


def save_state(state: dict[str, Any]) -> None:
    p = state_path(state["session_id"])
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".state-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def die(code: str, message: str) -> None:
    json.dump({"ok": False, "error": code, "message": message}, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    sys.exit(2)


def emit(payload: dict[str, Any]) -> None:
    payload = {"ok": True, **payload}
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def require_phase(state: dict[str, Any], *allowed: str) -> None:
    if state["phase"] not in allowed:
        die(
            "WRONG_PHASE",
            f"当前阶段是 {state['phase']}，这个命令只能在 {'/'.join(allowed)} 阶段用。"
            f" 下一步应该是：{NEXT_ACTION.get(state['phase'], '先跑 status')}",
        )


# --------------------------------------------------------------------------
# 座位辅助
# --------------------------------------------------------------------------

def seat_of(state: dict[str, Any], seat: int) -> dict[str, Any]:
    for s in state["seats"]:
        if s["seat"] == seat:
            return s
    die("NO_SEAT", f"没有 {seat} 号座位。")
    raise AssertionError("unreachable")


def alive_seats(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in state["seats"] if s["alive"]]


def current_round(state: dict[str, Any]) -> dict[str, Any]:
    return state["rounds"][-1]


def public_history(state: dict[str, Any]) -> list[dict[str, Any]]:
    """给玩家看的公开发言史，只含可展示文本，不含词与身份。"""
    out = []
    for rnd in state["rounds"]:
        if not rnd["speeches"]:
            continue
        out.append(
            {
                "round": rnd["round"],
                "speeches": [
                    {
                        "seat": int(seat),
                        "player": seat_of(state, int(seat))["display"],
                        "text": rec["display"],
                    }
                    for seat, rec in sorted(rnd["speeches"].items(), key=lambda kv: int(kv[0]))
                ],
            }
        )
    return out


# --------------------------------------------------------------------------
# 违规检查与遮蔽
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# 发言钝度
# --------------------------------------------------------------------------

# 泄词有两条路：字面上说出词（forbid_line 拦得住），和语义上把词描述到只对应一件
# 东西（脚本拦不住）。第二条才是实战里致命的那条——第一轮就下定义，卧底当场暴露，
# 一轮结束。
#
# 这里给的是一把玩家能自己算的尺子：把这句话念给局外人听，他能不能列出至少 N 样
# 符合的东西。N 按轮次收敛，信息就按轮次释放。
#
# 为什么必须收敛：投票只看历史发言，发言要是一直很钝，投票就退化成随机；6 人 1 个
# 卧底、每轮出局 1 个，第 4 轮结束只剩 2 人卧底就赢了，所以平民只有 4 轮时间，第 3
# 轮起必须放到能真正推理的程度。
BLUNTNESS_LADDER = {1: 5, 2: 3}
BLUNTNESS_FLOOR = 2


def bluntness_n(rnd: int) -> int:
    return BLUNTNESS_LADDER.get(rnd, BLUNTNESS_FLOOR)


def bluntness_block(rnd: int, first: bool) -> str:
    """本轮的钝度要求，逐字写进每个发言节点的 instruction。"""
    n = bluntness_n(rnd)
    lines = [
        f"【本轮钝度 = {n}】",
        f"写完先自检：把这句话原样念给一个没参加游戏的人，他能列出至少 {n} 样符合的东西吗？"
        f"列不出来就是太具体了，重写得更钝。",
    ]
    if rnd == 1:
        lines.append(
            "第一轮只从这四类里挑一类说：什么时候会想到它 / 多久遇到一次 / "
            "用完是什么感觉 / 一般放在哪儿。"
        )
        lines.append(
            "不要说它是干什么用的、什么形状、什么材质、用在身体哪个部位。"
            "一句话里「动作」和「对象」同时出现就是在下定义，本轮不许下定义。"
        )
    elif rnd == 2:
        lines.append(
            "这一轮可以加一个属性——形状、材质、使用场合，三选一，只挑一个。"
            "仍然不要把用途和对象放在同一句里。"
        )
    else:
        lines.append("这一轮可以说用途了，但整句仍然不能等价于这个词的定义。")
    if first:
        lines.append("你是本轮第一个发言的人，没有参照，本轮的钝度由你定——宁可更钝。")
    else:
        lines.append(
            "先看前面的人到了什么钝度，你不能比他们更锐利。"
            "换个角度可以，但要换成同样钝的角度，不要去补一个只有你的词才成立的角度。"
        )
    return "\n".join(lines)


def bigrams(word: str) -> list[str]:
    """词里全部连续两字片段；两字词返回它自己，一字词返回空。"""
    return [word[i : i + 2] for i in range(len(word) - 1)]


def check_text(state: dict[str, Any], seat: int, text: str, max_chars: int) -> tuple[str, str | None]:
    """返回 (可展示文本, 违规原因或 None)。

    泄词判定有三种，都精确、可复现，也和告诉玩家的规则一字不差：
      - 完整词语出现在文本里；
      - 词里任意连续两个字出现在文本里（说了大半个词）；
      - 词的每一个字都在文本里出现过（拆散了说全）。
    单个常用字命中不算违规，否则「其他人」这种话会被误判。
    对家的词只要完整出现也遮蔽，防止串词直接摊牌给人类。
    """
    own = seat_of(state, seat)["word"]
    other = state["words"]["undercover"] if own == state["words"]["civilian"] else state["words"]["civilian"]
    display = text
    reason = None

    if own and own in display:
        display = display.replace(own, MASK * len(own))
        reason = "说出了自己的词"
    elif own and any(g in display for g in bigrams(own)):
        for g in bigrams(own):
            display = display.replace(g, MASK * len(g))
        reason = "说出了自己词里连续的两个字"
    elif own and all(ch in display for ch in own):
        for ch in set(own):
            display = display.replace(ch, MASK)
        reason = "把自己的词拆开说了"

    if other and other in display:
        display = display.replace(other, MASK * len(other))
        reason = reason or "说出了对家的词"

    if reason is None and len(text) > max_chars:
        reason = f"超过 {max_chars} 字"

    return display, reason


# --------------------------------------------------------------------------
# 投票解析
# --------------------------------------------------------------------------

CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def parse_vote(state: dict[str, Any], voter_seat: int | None, text: str) -> tuple[int | None, str | None]:
    """从一句人话里解析出被投座位。返回 (座位号或 None, 说明)。"""
    if not text or not text.strip():
        return None, "没有内容"

    if re.search(r"弃权", text):
        return None, "弃权"

    living = {s["seat"] for s in alive_seats(state)}

    def acceptable(n: int) -> bool:
        return n in living and n != voter_seat

    # 1) 「我投N号」这类锚点，取「投」之后最近的数字
    for m in re.finditer(r"投[^0-9零一二两三四五六七八九]{0,4}([0-9]+|[零一二两三四五六七八九])", text):
        tok = m.group(1)
        n = int(tok) if tok.isdigit() else CN_DIGITS[tok]
        if acceptable(n):
            return n, None

    # 2) 名字匹配
    for s in state["seats"]:
        if s["display"] and s["display"] in text and acceptable(s["seat"]):
            return s["seat"], None

    # 3) 全文里恰好只出现一个合法座位号
    nums = set()
    for tok in re.findall(r"[0-9]+|[零一二两三四五六七八九]", text):
        n = int(tok) if tok.isdigit() else CN_DIGITS[tok]
        if acceptable(n):
            nums.add(n)
    if len(nums) == 1:
        return nums.pop(), None

    if voter_seat is not None and re.search(rf"投[^0-9]{{0,4}}{voter_seat}\b", text):
        return None, "投了自己"
    return None, "读不出投给谁"


# --------------------------------------------------------------------------
# YAML 渲染
# --------------------------------------------------------------------------

def yq(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def block(text: str, indent: int) -> str:
    pad = " " * indent
    lines = text.rstrip("\n").split("\n")
    return "\n".join(pad + line if line else "" for line in lines)


def forbid_line(word: str) -> str:
    chars = "、".join(f"「{ch}」" for ch in word)
    parts = [f"不得说出「{word}」这个词本身"]
    grams = bigrams(word)
    if len(grams) > 1:
        parts.append("不得说出其中连续的两个字（" + "、".join(f"「{g}」" for g in grams) + "）")
    if len(word) > 1:
        parts.append(f"也不得把 {chars} 拆散了说全")
    parts.append("不得用拼音、英文、谐音或逐字暗示")
    return "；".join(parts) + "。"


def write_run_files(session_id: str, kind: str, yaml_text: str, run_input: dict[str, Any]) -> tuple[str, str]:
    d = work_dir(session_id)
    yaml_path = d / f"{kind}.yaml"
    input_path = d / f"{kind}-input.json"
    yaml_path.write_text(yaml_text, encoding="utf-8")
    input_path.write_text(json.dumps(run_input, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(yaml_path), str(input_path)


def render_speak_yaml(state: dict[str, Any]) -> tuple[str, list[str]]:
    rnd = state["round"]
    living = sorted(alive_seats(state), key=lambda s: s["seat"])
    node_ids = [f"speak_{s['seat']}" for s in living]

    participants: list[str] = []
    for s in living:
        if s["kind"] == "bot":
            participants.append(
                f"  {s['binding']}:\n"
                f"    display_name: {yq(str(s['seat']) + '号 ' + s['display'])}\n"
                f"    required: true"
            )
    participants.append('  referee:\n    display_name: "主持人"\n    required: true')

    nodes: list[str] = []
    for idx, s in enumerate(living):
        nid = node_ids[idx]
        targets = node_ids[idx + 1 :] + ["collect"]
        targets_yaml = "[" + ", ".join(targets) + "]"
        first = idx == 0
        if s["kind"] == "human":
            instruction = (
                f"【你的词语】{s['word']}\n\n"
                f"第 {rnd} 轮 · 你是 {s['seat']} 号 · 轮到你发言了。\n"
                f"上方「上游产物」里是本轮在你之前玩家的原话，历史轮次在本次运行的输入里。\n\n"
                f"请写一句话（不超过 {SPEECH_MAX_CHARS} 个字）描述你的词语。\n"
                f"{forbid_line(s['word'])}\n\n"
                f"{bluntness_block(rnd, first)}\n\n"
                "不要提身份、不要点评别人，只描述你的词。"
            )
            nodes.append(
                f"      {nid}:\n"
                f"        kind: human_input\n"
                f"        display_name: {yq(str(s['seat']) + '号发言')}\n"
                f"        node_timeout_ms: {HUMAN_NODE_TIMEOUT_MS}\n"
                f"        instruction: |\n{block(instruction, 10)}\n"
                f"        transitions:\n"
                f"          complete:\n"
                f"            targets: {targets_yaml}"
            )
        else:
            instruction = (
                f"你是本局的 {s['seat']} 号玩家。你的词语是【{s['word']}】。\n"
                f"现在是第 {rnd} 轮发言。[Upstream Outputs] 里是本轮在你之前已经发言的玩家原话，"
                "历史轮次的发言在 [Input] 里。\n"
                f"请只输出一句话，不超过 {SPEECH_MAX_CHARS} 个字，描述你的词语。\n"
                f"{forbid_line(s['word'])}\n\n"
                f"{bluntness_block(rnd, first)}\n\n"
                "不要提身份、轮次、规则或票数，也不要点评其他玩家。\n"
                "只输出这一句话本身，不要编号、不要引号、不要 JSON、不要任何解释。"
            )
            nodes.append(
                f"      {nid}:\n"
                f"        kind: bot_task\n"
                f"        display_name: {yq(str(s['seat']) + '号发言')}\n"
                f"        assignee:\n"
                f"          type: bot_binding\n"
                f"          binding: {s['binding']}\n"
                f"        instruction: |\n{block(instruction, 10)}\n"
                f"        transitions:\n"
                f"          complete:\n"
                f"            targets: {targets_yaml}"
            )

    collect_instruction = (
        "【裁判节点 · 本轮发言汇总】\n"
        "[Upstream Outputs] 里是本轮全部玩家的发言。\n"
        "1. 先把每个座位的原话整理成 JSON，调 undercover.py speeches-set 交给事实层。\n"
        "2. 用它返回的可展示文本写主持稿：串场用你自己的话，发言逐字引用，不要改写、不要概括。\n"
        "3. 结尾请人类玩家在群里说一声，准备好就开始投票。\n"
        "只输出给玩家看的主持稿，不要输出任何词语、身份、内部状态、节点名或运行 ID。"
    )
    nodes.append(
        "      collect:\n"
        "        kind: bot_task\n"
        '        display_name: "本轮发言汇总"\n'
        "        assignee:\n"
        "          type: bot_binding\n"
        "          binding: referee\n"
        f"        node_timeout_ms: {CONTENDED_NODE_TIMEOUT_MS}\n"
        "        final_output: true\n"
        f"        instruction: |\n{block(collect_instruction, 10)}"
    )

    yaml_text = (
        f"name: {yq(f'谁是卧底 第{rnd}轮 发言')}\n"
        "metadata:\n"
        f"  description: {yq('存活玩家按座位顺序各说一句话描述自己的词语')}\n"
        "participants:\n" + "\n".join(participants) + "\n"
        "runtime:\n"
        "  kind: state_machine\n"
        "  state_machine:\n"
        "    version: 1\n"
        "    graph_mode: acyclic\n"
        "    defaults:\n"
        f"      node_timeout_ms: {BOT_NODE_TIMEOUT_MS}\n"
        f"      max_attempts: {NODE_MAX_ATTEMPTS}\n"
        "    nodes:\n" + "\n".join(nodes) + "\n"
    )
    bindings = [f"{s['binding']}={s['bot_uuid']}" for s in living if s["kind"] == "bot"]
    bindings.append(f"referee={state['referee_uuid']}")
    return yaml_text, bindings


def vote_gate_seat(living: list[dict[str, Any]]) -> dict[str, Any]:
    """挑一个 Bot 来当投票运行的零入度入口节点。

    绝对不能是裁判。入口节点在运行提交的那一刻就被派出去，而裁判正卡在提交它的
    那次激活里——节点会排在自己后面，等自己让出通道。一旦通道没及时释放，这个
    节点就永远等不到，整个运行随它一起超时失败。

    用第一位存活 Bot。已出局玩家的通道虽然更空，但那条通道要留给看门狗任务
    （见 cmd_render_vote_watchdog），两个都往那儿塞就是换个地方再犯一次同样的错。
    代价是入口节点结束的瞬间，它自己的投票节点会派回同一条通道——那个窗口只有
    零点几秒，用 CONTENDED_NODE_TIMEOUT_MS 兜住。
    """
    for s in living:
        if s["kind"] == "bot":
            return s
    die("NO_BOT_GATE", "场上没有可用的 Bot 来当投票运行的入口节点，这一轮投票开不了。")
    raise AssertionError("unreachable")


def render_vote_yaml(state: dict[str, Any]) -> tuple[str, list[str]]:
    rnd = state["round"]
    living = sorted(alive_seats(state), key=lambda s: s["seat"])
    node_ids = [f"vote_{s['seat']}" for s in living]
    seat_list = "、".join(f"{s['seat']}号 {s['display']}" for s in living)
    gate = vote_gate_seat(living)

    participants = []
    for s in living:
        if s["kind"] == "bot":
            participants.append(
                f"  {s['binding']}:\n"
                f"    display_name: {yq(str(s['seat']) + '号 ' + s['display'])}\n"
                f"    required: true"
            )
    participants.append('  referee:\n    display_name: "主持人"\n    required: true')

    # 入口节点的产物会作为 [Upstream Outputs] 流给每一个投票节点，所以它必须是
    # 一个固定的、零信息量的 token——不能让它捎带任何关于牌面的东西。
    open_instruction = (
        "【开场节点】\n"
        f"第 {rnd} 轮投票现在开始。这个节点只是运行的入口，你在这里不需要做任何判断。\n"
        "只输出两个字：开始\n"
        "不要输出别的任何内容，不要提到任何玩家、词语、发言或投票倾向。"
    )
    nodes = [
        "      vote_open:\n"
        "        kind: bot_task\n"
        '        display_name: "投票开场"\n'
        "        assignee:\n"
        "          type: bot_binding\n"
        f"          binding: {gate['binding']}\n"
        f"        instruction: |\n{block(open_instruction, 10)}\n"
        "        transitions:\n"
        "          complete:\n"
        "            targets: [" + ", ".join(node_ids) + "]"
    ]

    for idx, s in enumerate(living):
        nid = node_ids[idx]
        others = "、".join(f"{o['seat']}号" for o in living if o["seat"] != s["seat"])
        if s["kind"] == "human":
            instruction = (
                f"【你的词语】{s['word']}\n\n"
                f"第 {rnd} 轮投票 · 你是 {s['seat']} 号。\n"
                f"所有人历史全部发言在本次运行的输入里，主持人也已经在群里念过一遍。"
                "这些发言是唯一的判断依据。\n\n"
                f"可以投的人：{others}。不能投自己。\n"
                "只写「我投N号」，N 是阿拉伯数字，不要写理由。\n"
                "这一轮所有人都只交票号——理由会暴露自己那个词，所以谁都不写。\n"
                "不想投就写「我弃权」。"
            )
            nodes.append(
                f"      {nid}:\n"
                f"        kind: human_input\n"
                f"        display_name: {yq(str(s['seat']) + '号投票')}\n"
                f"        node_timeout_ms: {HUMAN_NODE_TIMEOUT_MS}\n"
                f"        instruction: |\n{block(instruction, 10)}\n"
                f"        transitions:\n"
                f"          complete:\n"
                f"            targets: [tally]"
            )
        else:
            instruction = (
                f"你是本局的 {s['seat']} 号玩家。你的词语是【{s['word']}】。\n"
                f"现在是第 {rnd} 轮投票。全场玩家是：{seat_list}。\n"
                f"[Input] 里是所有人**全部历史轮次**的发言原话，逐字可查——"
                "这是你唯一的判断依据。\n"
                f"可以投的人：{others}。不能投自己。\n\n"
                "在心里想清楚投谁，但**只输出票号本身**：「我投N号」，N 是阿拉伯数字。\n"
                "不写理由、不引用别人的话、不解释、不加任何前缀。整条不超过 "
                f"{VOTE_MAX_CHARS} 个字。\n"
                "理由是泄露渠道：说「跟我理解的不一样」就等于把自己那个词的一个属性"
                "广播给全场。所以这一轮谁都不写理由。\n"
                "确实无法判断时，只输出「我弃权」。\n"
                f"任何情况下都不得在输出里出现「{s['word']}」或它的任何一部分。"
            )
            # 入口节点那位刚在同一条通道上跑完 vote_open，投票节点会在它结束的
            # 瞬间派回来，是全场唯一一个可能撞上通道未释放的玩家节点。
            timeout_line = (
                f"        node_timeout_ms: {CONTENDED_NODE_TIMEOUT_MS}\n"
                if s["seat"] == gate["seat"]
                else ""
            )
            nodes.append(
                f"      {nid}:\n"
                f"        kind: bot_task\n"
                f"        display_name: {yq(str(s['seat']) + '号投票')}\n"
                f"        assignee:\n"
                f"          type: bot_binding\n"
                f"          binding: {s['binding']}\n"
                f"{timeout_line}"
                f"        instruction: |\n{block(instruction, 10)}\n"
                f"        transitions:\n"
                f"          complete:\n"
                f"            targets: [tally]"
            )

    tally_instruction = (
        "【裁判节点 · 计票】\n"
        "[Upstream Outputs] 里是本轮全部玩家的投票，每条只有票号，没有理由。\n"
        "1. 把每个座位的原话整理成 JSON，调 undercover.py votes-set 交给事实层计票。\n"
        "2. 用它返回的结果写开票主持稿：逐条报谁投了谁、报票数、宣布出局者、"
        "说明身份暂不公布、报剩下几个人。判定一律以事实层返回为准，不要自己数票。\n"
        "   **玩家没有给理由，你也不许替他们编、不许猜他们为什么这么投。**"
        "这一段的戏在票型上——谁压谁、谁是孤票、谁被围了。\n"
        "3. 如果事实层说本局结束，就在这里公布完整真相（先调 reveal）。\n"
        "只输出给玩家看的主持稿，不要输出任何未出局玩家的词语或身份、内部状态、节点名或运行 ID。"
    )
    nodes.append(
        "      tally:\n"
        "        kind: bot_task\n"
        '        display_name: "开票"\n'
        "        assignee:\n"
        "          type: bot_binding\n"
        "          binding: referee\n"
        f"        node_timeout_ms: {CONTENDED_NODE_TIMEOUT_MS}\n"
        "        final_output: true\n"
        f"        instruction: |\n{block(tally_instruction, 10)}"
    )

    yaml_text = (
        f"name: {yq(f'谁是卧底 第{rnd}轮 投票')}\n"
        "metadata:\n"
        f"  description: {yq('存活玩家根据全部发言并行投票，主持人计票')}\n"
        "participants:\n" + "\n".join(participants) + "\n"
        "runtime:\n"
        "  kind: state_machine\n"
        "  state_machine:\n"
        "    version: 1\n"
        "    graph_mode: acyclic\n"
        "    defaults:\n"
        f"      node_timeout_ms: {BOT_NODE_TIMEOUT_MS}\n"
        f"      max_attempts: {NODE_MAX_ATTEMPTS}\n"
        "    nodes:\n" + "\n".join(nodes) + "\n"
    )
    bindings = [f"{s['binding']}={s['bot_uuid']}" for s in living if s["kind"] == "bot"]
    bindings.append(f"referee={state['referee_uuid']}")
    return yaml_text, bindings


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------

def load_word_bank(difficulty: str) -> list[tuple[str, str]]:
    path = skill_dir() / "references" / "word-bank" / f"{difficulty}.tsv"
    if not path.exists():
        die("NO_WORD_BANK", f"词库不存在：{path}")
    pairs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) == 2 and parts[0] and parts[1]:
            pairs.append((parts[0], parts[1]))
    if not pairs:
        die("EMPTY_WORD_BANK", f"词库是空的：{path}")
    return pairs


def cmd_init(args: argparse.Namespace) -> None:
    with locked(args.session):
        if state_path(args.session).exists() and not args.force:
            die("ALREADY_STARTED", "这一局已经开过了。想重开请加 --force。")

        bots = []
        for item in args.bot:
            if "=" not in item:
                die("BAD_BOT", f"--bot 要写成 名称=UUID，收到的是：{item}")
            name, uuid = item.split("=", 1)
            bots.append((name.strip(), uuid.strip()))
        if not bots:
            die("NO_BOTS", "至少要有一个 Bot 玩家。")

        cfg = dict(DEFAULT_CONFIG)
        cfg["difficulty"] = args.difficulty
        cfg["undercover"] = args.undercover
        cfg["max_rounds"] = args.max_rounds
        cfg["players"] = len(bots) + 1
        if cfg["undercover"] >= cfg["players"]:
            die("BAD_CONFIG", "卧底人数必须小于总人数。")

        rng = random.Random(args.seed) if args.seed is not None else random.SystemRandom()

        civ, und = rng.choice(load_word_bank(args.difficulty))
        if rng.randint(0, 1):
            civ, und = und, civ

        order = [name for name, _ in bots] + ["__HUMAN__"]
        rng.shuffle(order)
        uuid_of = dict(bots)

        seats = []
        for idx, who in enumerate(order, start=1):
            if who == "__HUMAN__":
                seats.append(
                    {
                        "seat": idx,
                        "kind": "human",
                        "display": "你",
                        "bot_name": None,
                        "bot_uuid": None,
                        "binding": None,
                        "word": "",
                        "role": "civilian",
                        "alive": True,
                        "eliminated_round": None,
                    }
                )
            else:
                seats.append(
                    {
                        "seat": idx,
                        "kind": "bot",
                        "display": who[2:] if who.startswith("玩家") else who,
                        "bot_name": who,
                        "bot_uuid": uuid_of[who],
                        "binding": f"seat{idx}",
                        "word": "",
                        "role": "civilian",
                        "alive": True,
                        "eliminated_round": None,
                    }
                )

        spy_seats = rng.sample([s["seat"] for s in seats], cfg["undercover"])
        for s in seats:
            spy = s["seat"] in spy_seats
            s["role"] = "undercover" if spy else "civilian"
            s["word"] = und if spy else civ

        state = {
            "version": STATE_VERSION,
            "session_id": args.session,
            "group_id": args.group,
            "human_actor_id": args.human,
            "referee_uuid": args.referee_uuid,
            "phase": "AWAIT_START",
            "config": cfg,
            "words": {"civilian": civ, "undercover": und},
            "seats": seats,
            "round": 0,
            "rounds": [],
            "consecutive_ties": 0,
            "pending_ping": None,
            "result": None,
        }
        save_state(state)

    emit(
        {
            "phase": "AWAIT_START",
            "players": cfg["players"],
            "undercover_count": cfg["undercover"],
            "difficulty": cfg["difficulty"],
            "max_rounds": cfg["max_rounds"],
            "seating": [
                {"seat": s["seat"], "player": s["display"], "kind": s["kind"]} for s in seats
            ],
            "human_seat": next(s["seat"] for s in seats if s["kind"] == "human"),
            "next_action": NEXT_ACTION["AWAIT_START"],
        }
    )


def cmd_status(args: argparse.Namespace) -> None:
    state = load_state(args.session)
    emit(
        {
            "phase": state["phase"],
            "round": state["round"],
            "max_rounds": state["config"]["max_rounds"],
            "alive": [
                {"seat": s["seat"], "player": s["display"], "kind": s["kind"]}
                for s in alive_seats(state)
            ],
            "eliminated": [
                {"seat": s["seat"], "player": s["display"], "round": s["eliminated_round"]}
                for s in state["seats"]
                if not s["alive"]
            ],
            "human_seat": next(s["seat"] for s in state["seats"] if s["kind"] == "human"),
            "pending_ping": state["pending_ping"],
            "consecutive_ties": state["consecutive_ties"],
            "renders": (state["rounds"][-1].get("renders", {}) if state["rounds"] else {}),
            "result": state["result"],
            "next_action": NEXT_ACTION.get(state["phase"], "先跑 status"),
        }
    )


def bump_render(state: dict[str, Any], kind: str) -> int:
    """记下这一轮的某个运行渲染到第几次，并返回次数。第一次是 1。"""
    renders = current_round(state).setdefault("renders", {})
    renders[kind] = renders.get(kind, 0) + 1
    return renders[kind]


def run_file_kind(kind: str, rnd: int, attempt: int) -> str:
    return f"{kind}-r{rnd}" if attempt <= 1 else f"{kind}-r{rnd}-retry{attempt - 1}"


def cmd_render_speak_run(args: argparse.Namespace) -> None:
    with locked(args.session):
        state = load_state(args.session)
        if args.retry:
            # 重开当前这一轮：上一次提交的运行失败了，而运行失败不会唤醒裁判。
            # 轮次不推进、本轮记录不重建，只把同一份 YAML 重新渲染一次。
            require_phase(state, "SPEAK_RUNNING")
            if current_round(state)["speeches"]:
                die(
                    "ALREADY_SPOKEN",
                    "本轮发言已经收齐落盘了，不能重开发言运行。"
                    "如果卡住的是投票，用 render-vote-run --retry。",
                )
        else:
            require_phase(state, "AWAIT_START", "AWAIT_NEXT_ROUND")
            state["round"] += 1
            state["pending_ping"] = None
            state["rounds"].append(
                {
                    "round": state["round"],
                    "order": [
                        s["seat"] for s in sorted(alive_seats(state), key=lambda s: s["seat"])
                    ],
                    "speeches": {},
                    "votes": {},
                    "counts": {},
                    "eliminated": None,
                    "tie": False,
                    "renders": {},
                }
            )
        living = sorted(alive_seats(state), key=lambda s: s["seat"])
        state["phase"] = "SPEAK_RUNNING"
        attempt = bump_render(state, "speak")
        yaml_text, bindings = render_speak_yaml(state)
        run_input = {
            "game": "谁是卧底",
            "round": state["round"],
            "alive": [f"{s['seat']}号 {s['display']}" for s in living],
            "history": public_history(state),
            "rule": (
                f"用一句话（不超过{SPEECH_MAX_CHARS}字）描述你的词语，不得说出词本身，也不得拆开说；"
                f"本轮的描述要钝到至少还能套在 {bluntness_n(state['round'])} 样别的东西上"
            ),
        }
        yaml_path, input_path = write_run_files(
            args.session, run_file_kind("speak", state["round"], attempt), yaml_text, run_input
        )
        save_state(state)

    emit(
        {
            "phase": "SPEAK_RUNNING",
            "round": state["round"],
            "attempt": attempt,
            "yaml_path": yaml_path,
            "input_path": input_path,
            "bindings": bindings,
            "binding_args": " ".join(f'--binding "{b}"' for b in bindings),
            "speaking_order": [
                {"seat": s["seat"], "player": s["display"], "kind": s["kind"]} for s in living
            ],
            "human_seat": next(s["seat"] for s in state["seats"] if s["kind"] == "human"),
            "note": "YAML 里含词语，不要读它、不要贴它，直接交给 bcs collaborate run。",
        }
    )


def cmd_speeches_set(args: argparse.Namespace) -> None:
    payload = json.loads(args.json)
    flags = {}
    for item in args.flag or []:
        seat, _, reason = item.partition("=")
        flags[int(seat)] = reason
    with locked(args.session):
        state = load_state(args.session)
        require_phase(state, "SPEAK_RUNNING")
        rnd = current_round(state)
        expected = set(rnd["order"])
        got = {int(k) for k in payload}
        if got != expected:
            die(
                "SPEECH_SET_MISMATCH",
                f"本轮应该有 {sorted(expected)} 号的发言，收到的是 {sorted(got)}。"
                "请把每个座位的原话都取全再提交。",
            )
        results = []
        for seat in rnd["order"]:
            raw = str(payload[str(seat)] if str(seat) in payload else payload[seat]).strip()
            display, reason = check_text(state, seat, raw, SPEECH_MAX_CHARS)
            if seat in flags and not reason:
                reason = flags[seat]
            rnd["speeches"][str(seat)] = {"raw": raw, "display": display, "violation": reason}
            results.append(
                {
                    "seat": seat,
                    "player": seat_of(state, seat)["display"],
                    "kind": seat_of(state, seat)["kind"],
                    "text": display,
                    "violation": reason,
                }
            )
        state["phase"] = "AWAIT_VOTE_START"
        save_state(state)

    emit(
        {
            "phase": "AWAIT_VOTE_START",
            "round": state["round"],
            "speeches": results,
            "next_action": NEXT_ACTION["AWAIT_VOTE_START"],
            "note": "只使用 text 字段念稿，永远不要使用原始文本。",
        }
    )


def cmd_render_vote_run(args: argparse.Namespace) -> None:
    with locked(args.session):
        state = load_state(args.session)
        if args.retry:
            # 投票运行失败不会唤醒裁判，phase 却已经推到 VOTE_RUNNING 了。
            # 没有这条路，卡住诊断走到重开那一步就会被阶段卫兵拦死。
            require_phase(state, "VOTE_RUNNING")
        else:
            require_phase(state, "AWAIT_VOTE_START")
        state["phase"] = "VOTE_RUNNING"
        attempt = bump_render(state, "vote")
        yaml_text, bindings = render_vote_yaml(state)
        living = sorted(alive_seats(state), key=lambda s: s["seat"])
        run_input = {
            "game": "谁是卧底",
            "round": state["round"],
            "phase": "投票",
            "alive": [f"{s['seat']}号 {s['display']}" for s in living],
            "history": public_history(state),
            "rule": "只根据所有人历史全部发言，投出你认为词语和大家不一样的人；不能投自己；只交票号，不写理由",
        }
        yaml_path, input_path = write_run_files(
            args.session, run_file_kind("vote", state["round"], attempt), yaml_text, run_input
        )
        save_state(state)

    emit(
        {
            "phase": "VOTE_RUNNING",
            "round": state["round"],
            "attempt": attempt,
            "yaml_path": yaml_path,
            "input_path": input_path,
            "bindings": bindings,
            "binding_args": " ".join(f'--binding "{b}"' for b in bindings),
            "voters": [
                {"seat": s["seat"], "player": s["display"], "kind": s["kind"]} for s in living
            ],
            "note": "YAML 里含词语，不要读它、不要贴它，直接交给 bcs collaborate run。"
            + ("" if attempt <= 1 else " 这是本轮第 %d 次开投，要向人类说明之前的票作废。" % attempt),
        }
    )


def choose_ping(state: dict[str, Any], eliminated_seat: int | None) -> dict[str, Any] | None:
    """选一个能把裁判叫醒去开下一轮的人。出局者优先（遗言），否则找下一轮首发。"""
    if eliminated_seat is not None:
        s = seat_of(state, eliminated_seat)
        if s["kind"] == "bot":
            return {"kind": "eulogy", "seat": s["seat"], "bot_name": s["bot_name"], "player": s["display"]}
    for s in sorted(alive_seats(state), key=lambda x: x["seat"]):
        if s["kind"] == "bot":
            return {"kind": "standby", "seat": s["seat"], "bot_name": s["bot_name"], "player": s["display"]}
    return None


def cmd_votes_set(args: argparse.Namespace) -> None:
    payload = json.loads(args.json)
    with locked(args.session):
        state = load_state(args.session)
        require_phase(state, "VOTE_RUNNING")
        rnd = current_round(state)
        expected = set(rnd["order"])
        got = {int(k) for k in payload}
        if got != expected:
            die(
                "VOTE_SET_MISMATCH",
                f"本轮应该有 {sorted(expected)} 号的投票，收到的是 {sorted(got)}。"
                "请把每个座位的原话都取全再提交。",
            )

        results = []
        counts: dict[int, int] = {}
        for seat in rnd["order"]:
            raw = str(payload[str(seat)] if str(seat) in payload else payload[seat]).strip()
            _masked, reason = check_text(state, seat, raw, VOTE_MAX_CHARS)
            target, note = parse_vote(state, seat, raw)
            if reason and reason != f"超过 {VOTE_MAX_CHARS} 字":
                target, note = None, "投票内容违规，本票作废"
            # 票面一律规范化成票号，玩家写了什么原话都不往外传。
            #
            # 投票理由是泄露渠道——一条理由就是「拿我的词比对他的话」的结果，念出来
            # 等于广播自己那个词的属性。节点指令里已经要求只交票号，但指令是软的：
            # 只要有一个玩家多写了半句，主持稿就会把它念出去。所以在这里把渠道彻底
            # 关死——主持人拿不到原话，也就没得念、没得猜。
            if target is not None:
                display = f"我投{target}号"
            elif note == "弃权":
                display = "我弃权"
            else:
                display = "无效票"
            rnd["votes"][str(seat)] = {
                "raw": raw,
                "display": display,
                "target": target,
                "violation": reason,
                "note": note,
            }
            if target is not None:
                counts[target] = counts.get(target, 0) + 1
            results.append(
                {
                    "seat": seat,
                    "player": seat_of(state, seat)["display"],
                    "text": display,
                    "target_seat": target,
                    "target_player": seat_of(state, target)["display"] if target else None,
                    "violation": reason,
                    "note": note,
                }
            )

        top = max(counts.values()) if counts else 0
        candidates = sorted(s for s, c in counts.items() if c == top) if top else []
        tie = len(candidates) != 1
        eliminated = None
        forced = False
        if not tie:
            eliminated = candidates[0]
            state["consecutive_ties"] = 0
        elif candidates and state["consecutive_ties"] >= 1:
            eliminated = random.SystemRandom().choice(candidates)
            state["consecutive_ties"] = 0
            forced = True
        else:
            state["consecutive_ties"] += 1

        if eliminated is not None:
            s = seat_of(state, eliminated)
            s["alive"] = False
            s["eliminated_round"] = state["round"]
        rnd["counts"] = {str(k): v for k, v in counts.items()}
        rnd["eliminated"] = eliminated
        rnd["tie"] = tie

        spies_alive = [s for s in alive_seats(state) if s["role"] == "undercover"]
        survivors = alive_seats(state)
        if not spies_alive:
            verdict, winner, reason = "finished", "civilian", "卧底已经全部出局"
        elif len(survivors) <= 2:
            verdict, winner, reason = "finished", "undercover", "场上只剩两个人，卧底还在"
        elif state["round"] >= state["config"]["max_rounds"]:
            verdict, winner, reason = "finished", "undercover", "轮数用完了还没抓到卧底"
        else:
            verdict, winner, reason = "continue", None, ""

        ping = None
        if verdict == "continue":
            ping = choose_ping(state, eliminated)
            state["pending_ping"] = ping
            state["phase"] = "AWAIT_NEXT_ROUND"
        else:
            state["result"] = {"winner": winner, "reason": reason, "round": state["round"]}
            state["phase"] = "FINISHED"
        save_state(state)

    emit(
        {
            "phase": state["phase"],
            "round": state["round"],
            "votes": results,
            "counts": [
                {"seat": s, "player": seat_of(state, s)["display"], "votes": c}
                for s, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            ],
            "tie": tie,
            "forced_by_repeat_tie": forced,
            "eliminated": (
                {"seat": eliminated, "player": seat_of(state, eliminated)["display"]}
                if eliminated
                else None
            ),
            "alive": [
                {"seat": s["seat"], "player": s["display"]} for s in alive_seats(state)
            ],
            "verdict": verdict,
            "winner": winner,
            "win_reason": reason,
            "ping": ping,
            "next_action": NEXT_ACTION[state["phase"]],
            "note": "只使用 text 字段念稿——它已经规范化成票号，玩家的原话不会给你。"
            "不要替玩家编造或猜测投票理由。出局者身份不要公布，除非 verdict 是 finished。",
        }
    )


def cmd_render_ping(args: argparse.Namespace) -> None:
    state = load_state(args.session)
    ping = state["pending_ping"]
    if not ping:
        die("NO_PING", "当前没有待发的遗言或预备任务。")
    if ping["kind"] == "eulogy":
        message = (
            f"你在第 {state['round']} 轮被投出局了。\n"
            "请说一句不超过 20 个字的遗言，可以喊冤也可以放狠话，符合你自己的性格。\n"
            "不要说出你的词语，也不要说自己是不是卧底。\n"
            "只输出这一句话，不要任何解释。"
        )
    else:
        message = (
            f"第 {state['round']} 轮结束了，本轮没有人出局。\n"
            "下一轮由你第一个发言。收到请只回一句不超过 15 个字的话，表示你准备好了，"
            "符合你自己的性格。不要提词语、身份或任何推理。"
        )
    emit(
        {
            "kind": ping["kind"],
            "target_bot": ping["bot_name"],
            "player": ping["player"],
            "message": message,
            "note": "用 bcs_assign_task 把 message 原样发给 target_bot；它的回执会把你叫醒开下一轮。",
        }
    )


def cmd_render_vote_watchdog(args: argparse.Namespace) -> None:
    """渲染一条不依赖人类的兜底唤醒任务。

    投票运行失败时裁判不会被唤醒，唯一的兜底是人类主动喊「卡住了」。这条任务派给
    一个**已出局**的 Bot：它在本轮投票运行里没有任何节点，通道整场空着，多这一条
    消息不会和它自己的投票节点抢通道。它的回执会把裁判叫醒，裁判醒来先查 status
    和 permission，发现运行已经失败就直接重开。

    第一轮没有出局者，所以没有安全的看门狗人选——那一轮只能靠人类兜底，主持稿里
    必须把时限说清楚。绝不退回给存活玩家：那等于故意在别人的通道里塞第二件事，
    也正是入口节点不挑已出局玩家、把这条通道整个让出来的原因。
    """
    state = load_state(args.session)
    require_phase(state, "VOTE_RUNNING")
    dead_bots = [
        s for s in state["seats"] if s["kind"] == "bot" and not s["alive"] and s["bot_name"]
    ]
    if not dead_bots:
        emit(
            {
                "available": False,
                "reason": "场上还没有出局的 Bot，这一轮没有安全的看门狗人选。",
                "note": "不要改派给存活玩家。这一轮的兜底是人类：主持稿里说清超过 5 分钟没结果就回你一句。",
            }
        )
        return
    target = max(dead_bots, key=lambda s: (s["eliminated_round"] or 0, s["seat"]))
    message = (
        "【看门狗任务】\n"
        f"第 {state['round']} 轮投票正在进行，你已经出局了，不参与投票。\n"
        "请等大约三分钟，然后只回一句：看门狗回执。\n"
        "不要说别的，不要提词语、身份、发言或任何推理。"
    )
    emit(
        {
            "available": True,
            "target_bot": target["bot_name"],
            "player": target["display"],
            "message": message,
            "note": "用 bcs_assign_task 把 message 原样发给 target_bot。"
            "收到「看门狗回执」时先查 status 和 collaborate permission：allowed 就说明投票运行已经失败，走卡住诊断重开。",
        }
    )


def cmd_reveal(args: argparse.Namespace) -> None:
    state = load_state(args.session)
    if state["phase"] != "FINISHED":
        die("NOT_FINISHED", "本局还没结束，现在不能公布真相。")
    emit(
        {
            "winner": state["result"]["winner"],
            "win_reason": state["result"]["reason"],
            "words": state["words"],
            "seats": [
                {
                    "seat": s["seat"],
                    "player": s["display"],
                    "role": s["role"],
                    "word": s["word"],
                    "eliminated_round": s["eliminated_round"],
                }
                for s in sorted(state["seats"], key=lambda x: x["seat"])
            ],
            "rounds": [
                {
                    "round": r["round"],
                    "speeches": {k: v["display"] for k, v in r["speeches"].items()},
                    "votes": {k: v["target"] for k, v in r["votes"].items()},
                    "eliminated": r["eliminated"],
                    "tie": r["tie"],
                }
                for r in state["rounds"]
            ],
        }
    )


def cmd_parse_vote(args: argparse.Namespace) -> None:
    state = load_state(args.session)
    target, note = parse_vote(state, args.voter, args.text)
    emit(
        {
            "target_seat": target,
            "target_player": seat_of(state, target)["display"] if target else None,
            "note": note,
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="谁是卧底 · 裁判事实层")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_session(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--session", required=True)
        return p

    p = with_session(sub.add_parser("init", help="开一局：抽词、排座位、抽卧底"))
    p.add_argument("--group", required=True)
    p.add_argument("--human", required=True)
    p.add_argument("--referee-uuid", required=True)
    p.add_argument("--bot", action="append", default=[], metavar="名称=UUID")
    p.add_argument("--difficulty", default="medium", choices=["easy", "medium", "hard"])
    p.add_argument("--undercover", type=int, default=1)
    p.add_argument("--max-rounds", type=int, default=6)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    with_session(sub.add_parser("status", help="当前阶段和下一步")).set_defaults(func=cmd_status)

    p = with_session(sub.add_parser("render-speak-run", help="渲染本轮发言协作"))
    p.add_argument(
        "--retry",
        action="store_true",
        help="重开当前这一轮的发言运行（上一次提交的运行失败了）。轮次不推进。",
    )
    p.set_defaults(func=cmd_render_speak_run)

    p = with_session(sub.add_parser("render-vote-run", help="渲染本轮投票协作"))
    p.add_argument(
        "--retry",
        action="store_true",
        help="重开当前这一轮的投票运行（上一次提交的运行失败了）。之前的票作废。",
    )
    p.set_defaults(func=cmd_render_vote_run)

    with_session(sub.add_parser("render-ping", help="渲染遗言或预备任务")).set_defaults(
        func=cmd_render_ping
    )
    with_session(
        sub.add_parser("render-vote-watchdog", help="渲染投票期间的兜底唤醒任务")
    ).set_defaults(func=cmd_render_vote_watchdog)
    with_session(sub.add_parser("reveal", help="终局公布真相")).set_defaults(func=cmd_reveal)

    p = with_session(sub.add_parser("speeches-set", help="提交本轮发言：检查、遮蔽、落盘"))
    p.add_argument("--json", required=True, metavar='{"1":"...","3":"..."}')
    p.add_argument("--flag", action="append", default=[], metavar="座位=原因")
    p.set_defaults(func=cmd_speeches_set)

    p = with_session(sub.add_parser("votes-set", help="提交本轮投票：解析、计票、判定"))
    p.add_argument("--json", required=True, metavar='{"1":"...","3":"..."}')
    p.set_defaults(func=cmd_votes_set)

    p = with_session(sub.add_parser("parse-vote", help="单独解析一句投票（调试用）"))
    p.add_argument("--text", required=True)
    p.add_argument("--voter", type=int, default=None)
    p.set_defaults(func=cmd_parse_vote)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
