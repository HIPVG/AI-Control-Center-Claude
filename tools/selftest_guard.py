#!/usr/bin/env python3
"""ガードの違反試験（標準 D4）。一時ディレクトリで実行し、実リポジトリは変更しない。
使い方: python tools/selftest_guard.py"""
import json, os, shutil, subprocess, sys, tempfile, time
from datetime import timedelta
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC / ".claude" / "hooks"))
import acc_common as C0

T = Path(tempfile.mkdtemp(prefix="acc-selftest-"))
shutil.copytree(SRC / ".claude" / "hooks", T / ".claude" / "hooks", ignore=shutil.ignore_patterns("__pycache__"))
shutil.copytree(SRC / "tools", T / "tools", ignore=shutil.ignore_patterns("__pycache__"))
ENV = {**os.environ, "CLAUDE_PROJECT_DIR": str(T)}
fails = 0


def run(args, stdin=None):
    return subprocess.run([sys.executable, *args], cwd=T, env=ENV, input=stdin, capture_output=True, text=True)


def guard(payload, raw=None):
    return run([".claude/hooks/acc_guard.py"], raw if raw is not None else json.dumps(payload)).returncode


def guard_utf8_bytes(payload):
    """UTF-8のバイト列をそのまま標準入力へ渡す。text=True 経由だと、この
    サンドボックスの既定ロケール次第でエンコードが変わってしまい、Windows の
    CP932 誤読（2026-09-29 に実機で発見）を再現・回帰検知できないため、
    バイト列を明示して環境非依存にする。"""
    r = subprocess.run([sys.executable, ".claude/hooks/acc_guard.py"], cwd=T, env=ENV,
                        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"), capture_output=True)
    return r.returncode


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(("PASS " if ok else "FAIL ") + f"{name} (exit={got}, expected={want})")


def card(cid, secs=3600, calls=50, chk="self"):
    filled = "記入済み"
    return {"card_id": cid, "status": "DRAFT", "tier": "lightweight", "modules": [],
            "purpose": {"goal": filled, "non_goals": [filled]},
            "target_and_current_state": {"targets": [filled], "out_of_scope": [filled]},
            "decision_owner": {"name": "owner", "governance_not_needed_because": filled},
            "permissions_and_cost": {"allowed": [filled], "forbidden": [filled], "cost": "費用なし"},
            "actors_and_authority": {"actors": [filled]},
            "verification_and_evidence": {"method": filled},
            "stop_and_recovery": {"stop": filled},
            "result_and_open_items": {},
            "scope": {"allowed_paths": ["src/**", "tests/**", "docs/**"], "forbidden_paths": ["src/secret/**"],
                      "allowed_commands": ["python -m pytest", "git status", "git diff"]},
            "budget": {"deadline_utc": C0.iso(C0.now() + timedelta(seconds=secs)), "max_tool_calls": calls},
            "acceptance": [{"id": "A1", "criterion": "終了コード0", "command": "python -c pass", "check": chk}]}


def activate(c):
    p = T / "work-cards" / "drafts" / f"{c['card_id']}.json"
    C0.write_json(p, c)
    return run(["tools/acc.py", "activate", str(p), "--approve-as", "owner"])


W = lambda p: {"tool_name": "Write", "tool_input": {"file_path": p}}
B = lambda c: {"tool_name": "Bash", "tool_input": {"command": c}}

check("カードなしで編集は拒否", guard(W("src/a.py")), 2)
check("下書きカードの作成は許可", guard(W("work-cards/drafts/x.json")), 0)
check("不正な入力はフェイルクローズ", guard(None, raw="not json"), 2)
bad = card("BAD-001"); bad["purpose"]["goal"] = ""
check("未記入カードは有効化できない", activate(bad).returncode, 1)
check("正しいカードの有効化", activate(card("T-001")).returncode, 0)
check("範囲内の編集は許可", guard(W("src/a.py")), 0)
check("範囲外の編集は拒否", guard(W("other/a.py")), 2)
check("カードの forbidden_paths は拒否", guard(W("src/secret/k.py")), 2)
check("受入テストは allowed でも拒否", guard(W("tests/acceptance/test_x.py")), 2)
check("正本規則は allowed でも拒否", guard(W("docs/WORKING_RULES.md")), 2)
check(".claude 改変は拒否", guard(W(".claude/settings.json")), 2)
check("作業ルート外は拒否", guard(W("../evil.py")), 2)
check("許可コマンドは許可", guard(B("python -m pytest tests/unit")), 0)
check("git push は拒否", guard(B("git push origin main")), 2)
check("コマンド連結は拒否", guard(B("python -m pytest && echo x")), 2)
check("リダイレクトは拒否", guard(B("git status > out.txt")), 2)
check("許可リスト外のコマンドは拒否", guard(B("python -c print(1)")), 2)
ap = T / "work-cards" / "approved" / "T-001.json"
orig = ap.read_text(encoding="utf-8")
ap.write_text(orig.replace("owner", "attacker"), encoding="utf-8")
check("承認後のカード改変を検知して拒否", guard(W("src/a.py")), 2)
ap.write_text(orig, encoding="utf-8")
check("復元すれば再び許可", guard(W("src/a.py")), 0)
run(["tools/acc.py", "stop", "--reason", "test"])
check("STOP 中は拒否", guard(W("src/a.py")), 2)
run(["tools/acc.py", "resume", "--by", "owner"])
check("STOP 解除後は許可", guard(W("src/a.py")), 0)
activate(card("T-002", calls=2))
check("上限内 1回目", guard(W("src/a.py")), 0)
check("上限内 2回目", guard(W("src/a.py")), 0)
check("回数上限超過は拒否", guard(W("src/a.py")), 2)
activate(card("T-003", secs=2)); time.sleep(3)
check("期限超過は拒否", guard(W("src/a.py")), 2)

activate(card("T-004", chk="independent"))
run(["tools/acc.py", "verify"])
ev = sorted((T / ".acc" / "evidence" / "T-004").glob("*.json"))[-1]
check("独立確認が必要な条件の自己受入は拒否",
      run(["tools/acc.py", "accept", "--evidence", str(ev), "--by", "o", "--kind", "self", "--open-items", "なし"]).returncode, 1)
check("独立受入として記録できる",
      run(["tools/acc.py", "accept", "--evidence", str(ev), "--by", "o", "--kind", "independent", "--open-items", "なし"]).returncode, 0)

# 回帰試験: Windowsネイティブの一部バージョンで、Claude Code がフックへ渡す JSON 内の
# パスのバックスラッシュを正しくエスケープしないまま送ってくることがあった
# (例: "work-cards\drafts\x.json" の \d が不正な JSON エスケープになる)。
# 2026-09-29 に実機で発見。json.loads を素通しする guard(raw=...) で、意図的に壊れた
# JSON を直接送り、正しくパスとして解釈されることを確認する。
activate(card("T-005"))
# file_path 自体はスラッシュ区切りにして OS 依存を避け、content 側に Windows 風の
# 不正エスケープ（\d, \x）を混ぜて、JSON 解析の回復だけを検証する。
raw_broken = r'{"tool_name": "Write", "tool_input": {"file_path": "src/a.py", "content": "work-cards\drafts\x.json"}}'
check("不正エスケープを含むJSONでも解析できる(回帰)", guard(None, raw=raw_broken), 0)

# 回帰試験: Windows で sys.stdin が既定エンコーディング(CP932等)で開かれ、UTF-8で
# 送られた日本語パス（例: C:\Users\広瀬剛\...）の多バイト文字が直後のバックスラッシュ
# を巻き込んで誤読され、JSON が壊れた（2026-09-29 実機で発見・特定）。
# sys.stdin.buffer を明示的に UTF-8 でデコードすることで解消したことを確認する。
activate(card("T-006"))
check("日本語(マルチバイト)を含むパスでも解析できる(回帰)",
      guard_utf8_bytes({"tool_name": "Write", "tool_input": {"file_path": "src/a.py",
                                                              "content": "C:\\Users\\広瀬剛\\work-cards\\drafts\\x.json"}}), 0)

# 回帰試験: ブートストラップ例外（2026-09-29 実機で発見した「有効化するために有効化が
# 要る」循環の解消）。STOP 中でも、有効カードが無くても tools/acc.py は許可される。
run(["tools/acc.py", "stop", "--reason", "bootstrap-test"])
check("STOP中でもtools/acc.pyの呼び出しは許可される(回帰)",
      guard(B("python tools/acc.py resume --by owner")), 0)
run(["tools/acc.py", "resume", "--by", "owner"])
(T / ".acc" / "ACTIVE.json").unlink(missing_ok=True)
check("前提: 有効カード無しでは通常のBashは拒否", guard(B("python -m pytest")), 2)
check("有効カードが無くてもtools/acc.py activateは実行できる(回帰)",
      guard(B("python tools/acc.py activate work-cards/drafts/x.json --approve-as owner")), 0)

print(f"\n{'全て合格' if not fails else str(fails) + ' 件失敗'}（作業場所: {T}）")
sys.exit(1 if fails else 0)
