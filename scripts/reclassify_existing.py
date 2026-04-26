"""
既存DBの企業名・カテゴリを一括再処理するスクリプト v9
APIを使わずキーワードマッチングのみで全記事を更新する。
単発実行用（通常ワークフローには含めない）。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fetch_news import extract_entities, keyword_classify, DATA_FILE


def main():
    if not DATA_FILE.exists():
        print("DBファイルが見つかりません:", DATA_FILE)
        return

    with open(DATA_FILE, encoding="utf-8") as f:
        db = json.load(f)

    articles = db.get("articles", [])
    print(f"全{len(articles)}件を再処理中...\n")

    updated = 0
    for i, a in enumerate(articles, 1):
        title = a.get("title", "")
        desc  = a.get("description", "")

        main_ents, related_ents = extract_entities(title, desc)
        category = keyword_classify(title, desc)

        # summaryが短すぎる場合もdescriptionで補完
        if len(a.get("summary", "")) < 30 and len(desc) >= 30:
            a["summary"] = desc[:400]

        a["main_entities"]    = main_ents
        a["related_entities"] = related_ents
        a["category"]         = category

        if i % 20 == 0 or i == len(articles):
            print(f"  [{i}/{len(articles)}] 処理中...")
        updated += 1

    db["articles"] = articles

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

    print(f"\n完了: {updated}件を再処理しました")

    # サンプル確認
    print("\n--- サンプル（最新3件）---")
    for a in articles[:3]:
        print(f"タイトル: {a['title'][:40]}")
        print(f"  カテゴリ: {a.get('category')}")
        print(f"  主体: {a.get('main_entities')}")
        print(f"  関連: {a.get('related_entities', [])[:4]}")
        print()


if __name__ == "__main__":
    main()
