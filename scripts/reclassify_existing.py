"""
既存DBの記事を一括再分析するスクリプト v12
- manually_edited=true の記事は保護
- Gemini APIが使えれば使用、なければキーワードフォールバック
- entities.json も全記事から再構築
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fetch_news import (
    analyze_article, update_entities_db, save_entities_db,
    load_entities_db, DATA_FILE,
)

JST = timezone(timedelta(hours=9))


def main():
    if not DATA_FILE.exists():
        print("DBファイルが見つかりません:", DATA_FILE)
        return

    # ★ APIキーの値はログに出さない
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    print("✓ Gemini API 有効" if gemini_api_key else "⚠ キーなし → キーワードフォールバック")

    with open(DATA_FILE, encoding="utf-8") as f:
        db = json.load(f)

    articles = db.get("articles", [])

    # 対象: manually_edited=false かつ (entitiesが空 or summaryが200字未満)
    target_indices = [
        i for i, a in enumerate(articles)
        if not a.get("manually_edited")
        and (not a.get("main_entities") or len(a.get("summary", "")) < 200)
    ]

    print(f"再分析対象: {len(target_indices)} 件 / 全{len(articles)} 件\n")

    for n, idx in enumerate(target_indices, 1):
        a     = articles[idx]
        title = a.get("title", "")
        desc  = a.get("description", "")
        print(f"  [{n:>2}/{len(target_indices)}] {title[:48]}...")

        result = analyze_article(gemini_api_key, title, desc)
        articles[idx].update({
            "summary":          result["summary"],
            "category":         result["category"],
            "main_entities":    result["main_entities"],
            "related_entities": result["related_entities"],
        })
        time.sleep(0.5)

    db["articles"] = articles
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    print(f"\n記事DB更新完了: {len(target_indices)}件を再処理\n")

    # entities.json を全記事から再構築
    print("企業名マスタ (entities.json) を再構築中...")
    entities_db = {"entities": {}, "last_updated": None, "total_count": 0}
    now_jst = datetime.now(JST)
    entities_db = update_entities_db(entities_db, articles, now_jst)
    save_entities_db(entities_db)

    top = sorted(entities_db["entities"].values(),
                 key=lambda e: e["article_count"], reverse=True)[:10]
    print(f"企業名マスタ: {entities_db['total_count']} 社\n")
    print("--- 登場回数上位10社 ---")
    for e in top:
        print(f"  {e['name']:<25} 記事:{e['article_count']:>3}件  主体:{e['as_main_count']:>3}回")


if __name__ == "__main__":
    main()
