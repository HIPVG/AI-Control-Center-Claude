# 新規プロジェクトへの移行メモ（2026-09-29）

## 経緯
既存の `AI-Control-Center`（`C:\AI-Control-Center`）は、ChatGPT/Codex を前提にした
運用が設計に組み込まれていた。
- `Control Tower`: GitHub PR コメント起点で、別リポジトリ
  (`HIPVG/AI-Control-Center-Review-Bridge`) に対してスケジュールタスクが
  レビュー結果を自動投稿する仕組み。認証切れ・タスク停止・ChatGPT 無応答で
  繰り返し破綻していた。
- `docs/WORKING_RULES.md` の ChatGPT レビュー境界（`DECISION_REQUEST` /
  `COMPLETION_REPORT`）: 実装担当（Codex）とレビュー担当（ChatGPT の別インスタンス）
  を分離することで「独立確認」を担保する設計だった。

このうち Control Tower は ChatGPT 運用に強く依存した部品であり、作り直す対象。
役割分離（実装担当／検証担当を分ける）という考え方自体は規約（01文書）の一般原則
であり、Claude だけでも `implementer` / `verifier` サブエージェントで再現できる。

## この scaffold（新規リポジトリの土台）で変えたこと
- `CLAUDE.md`: `docs/WORKING_RULES.md` の ChatGPT 境界への参照を削除。独立確認は
  `verifier` サブエージェント又は人間が行うと明記。外部AIサービス連携を前提にしない
  一文を追加。
- Control Tower・Review-Bridge 相当の仕組みはこの scaffold に含めていない
  （最初から作っていない）。
- `.claude/hooks/acc_guard.py`（ACC-GUARD）、`tools/acc.py`、`work-cards/` の
  仕組み自体はもとから ChatGPT に依存していない。手直しなしでそのまま使える。

## 旧リポジトリから持ってくるかどうかの判断材料（未調査＝UNKNOWN）
以下はまだ中身を見ていないので、移行前に確認したほうがよい。
- `backend/` 等の実装コード: ChatGPT/Codex 固有の制約（コンテキスト長、チャット
  ログ駆動の状態管理など）に構造が引きずられていないか。引きずられていなければ
  そのまま新リポジトリに移植して問題ない。
- `docs/ENGINEERING_WORK_HISTORY.md`、`docs/DEV_PROGRESS.json`: 経緯の記録として
  資料的価値はあるが、新リポジトリの正本にはしない（そのまま複製すると規約4.4節
  「正本を複写して増やさない」に反する）。参考資料として別置きするか、要点だけ
  新しい `docs/CURRENT_WORK.md` に引き継ぐ。
- 01/02 の標準文書自体: 特定AIに依存しない一般原則なので、新リポジトリでもそのまま
  正本として使える。

## 次にやること（人間の判断が必要）
1. 新しい GitHub リポジトリ名を決めて作成する（例: `AI-Control-Center-v2` 等）。
   このセッションからは push 権限のある GitHub 操作ができないため、作成は
   ユーザー側（またはローカルの Claude Code）で行う。
2. この scaffold 一式を新リポジトリの初期コミットとして配置する。
3. 旧リポジトリの `backend/` 等、実際に動いているコードの中身を確認し、
   ChatGPT/Codex 固有の歪みがないか診断してから、移植するかどうかを決める。
4. `docs/CURRENT_WORK.md`（今回の目的・受入条件・停止条件）を新規に書き、
   最初の作業カード（WC-01 相当）から作り直す。
