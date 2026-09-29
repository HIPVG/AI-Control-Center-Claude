"""AI-Control-Center 共通部品（hook と tools/acc.py が共有）。標準ライブラリのみ。"""
import hashlib, json, os, re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()
ACC = ROOT / ".acc"
APPROVED = ROOT / "work-cards" / "approved"
DRAFTS_GLOB = "work-cards/drafts/**"

CARD_ITEMS = ["purpose", "target_and_current_state", "decision_owner",
              "permissions_and_cost", "actors_and_authority",
              "verification_and_evidence", "stop_and_recovery", "result_and_open_items"]

PROTECTED = [".claude/**", ".acc/**", "CLAUDE.md", "AGENTS.md", "docs/WORKING_RULES.md",
             "tests/acceptance/**", "work-cards/approved/**", "tools/acc.py"]

DENIED_CMD = [
    r"\bgit\s+(push|reset|clean|stash|checkout|restore|rebase|filter-branch|config)\b",
    r"--force|--amend|--no-verify",
    r"\b(rm|del|rmdir|rd|sudo|icacls|chmod|chown)\b",
    r"\b(pip3?|npm|yarn|winget|choco)\s+(install|i|add|uninstall)\b",
    r"\b(curl|wget|Invoke-WebRequest|Invoke-RestMethod|iwr|irm)\b",
    r"\b(powershell|pwsh|cmd|bash|sh)\b",
]
META = re.compile(r"[;&|<>`\n\r]|\$\(")


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def sha256_file(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def read_json(p, default=None):
    p = Path(p)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def audit(event):
    ACC.mkdir(exist_ok=True)
    rec = {"ts": iso(now()), **event}
    with open(ACC / "audit.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def glob_to_re(g):
    out, i = "", 0
    while i < len(g):
        if g[i:i + 3] == "**/":
            out += "(?:.*/)?"; i += 3
        elif g[i:i + 2] == "**":
            out += ".*"; i += 2
        elif g[i] == "*":
            out += "[^/]*"; i += 1
        elif g[i] == "?":
            out += "[^/]"; i += 1
        else:
            out += re.escape(g[i]); i += 1
    return re.compile("^" + out + "$", re.I if os.name == "nt" else 0)


def matches(rel, globs):
    return any(glob_to_re(g).match(rel) for g in globs)


def rel_in_root(fp):
    p = Path(fp)
    if not p.is_absolute():
        p = ROOT / p
    try:
        return Path(os.path.normpath(p)).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return None


def has_content(v):
    if isinstance(v, bool):
        return True
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, tuple)):
        return len(v) > 0 and all(has_content(x) for x in v)
    if isinstance(v, dict):
        return len(v) > 0 and all(has_content(x) for x in v.values())
    return v is not None


def validate_card(card, *, approved=False):
    e = []
    for k in CARD_ITEMS:
        if k not in card:
            e.append(f"項目 {k} がない")
    for k in CARD_ITEMS[:-1]:
        if k in card and not has_content(card[k]):
            e.append(f"項目 {k} に未記入がある（該当なしも理由を書く）")
    if card.get("tier") not in ("lightweight", "normal", "high_risk"):
        e.append("tier は lightweight / normal / high_risk のいずれか")
    elif card["tier"] != "lightweight":
        if not has_content(card.get("modules")) or not has_content(card.get("governance_record")):
            e.append("lightweight 以外は modules（M1〜M6）と governance_record（統治表・レビュー計画の所在）が必要")
    sc = card.get("scope") or {}
    if not has_content(sc.get("allowed_paths")):
        e.append("scope.allowed_paths が空")
    if not isinstance(sc.get("allowed_commands"), list) or not isinstance(sc.get("forbidden_paths"), list):
        e.append("scope.allowed_commands / forbidden_paths は一覧（空でも可）で必要")
    b = card.get("budget") or {}
    try:
        if parse_ts(b.get("deadline_utc")) <= now():
            e.append("budget.deadline_utc が過去")
    except Exception:
        e.append("budget.deadline_utc が ISO 形式でない")
    if not (isinstance(b.get("max_tool_calls"), int) and b["max_tool_calls"] > 0):
        e.append("budget.max_tool_calls は正の整数")
    acc = card.get("acceptance")
    if not (isinstance(acc, list) and acc):
        e.append("acceptance が空")
    else:
        for a in acc:
            if not all(has_content(a.get(x)) for x in ("id", "criterion", "command", "check")) or a.get("check") not in ("self", "independent"):
                e.append(f"acceptance {a.get('id')} が不完全（check は self / independent）")
    if approved:
        if card.get("status") != "READY":
            e.append("status が READY でない")
        if not has_content(card.get("approved_by")) or not has_content(card.get("approved_at")):
            e.append("approved_by / approved_at がない")
    return e


def load_active():
    act = read_json(ACC / "ACTIVE.json")
    if not act:
        return None, None, ["有効な作業カードがない（tools/acc.py activate が必要）"]
    p = APPROVED / f"{act['card_id']}.json"
    if not p.exists():
        return None, p, ["承認済みカードのファイルがない"]
    if sha256_file(p) != act["sha256"]:
        return None, p, ["承認後にカードが改変された（ハッシュ不一致）"]
    card = read_json(p)
    return card, p, validate_card(card, approved=True)
