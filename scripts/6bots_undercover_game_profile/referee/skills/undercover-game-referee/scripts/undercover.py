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

STATE_VERSION = 2

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
    "VOTE_RUNNING": "等投票协作把须知节点和计票节点派给你；计票节点上调 votes-set。",
    "AWAIT_NEXT_ROUND": "等出局者的遗言回执；收到后 render-speak-run 开下一轮。",
    "FINISHED": "本局已结束，只能 reveal。想再来一局请新建会话。",
}

SPEECH_MAX_CHARS = 25
VOTE_MAX_CHARS = 40
MASK = "○"

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
        if s["kind"] == "human":
            instruction = (
                f"【你的词语】{s['word']}\n\n"
                f"第 {rnd} 轮 · 你是 {s['seat']} 号 · 轮到你发言了。\n"
                f"上方「上游产物」里是本轮在你之前玩家的原话，历史轮次在本次运行的输入里。\n\n"
                f"请写一句话（不超过 {SPEECH_MAX_CHARS} 个字）描述你的词语。\n"
                f"{forbid_line(s['word'])}\n"
                "不要提身份、不要点评别人，只描述你的词。"
            )
            nodes.append(
                f"      {nid}:\n"
                f"        kind: human_input\n"
                f"        display_name: {yq(str(s['seat']) + '号发言')}\n"
                f"        node_timeout_ms: 900000\n"
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
                f"{forbid_line(s['word'])}\n"
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
        "      node_timeout_ms: 180000\n"
        "      max_attempts: 2\n"
        "    nodes:\n" + "\n".join(nodes) + "\n"
    )
    bindings = [f"{s['binding']}={s['bot_uuid']}" for s in living if s["kind"] == "bot"]
    bindings.append(f"referee={state['referee_uuid']}")
    return yaml_text, bindings


def render_vote_yaml(state: dict[str, Any]) -> tuple[str, list[str]]:
    rnd = state["round"]
    living = sorted(alive_seats(state), key=lambda s: s["seat"])
    node_ids = [f"vote_{s['seat']}" for s in living]
    seat_list = "、".join(f"{s['seat']}号 {s['display']}" for s in living)

    participants = []
    for s in living:
        if s["kind"] == "bot":
            participants.append(
                f"  {s['binding']}:\n"
                f"    display_name: {yq(str(s['seat']) + '号 ' + s['display'])}\n"
                f"    required: true"
            )
    participants.append('  referee:\n    display_name: "主持人"\n    required: true')

    open_instruction = (
        "【裁判节点 · 投票须知】\n"
        f"这是第 {rnd} 轮投票的开场节点。只输出一句话：本轮投票开始，请各位根据全部发言投票。\n"
        "不要复述发言，不要点评，不要透露任何词语或身份，不要超过 30 个字。"
    )
    nodes = [
        "      vote_open:\n"
        "        kind: bot_task\n"
        '        display_name: "投票须知"\n'
        "        assignee:\n"
        "          type: bot_binding\n"
        "          binding: referee\n"
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
                f"本轮及历史全部发言在本次运行的输入里，主持人也已经在群里念过一遍。\n\n"
                f"可以投的人：{others}。不能投自己。\n"
                "请以「我投N号」开头写一句话，后面用一句话说说为什么，"
                "并且引用你要投的那个人说过的一句原话。\n"
                "不想投就写「我弃权」加一句原因。"
            )
            nodes.append(
                f"      {nid}:\n"
                f"        kind: human_input\n"
                f"        display_name: {yq(str(s['seat']) + '号投票')}\n"
                f"        node_timeout_ms: 900000\n"
                f"        instruction: |\n{block(instruction, 10)}\n"
                f"        transitions:\n"
                f"          complete:\n"
                f"            targets: [tally]"
            )
        else:
            instruction = (
                f"你是本局的 {s['seat']} 号玩家。你的词语是【{s['word']}】。\n"
                f"现在是第 {rnd} 轮投票。全场玩家是：{seat_list}。\n"
                f"本轮及历史全部发言在 [Input] 里，逐字可查。\n"
                f"可以投的人：{others}。不能投自己。\n"
                "输出格式：以「我投N号」开头的一句话，后面接一句你自己口吻的理由，"
                "理由里必须引用你要投的那个人说过的一句原话。整句不超过 "
                f"{VOTE_MAX_CHARS} 个字。\n"
                f"{forbid_line(s['word'])}\n"
                "不要提「卧底」「平民」「身份」，不要输出编号列表、JSON 或解释。\n"
                "确实无法判断时，写「我弃权」加一句原因。"
            )
            nodes.append(
                f"      {nid}:\n"
                f"        kind: bot_task\n"
                f"        display_name: {yq(str(s['seat']) + '号投票')}\n"
                f"        assignee:\n"
                f"          type: bot_binding\n"
                f"          binding: {s['binding']}\n"
                f"        instruction: |\n{block(instruction, 10)}\n"
                f"        transitions:\n"
                f"          complete:\n"
                f"            targets: [tally]"
            )

    tally_instruction = (
        "【裁判节点 · 计票】\n"
        "[Upstream Outputs] 里是本轮全部玩家的投票原话。\n"
        "1. 把每个座位的原话整理成 JSON，调 undercover.py votes-set 交给事实层计票。\n"
        "2. 用它返回的结果写开票主持稿：逐条念票（引用原话）、报票数、宣布出局者、"
        "说明身份暂不公布、报剩下几个人。判定一律以事实层返回为准，不要自己数票。\n"
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
        "      node_timeout_ms: 180000\n"
        "      max_attempts: 2\n"
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
            "result": state["result"],
            "next_action": NEXT_ACTION.get(state["phase"], "先跑 status"),
        }
    )


