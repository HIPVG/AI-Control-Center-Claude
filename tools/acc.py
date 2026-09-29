#!/usr/bin/env python3
"""人間（又は CI）が使う操作。AI の Bash 許可には入れないこと。
  activate  下書きカードを検査し、承認者を記録して有効化
  stop / resume   STOP の発行と解除
  status    現在の状態
  verify    カードの受入コマンドを固定条件で実行し、証拠を保存（合格でも COMPLETE ではない）
  accept    証拠・HEAD・受入テストのハッシュが一致するとき、人間の受入として COMPLETE を記録"""
import argparse, hashlib, json, os, shlex, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / ".claude" / "hooks"))
import acc_common as C


def git(*a):
    try:
        r = subprocess.run(["git", *a], cwd=C.ROOT, capture_output=True, text=True, timeout=30)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def tree_hash(rel):
    d = C.ROOT / rel
    if not d.exists():
        return "ABSENT"
    h = hashlib.sha256()
    for p in sorted(x for x in d.rglob("*") if x.is_file() and "__pycache__" not in x.parts):
        h.update(p.relative_to(d).as_posix().encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def cmd_activate(a):
    card = C.read_json(a.card)
    if card.get("status") != "DRAFT":
        sys.exit("status が DRAFT のカードだけ有効化できる")
    errs = C.validate_card(card)
    if errs:
        sys.exit("検査不合格:\n- " + "\n- ".join(errs))
    card["status"], card["approved_by"], card["approved_at"] = "READY", a.approve_as, C.iso(C.now())
    dst = C.APPROVED / f"{card['card_id']}.json"
    if dst.exists():
        sys.exit("同じ card_id が承認済み。新しい card_id を使う")
    C.write_json(dst, card)
    C.write_json(C.ACC / "ACTIVE.json", {"card_id": card["card_id"], "sha256": C.sha256_file(dst)})
    usage = C.read_json(C.ACC / "usage.json", {}); usage[card["card_id"]] = 0
    C.write_json(C.ACC / "usage.json", usage)
    C.audit({"event": "ACTIVATE", "card": card["card_id"], "by": a.approve_as})
    print(f"有効化: {card['card_id']}（承認 {a.approve_as}、期限 {card['budget']['deadline_utc']}）")


def cmd_stop(a):
    C.ACC.mkdir(exist_ok=True)
    (C.ACC / "STOP").write_text(C.iso(C.now()) + " " + (a.reason or ""), encoding="utf-8")
    C.audit({"event": "STOP", "reason": a.reason}); print("STOP を発行した")


def cmd_resume(a):
    (C.ACC / "STOP").unlink(missing_ok=True)
    C.audit({"event": "RESUME", "by": a.by}); print("STOP を解除した（新しい明示指示として記録）")


def cmd_status(a):
    card, _, errs = C.load_active()
    print("STOP:", (C.ACC / "STOP").exists())
    print("有効カード:", card["card_id"] if card else None, "| 問題:", errs or "なし")
    print("操作回数:", C.read_json(C.ACC / "usage.json", {}))


def cmd_verify(a):
    card, _, errs = C.load_active()
    if errs:
        sys.exit("有効カードなし: " + "; ".join(errs))
    results, ok = [], True
    for item in card["acceptance"]:
        t0 = time.time()
        try:
            r = subprocess.run(shlex.split(item["command"], posix=(os.name != "nt")), cwd=C.ROOT,
                               capture_output=True, text=True, timeout=1800)
            code, out = r.returncode, (r.stdout + r.stderr)
        except Exception as ex:
            code, out = -1, f"{type(ex).__name__}: {ex}"
        ok &= code == 0
        results.append({"id": item["id"], "command": item["command"], "exit_code": code,
                        "duration_s": round(time.time() - t0, 2), "output_tail": out[-2000:],
                        "output_sha256": hashlib.sha256(out.encode()).hexdigest()})
    ev = {"card_id": card["card_id"], "card_sha256": C.sha256_file(C.APPROVED / f"{card['card_id']}.json"),
          "git_head": git("rev-parse", "HEAD"), "git_dirty": bool(git("status", "--porcelain")),
          "acceptance_tree_sha256": tree_hash("tests/acceptance"), "verified_at": C.iso(C.now()),
          "results": results, "machine_result": "PASSED" if ok else "FAILED",
          "note": "終了コード0は受入証拠の一部に過ぎない。COMPLETE ではない。"}
    p = C.ACC / "evidence" / card["card_id"] / (C.now().strftime("%Y%m%dT%H%M%SZ") + ".json")
    C.write_json(p, ev); C.audit({"event": "VERIFY", "card": card["card_id"], "result": ev["machine_result"]})
    print(ev["machine_result"], "->", p)


def cmd_accept(a):
    card, _, errs = C.load_active()
    if errs:
        sys.exit("有効カードなし: " + "; ".join(errs))
    ev = C.read_json(a.evidence)
    checks = [
        (ev["machine_result"] == "PASSED", "機械検査が合格でない"),
        (ev["card_id"] == card["card_id"], "証拠のカードが違う"),
        (ev["card_sha256"] == C.sha256_file(C.APPROVED / f"{card['card_id']}.json"), "カードが検証後に変わった"),
        (ev["git_head"] == git("rev-parse", "HEAD"), "検証後に HEAD が変わった。再検証が必要"),
        (ev["acceptance_tree_sha256"] == tree_hash("tests/acceptance"), "受入テストが検証後に変わった"),
        (not (a.kind == "self" and any(x["check"] == "independent" for x in card["acceptance"])),
         "独立確認が必要な受入条件を自己受入にはできない"),
    ]
    bad = [m for ok, m in checks if not ok]
    if bad:
        sys.exit("受入拒否:\n- " + "\n- ".join(bad))
    rec = {"card_id": card["card_id"], "status": "COMPLETE", "acceptance_kind": a.kind,
           "accepted_by": a.by, "accepted_at": C.iso(C.now()), "evidence": str(a.evidence),
           "evidence_sha256": C.sha256_file(a.evidence), "open_items": a.open_items}
    C.write_json(C.ACC / "acceptance" / f"{card['card_id']}.json", rec)
    C.audit({"event": "ACCEPT", "card": card["card_id"], "kind": a.kind, "by": a.by})
    print(f"COMPLETE（{'自己受入' if a.kind == 'self' else '独立受入'}）: {card['card_id']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); sp = ap.add_subparsers(dest="c", required=True)
    x = sp.add_parser("activate"); x.add_argument("card"); x.add_argument("--approve-as", required=True); x.set_defaults(f=cmd_activate)
    x = sp.add_parser("stop"); x.add_argument("--reason", default=""); x.set_defaults(f=cmd_stop)
    x = sp.add_parser("resume"); x.add_argument("--by", required=True); x.set_defaults(f=cmd_resume)
    x = sp.add_parser("status"); x.set_defaults(f=cmd_status)
    x = sp.add_parser("verify"); x.set_defaults(f=cmd_verify)
    x = sp.add_parser("accept"); x.add_argument("--evidence", required=True); x.add_argument("--by", required=True)
    x.add_argument("--kind", choices=["self", "independent"], required=True)
    x.add_argument("--open-items", required=True, help="未検証・残課題。なければ「なし」"); x.set_defaults(f=cmd_accept)
    a = ap.parse_args(); a.f(a)
