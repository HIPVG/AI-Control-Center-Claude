# Current Work

最終更新: 2026-09-29（JST）
旧版（ChatGPT/Codex・Control Tower 前提の履歴）は `legacy/docs/CURRENT_WORK.codex-era.md` に保管。
以下の記述に旧版の指示・待ち状態・承認は引き継がない。

## 目的

旧 AI-Control-Center の ChatGPT/Codex 前提の設計（Control Tower による GitHub PR 中継レビュー、
Codex CLI ランナー、Codex 前提のオーケストレーション）を取り除き、Claude Code だけで
「作業カード → 実装 → 独立確認 → 受入」が完結する構成へ作り直す。

今回の段階は「機能面のコード移行の完了」までとする。ACC-GUARD（作業カード＋フック）の
本番適用は、この段階の完了後に別段階として扱う（理由は下記「既知の問題」）。

## 現在の到達点

- 政策コード（day_state_machine / scope_guard / git_guard / evidence_registry / backend/models の大半）は現役。
- Control Tower 本体・Codex ランナー・Codex 前提オーケストレーション・依存テストは `legacy/` へ移動済み。
- `backend/control/tasks.py` と `goal_policy.py` は現役コード（faults / next_action / task_discovery）から
  参照されているため `legacy/` から戻した。
- フィールド名から Codex を除去: `needs_codex` → `requires_implementation`、
  `requires_codex` → `requires_implementation`、`codex_review_outcome` / `codex_handoff` →
  `implementer_review_outcome` / `implementer_handoff`。後方互換は持たない。

## 受入条件（この段階）

| ID | 条件 | 確認方法 |
|----|------|----------|
| A1 | `legacy/` 以外に `requires_codex` / `needs_codex` が残っていない | `git grep -n -E "requires_codex\|needs_codex" -- ':!legacy'` が0件 |
| A2 | `backend/` 配下の全モジュールが import できる | `pkgutil.walk_packages` で全件 import し失敗0件 |
| A3 | `config/tasks.yaml` と `config/tasks.example.yaml` が新しいキーで読み込める | `load_task_registry()` で読み込み、各タスクの `requires_implementation` を確認 |
| A4 | pytest（`legacy/` 除く）が全件合格 | `python -m pytest --ignore=legacy -q` |
| A5 | 変更が `redesign` ブランチに push されている（main へは反映しない） | `git ls-remote origin redesign` がローカルの HEAD と一致 |

## ブランチ運用（2026-09-29 人間の指示）

- Claude での作業は `redesign` ブランチだけで進める。
- `main` は ChatGPT 側の実装が進むブランチである。Claude は `main` への push・マージ・PR 作成を行わない。
- `main` の変更を `redesign` に取り込む（merge / rebase）ことも、人間の指示がある場合に限る。

## 停止条件

- 現役コードが `legacy/` のモジュールを必要とすると判明した場合 → 戻すか置き換えるかを人間に確認する（今回の tasks.py の再発防止）。
- 同じ失敗分類で2回修正して再発した場合 → 3回目の小修正をせず、設計又は人間判断へ戻す。
- ACC-GUARD の拒否を受けた場合 → 回避せず理由を報告して止まる。
- `main` への反映又は `main` の取り込み、履歴の書き換え、`.claude/` 設定・フック・受入テストの変更 → 人間の明示的な指示なしには行わない。

## 次の段階（未着手・この段階の範囲外）

1. **ACC-GUARD の巻き込み範囲の検証（バグ2）**: 作業ディレクトリ切替後、作業ルート外として
   無関係なパスへの書き込みまで拒否される事象。Windows ローカルとクラウドセッションの両方で観測
   （REPORTED）。原因は `CLAUDE_PROJECT_DIR` の扱いとマルチディレクトリ操作の組み合わせと推測
   （HYPOTHESIS）。本番のカード運用に入る前に、再現手順と許可範囲を先に確定する。
2. **作業カード運用の前提見直し**: 「人間が別端末でカードを有効化する」前提は単一シェル環境では成立しない。
3. **残存する Codex 期の文書**: `docs/WORKING_RULES.md` ほか `docs/` 配下には旧運用の記述が残る。
   `WORKING_RULES.md` はフックで保護されているため、改訂は人間の判断で行う。