def cmd_render_speak_run(args: argparse.Namespace) -> None:
    with locked(args.session):
        state = load_state(args.session)
        require_phase(state, "AWAIT_START", "AWAIT_NEXT_ROUND")
        state["round"] += 1
        state["pending_ping"] = None
        living = sorted(alive_seats(state), key=lambda s: s["seat"])
        state["rounds"].append(
            {
                "round": state["round"],
                "order": [s["seat"] for s in living],
                "speeches": {},
                "votes": {},
                "counts": {},
                "eliminated": None,
                "tie": False,
            }
        )
        state["phase"] = "SPEAK_RUNNING"
        yaml_text, bindings = render_speak_yaml(state)
        run_input = {
            "game": "谁是卧底",
            "round": state["round"],
            "alive": [f"{s['seat']}号 {s['display']}" for s in living],
            "history": public_history(state),
            "rule": f"用一句话（不超过{SPEECH_MAX_CHARS}字）描述你的词语，不得说出词本身，也不得拆开说",
        }
        yaml_path, input_path = write_run_files(args.session, f"speak-r{state['round']}", yaml_text, run_input)
        save_state(state)

    emit(
        {
            "phase": "SPEAK_RUNNING",
            "round": state["round"],
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
        require_phase(state, "AWAIT_VOTE_START")
        state["phase"] = "VOTE_RUNNING"
        yaml_text, bindings = render_vote_yaml(state)
        living = sorted(alive_seats(state), key=lambda s: s["seat"])
        run_input = {
            "game": "谁是卧底",
            "round": state["round"],
            "phase": "投票",
            "alive": [f"{s['seat']}号 {s['display']}" for s in living],
            "history": public_history(state),
            "rule": "根据全部发言投出你认为词语和大家不一样的人，不能投自己",
        }
        yaml_path, input_path = write_run_files(args.session, f"vote-r{state['round']}", yaml_text, run_input)
        save_state(state)

    emit(
        {
            "phase": "VOTE_RUNNING",
            "round": state["round"],
            "yaml_path": yaml_path,
            "input_path": input_path,
            "bindings": bindings,
            "binding_args": " ".join(f'--binding "{b}"' for b in bindings),
            "voters": [
                {"seat": s["seat"], "player": s["display"], "kind": s["kind"]} for s in living
            ],
            "note": "YAML 里含词语，不要读它、不要贴它，直接交给 bcs collaborate run。",
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
            display, reason = check_text(state, seat, raw, VOTE_MAX_CHARS)
            target, note = parse_vote(state, seat, raw)
            if reason and reason != f"超过 {VOTE_MAX_CHARS} 字":
                target, note = None, "投票内容违规，本票作废"
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
            "note": "只使用 text 字段念稿。出局者身份不要公布，除非 verdict 是 finished。",
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
    with_session(sub.add_parser("render-speak-run", help="渲染本轮发言协作")).set_defaults(
        func=cmd_render_speak_run
    )
    with_session(sub.add_parser("render-vote-run", help="渲染本轮投票协作")).set_defaults(
        func=cmd_render_vote_run
    )
    with_session(sub.add_parser("render-ping", help="渲染遗言或预备任务")).set_defaults(
        func=cmd_render_ping
    )
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
