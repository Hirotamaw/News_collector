"""
既存DBの記事を再分類するスクリプト
「その他」になっている記事をキーワード分類で上書きする
使い方: python scripts/reclassify_existing.py
"""

import json
import os
import sys
import time
from pathlib import Path

# fetch_news.pyと同じディレクトリにある関数を流用
sys.path.insert(0, str(Path(__file__).parent))
from fetch_news import (
    analyze_article,
    keyword_classify,
    DATA_FILE,
    SOURCES,
)

import anthropic


def main():
    if not DATA_FILE.exists():
        print("DBファイルが見つかりません:", DATA_FILE)
        return

    with open(DATA_FILE, encoding="utf-8") as f:
        db = json.load(f)

    articles = db.get("articles", [])
    target = [a for a in articles if a.get("category") in ("その他", "", None)]
    print(f"再分類対象: {len(target)} 件 / 全{len(articles)} 件")

    api_key = os.environ.get("ANTHROPIC_API_KEY")

    # APIキーがあればAI再分類、なければキーワード分類のみ
    client = anthropic.Anthropic(api_key=api_key) if api_key else None

    updated = 0
    for i, article in enumerate(target, 1):
        title = article.get("title", "")
        desc  = article.get("description", "")
        print(f"  [{i:>2}/{len(target)}] {title[:50]}...")

        if client:
            result = analyze_article(client, title, desc)
            article["category"]         = result["category"]
            article["summary"]          = result["summary"] if result["summary"] else article.get("summary", "")
            article["main_entities"]    = result["main_entities"] or article.get("main_entities", [])
            article["related_entities"] = result["related_entities"] or article.get("related_entities", [])
            time.sleep(0.5)
        else:
            article["category"] = keyword_classify(title, desc)

        print(f"       → {article['category']}")
        updated += 1

    db["articles"] = articles
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

    print(f"\n完了: {updated} 件を再分類しました")


if __name__ == "__main__":
    main()
