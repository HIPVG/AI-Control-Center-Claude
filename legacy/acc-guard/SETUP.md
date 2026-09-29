# 導入手順と現状（AI-Control-Center × Claude Code）

## 入れるもの
既存リポジトリのルートへ、この一式をコピーします（既存の `AGENTS.md` / `docs/` は変更しません）。
`CLAUDE.md` は入口のみで、規則の正本は `docs/WORKING_RULES.md` のままです。

## 使い方（1作業カードの流れ）
1. Claude に、`work-cards/TEMPLATE.json` を元にカード案を `work-cards/drafts/` へ書かせる。
2. 人間が内容を読み、`python tools/acc.py activate work-cards/drafts/<id>.json --approve-as <名前>`
3. Claude（implementer）が範囲内で実装する。範囲外・拒否は判断依頼として返る。
4. 人間が `python tools/acc.py verify` → `python tools/acc.py accept --evidence <証拠> --by <名前> --kind self|independent --open-items "…"`
5. 緊急停止は `python tools/acc.py stop`、再開は `resume --by <名前>`。

## 先に必ずやる違反試験
`python tools/selftest_guard.py` （27項目。サンドボックスでは全て合格を確認済み）
その後、実際の Claude Code 上で次を試し、拒否されることを確認してください（D4）。
- カードなしで src を編集させる / `tests/acceptance/` を編集させる / `git push` を頼む
- **サブエージェント（implementer）経由でも同じ拒否になるか**（hook・権限がサブエージェントへ引き継がれない報告があるため、最重要）
- Windows で `python .claude/hooks/acc_guard.py` が実際に起動しているか（`.acc/audit.jsonl` に記録が出る）

## 強制できていないこと（正直な限界）
- `python -m pytest` 等の許可コマンドが内部で何をするかは制限できない（テストコードからの書込み等）。受入テストの改変は verify のハッシュ照合で検知するが、防止ではない。
- 「同一失敗分類で2回修正したら設計へ戻る」は CLAUDE.md の文言のみ（D1）。機械化は未実装。
- 独立確認は、同じ Claude の別サブエージェントでは成立しない。独立が必要な条件は人間、別モデル、CI で行い、`--kind independent` と記録する。
- tier が normal / high_risk のカードは、統治表・レビュー計画の存在を確認するだけ。中身の妥当性は検査しない。
- 標準の対策状態としては、現時点は `IMPLEMENTED`（自己試験は `TESTED` 相当）。`EXERCISED` は実運用後。

## 既存プロジェクトとの関係（人間の判断が必要）
`docs/WORKING_RULES.md` は現在、ChatGPT レビューを blocking 境界としています。Claude 導入でこの境界を置き換えるかは人間が規則を改めて決める事項で、この一式は迂回しません。
