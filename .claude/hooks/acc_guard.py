#!/usr/bin/env python3
"""PreToolUse ガード。exit 2 で操作を拒否し、理由を stderr でモデルへ返す。
例外・不正入力はすべて拒否（フェイルクローズ）。判定は決定的で、AI 判断を含まない。"""
import json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acc_common as C

FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

_STRAY_BACKSLASH = re.compile(r'\\(?!["\\/bfnrtu])')


def parse_tool_call(raw):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(_STRAY_BACKSLASH.sub(r"\\\\", raw))
    except json.JSONDecodeError as e:
        C.ACC.mkdir(exist_ok=True)
        (C.ACC / "debug").mkdir(exist_ok=True)
        p = C.ACC / "debug" / (C.iso(C.now()).replace(":", "") + ".txt")
        try:
            p.write_text(raw, encoding="utf-8", errors="replace")
        except Exception:
            pass
        raise json.JSONDecodeError(f"{e} (原文を {p} に保存した)", e.doc, e.pos)


def block(reason, **ctx):
    C.audit({"decision": "BLOCK", "reason": reason, **ctx})
    sys.stderr.write("ACC-GUARD BLOCKED: " + reason + "\n")
    sys.exit(2)


def allow(**ctx):
    C.audit({"decision": "ALLOW", **ctx})
    reason = "ACC-GUARD: 有効カード " + str(ctx.get("card")) + " の許可範囲内" if ctx.get("card") else ("ACC-GUARD: " + str(ctx.get("note", "許可")))
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                              "permissionDecision": "allow",
                                              "permissionDecisionReason": reason}}))
    sys.exit(0)


def count_call(card):
    usage = C.read_json(C.ACC / "usage.json", {})
    n = usage.get(card["card_id"], 0)
    if n >= card["budget"]["max_tool_calls"]:
        block(f"操作回数の上限 {card['budget']['max_tool_calls']} に達した。判断依頼を返して止まる", card=card["card_id"])
    usage[card["card_id"]] = n + 1
    C.write_json(C.ACC / "usage.json", usage)


# ブートストラップ例外の対象コマンド。tools/acc.py 自身の呼び出しと、状態を変更
# しない読み取り専用コマンドは、有効カードの有無や STOP 状態に関わらず常に許可
# する。理由:
# (a) tools/acc.py はカードの有効化・STOP解除など統制そのものを行う唯一の手段。
#     これをゲート対象にすると「有効化するために有効化が要る」「STOPを解除する
#     ためにSTOPが解除されている必要がある」という循環に陥る（2026-09-29 実機で
#     発見）。
# (b) tools/acc.py は対象コード(work-cardsのallowed_paths配下)を一切書き換えない。
#     カードの検証・状態管理・受入記録を行う統制プレーンそのものであり、ACC-GUARDが
#     防ごうとしている「無審査の実装行為」には当たらない。加えて tools/acc.py 自身が
#     ハッシュ改ざん検証・independent受入の自己受入拒否など固有の安全検証を持つ。
# (c) 読み取り専用コマンドは状態を変えないため、診断目的の実行を安全側に倒して
#     常に許可する。
_ACC_CLI = re.compile(r'^python3?\s+tools/acc\.py\b')
_READONLY = (re.compile(r'^git\s+(status|log|diff|branch|show|rev-parse)\b'),
             re.compile(r'^(ls|pwd|cat|find|head|tail|wc)\b'))


def main():
    raw = sys.stdin.buffer.read().decode("utf-8")
    data = parse_tool_call(raw)
    tool = data.get("tool_name", "")
    ti = data.get("tool_input") or {}
    ctx = {"tool": tool}

    if tool == "Bash":
        cmd = (ti.get("command") or "").strip()
        ctx["command"] = cmd
        if C.META.search(cmd):
            block("連結・リダイレクト・置換を含むコマンドは不可（単純な 1 コマンドのみ）", **ctx)
        if _ACC_CLI.match(cmd):
            allow(note="acc.py 統制コマンド", **ctx)
        if any(p.match(cmd) for p in _READONLY):
            allow(note="読み取り専用コマンド", **ctx)

    if (C.ACC / "STOP").exists():
        block("STOP が有効。新規操作を行わず、状況を報告して明示的な再開指示を待つ", **ctx)

    if tool in FILE_TOOLS:
        fp = ti.get("file_path") or ti.get("notebook_path") or ""
        rel = C.rel_in_root(fp) if fp else None
        ctx["path"] = fp
        if rel is None:
            block("作業ルート外、又はパス不明のため拒否", **ctx)
        if C.matches(rel, [C.DRAFTS_GLOB]):
            allow(note="draft card", **ctx)
        card, _, errs = C.load_active()
        if errs:
            block("; ".join(errs), **ctx)
        sc = card["scope"]
        if C.matches(rel, C.PROTECTED):
            block(f"{rel} は保護対象（実装担当は変更不可）", **ctx)
        if C.matches(rel, sc["forbidden_paths"]):
            block(f"{rel} はカードの forbidden_paths", **ctx)
        if not C.matches(rel, sc["allowed_paths"]):
            block(f"{rel} はカードの allowed_paths 外。範囲拡張は人間判断", **ctx)
        count_call(card)
        allow(card=card["card_id"], **ctx)

    if tool == "Bash":
        cmd = ctx["command"]
        card, _, errs = C.load_active()
        if errs:
            block("; ".join(errs), **ctx)
        for pat in C.DENIED_CMD:
            if re.search(pat, cmd, re.I):
                block(f"禁止コマンドに該当: {pat}", **ctx)
        toks = cmd.split()
        if not any(toks[:len(p.split())] == p.split() for p in card["scope"]["allowed_commands"]):
            block("カードの allowed_commands にないコマンド", **ctx)
        count_call(card)
        allow(card=card["card_id"], **ctx)

    block(f"想定外のツール {tool}（フェイルクローズ）", **ctx)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as ex:
        sys.stderr.write(f"ACC-GUARD BLOCKED: guard error {type(ex).__name__}: {ex}\n")
        sys.exit(2)
