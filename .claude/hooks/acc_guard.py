#!/usr/bin/env python3
"""旧 ACC-GUARD は 2026-09-29 に人間の判断で撤去した（docs/design/SAFETY_REDESIGN.md）。
settings.json からフック登録は外してある。このファイルは、撤去作業中のセッションが
起動時に取り込んだフック設定を持ち続ける場合に備えた、何もしない暫定スタブである。
次のセッション以降は不要なので削除してよい。"""
import sys

sys.exit(0)
