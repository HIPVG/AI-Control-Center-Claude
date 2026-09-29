# AI-Control-Center: Claude 用の入口（短く保つこと）

この文書は入口であり、規則の正本ではない。規則をここに複写しない。

## 正本（毎回、現在のブランチの版を読む）
1. 人間の最新の指示（全文）
2. `docs/WORKING_RULES.md`（恒久的な運用規則。優先順位もここに従う）
3. `docs/CURRENT_WORK.md`（今回の目的・受入条件・停止条件）

## 要点（詳細は WORKING_RULES）
- 承認・受入・push の承認は、すべてこのチャットで人間が行う。push は承認を得てから Claude が行う。
- 作業は `main` で直接行う。force push・`reset --hard`・`clean`・ブランチ削除はしない。
- 主張には OBSERVED / REPORTED / INFERRED / HYPOTHESIS / UNKNOWN を付ける。「完了」を自分で宣言しない。
