# チャット承認の引継ぎと確認経路 — 2026-09-28

## 人間指示・信頼境界

記録者: CODEX。判断者: 広瀬剛。受信channel: このCodexチャット。
source class: `RECORDED_DIRECT_CONVERSATION`。原message ID・正確な受信時刻: UNKNOWN。
原文の外部独立認証を主張しない。以下はCodexがこの会話で直接受けた指示の記録である。

| 判断ID | 原文 | 対象・効果 |
| --- | --- | --- |
| AUTH-G6-START-20260928-001 | 「Reviewerからの返信を確認後、G6を開始してください。」 | G5受理確認後のG6開始。G5順序の最初の実装カードWC-01を選択し、成果物bf34b6aを生成した根拠。これは成果物の事後人間受入を意味しない。 |
| AUTH-CHAT-APPROVAL-20260928-001 | 「このチャットで承認が完結する運用のほうが良いよね。」、続く「対応してください。」 | 承認記録・ポリシー・Reviewerプロンプト・Watcher引継ぎを今回の運用へ揃える。既存承認をGitHubやReviewerチャットで再取得しない。 |

G6の固定対象: `bf34b6a4d167cd007be2103c80ca8f0dd93b9531`。
旧依頼: `G6-ACC-WC01-20260928-001`、旧返信: `5864898586`（HUMAN_REQUIRED）。
G5受理: 依頼004の固定commit `4a2b7a2269adae8318903179b10bb59ef424b145`、人間コメント
`5863513310`、Reviewer ACCEPT_COMPLETE `5864471565`。G6開始指示とは別の判断である。

## 実装前の経路確認

現状: Python/UvicornのローカルWatcherは約120秒でPRを取得。G6旧返信を受領・適用し、
台帳にHUMAN_REQUIREDを保持している。旧reportをprocessedにすると同じIDは再実行しない。
Git開始点はbf34b6a。runtime configと履歴の既存差分を保全。

役割: 人間はこのチャットで判断。Codexは原文と対象・範囲を保存して確認報告を配送。
Reviewerは固定記録を照合し判断。Watcherは相関、継続一件制限、結果記録を所有する。
GitHubは共有・監査先であり、人間の再入力を要求しない。

正常経路: 既存のチャット指示→本記録を固定commitで公開→新確認report→相関付きReviewer返信
→Watcherが原報告本文と返信を継続へ渡す→継続成功→旧待機をRESOLVED_BY_CONFIRMATIONへ。
旧reply ID、旧state、解消report/reply/decision IDと時刻を保持し、再起動後も再実行しない。
これは承認引継ぎの処理結果であり、製品受入又はG6全体完了ではない。

失敗経路: 欠落／不一致返信はCONFIRMATION_BLOCKEDとして理由を保持し継続なし。
否定結果、継続失敗、出力欠落、後続配送失敗では旧人間待機を解消しない。
未返信なら既存待機。Git公開又はサービスreload失敗ならその証跡を保持して未適用と表示する。
別commitへの承認流用、曖昧な対象の推定、旧返信の再実行を禁止する。

採用経路はG4 §13の新確認reportであり、同じIDへの二件目SUPERSEDESを自動実行する機能は追加しない。
既存のG5二件目返信は受理証拠として保持し、必要な台帳整合も新確認reportで処理する。

## 変更・自己確認

WORKING_RULES、G4 §12.3/13.3、G5 H05/H09/H10、Reviewer動作プロンプトを一致させた。
Watcherは確認対象と旧返信・判断ID・commitの一致を継続前に検査し、確認成功後だけ旧待機を解消する。
原報告本文を継続に渡し、承認記録を参照できるようにした。台帳ファイルの手編集なし。

実行: `.venv/Scripts/python.exe -m pytest tests/test_reviewer_confirmation.py tests/test_reviewer_bus.py -q -p no:cacheprovider`。
1回目25 passed（2.34秒）。新規10ケースは実JSON保存・再起動を含む。
確認前は旧HUMAN_REQUIRED保持、肯定＋成功で解消、旧返信保持、同じ判断の二重継続なし、
4相関項目それぞれの不一致で継続なし、拒否／失敗で旧待機保持、commit変更の拒否をassertした。
これはfixtureの機械的検査。実Reviewer返信の正しい理解とライブ適用は別途観測する。
Day/Go/モデル実行なし。費用を伴う外部実行なし。
