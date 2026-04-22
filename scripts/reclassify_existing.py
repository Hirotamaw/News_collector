"""
既存DBの再分析スクリプト v7
- main_entities / related_entities が空の記事
- summaryが200字未満の記事
を対象にAI再分析を実行する（単発実行用・通常ワークフローには含めない）
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fetch_news import analyze_article, DATA_FILE

import anthropic


def main():
    if not DATA_FILE.exists():
        print("DBファイルが見つかりません:", DATA_FILE)
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY が設定されていません")
        return

    client = anthropic.Anthropic(api_key=api_key)

    with open(DATA_FILE, encoding="utf-8") as f:
        db = json.load(f)

    articles = db.get("articles", [])

    # 再分析対象: entitiesが空 または summaryが200字未満
    target_indices = [
        i for i, a in enumerate(articles)
        if not a.get("main_entities") or len(a.get("summary", "")) < 200
    ]
    print(f"再分析対象: {len(target_indices)} 件 / 全{len(articles)} 件\n")

    if not target_indices:
        print("再分析対象なし。終了します。")
        return

    updated = 0
    for n, idx in enumerate(target_indices, 1):
        a = articles[idx]
        title = a.get("title", "")
        desc  = a.get("description", "")
        print(f"  [{n:>2}/{len(target_indices)}] {title[:50]}...")
        print(f"       現状: summary={len(a.get('summary',''))}字 / main={a.get('main_entities',[])} ")

        result = analyze_article(client, title, desc)
        articles[idx]["summary"]          = result["summary"]
        articles[idx]["category"]         = result["category"]
        articles[idx]["main_entities"]    = result["main_entities"]
        articles[idx]["related_entities"] = result["related_entities"]

        print(f"       更新後: summary={len(result['summary'])}字 / main={result['main_entities']}")
        updated += 1
        time.sleep(0.5)

    db["articles"] = articles

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

    print(f"\n完了: {updated} 件を再分析しました")


if __name__ == "__main__":
    main()
