# AI-Control-Center: Claude 用の入口（短く保つこと）

この文書は入口であり、規則の正本ではない。規則を複写して増やさない（標準 4.4 節）。

## 正本（毎回、現在のブランチの版を読む）
1. 人間の最新の指示（全文）
2. `docs/WORKING_RULES.md`（恒久運用規則。優先順位もここに従う）
3. `docs/CURRENT_WORK.md`（今回の目的・受入条件・停止条件）
4. `docs/DAY_RUNNER_EXECUTION_SPEC.md`（Day Runner の唯一の仕様）
5. 有効な作業カード（`work-cards/approved/` の ACTIVE な1件）

## 開始手順
- 有効な作業カードがなければ、変更せず、カード案を `work-cards/drafts/` に書いて止まる。
- 指示要約（目的・対象・禁止・証拠・停止）を作り、原文と突合してから着手する。
- 作業カードの1件だけを扱う。次のカード、次の Day、範囲・費用・権限の拡張は始めない。

## 守ること
- 自分の成功報告を受入証拠にしない。`COMPLETE` を宣言しない。受入は `tools/acc.py` と人間が行う。
- 主張には OBSERVED / REPORTED / INFERRED / HYPOTHESIS / UNKNOWN を付ける。
- 同じ失敗分類で2回修正して再発したら、3回目の小修正をせず、設計又は人間判断へ戻す。
- 新しい権限・費用・破壊的操作が必要なら止めて、判断依頼書（決めること、事実、選択肢、推奨、可逆性）を出す。
- `STOP` を受けたら新規操作をしない。`.acc/STOP` があれば何もしない。
- 拒否（ACC-GUARD BLOCKED）は回避せず、理由を報告する。設定・hook・受入テストは変更しない。
- 独立確認（verification-and-evidenceでcheck: independentを要求する項目）は、`.claude/agents/verifier.md` のverifierサブエージェント、又は人間が行う。実装担当自身の報告・レビューは独立確認として扱わない・表示しない。
- 本プロジェクトは特定の外部AIサービス（ChatGPT等）との連携を前提としない。作業カード・ACC-GUARD・verifierサブエージェントだけで、実装から受入までが完結する設計とする。
