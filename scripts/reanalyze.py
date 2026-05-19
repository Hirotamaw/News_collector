"""
既存記事の再分析スクリプト v14
通常の毎日取得とは完全に分離。
手動で実行するか、reanalyze.yml ワークフローから呼び出す。

対象:
  - summary_error=True の記事（Gemini失敗）
  - all_entities が空の記事
  - summaryが200字未満の記事
  - manually_edited=False の記事のみ（手動修正済みは保護）
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fetch_news import analyze_article, update_entities_db, save_entities_db, load_entities_db, DATA_FILE

JST = timezone(timedelta(hours=9))


def main():
    if not DATA_FILE.exists():
        print("DBファイルが見つかりません:", DATA_FILE)
        return

    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    print("✓ Gemini API 有効" if gemini_api_key else "⚠ キーなし → キーワードフォールバック")

    with open(DATA_FILE, encoding="utf-8") as f:
        db = json.load(f)

    articles = db.get("articles", [])

    # 再分析対象の判定
    target_indices = [
        i for i, a in enumerate(articles)
        if not a.get("manually_edited")  # 手動修正済みは除外
        and (
            a.get("summary_error") is True          # Gemini失敗
            or not a.get("all_entities")             # all_entitiesが空
            or len(a.get("summary", "")) < 200       # summaryが短い
        )
    ]

    print(f"再分析対象: {len(target_indices)} 件 / 全{len(articles)} 件\n")

    if not target_indices:
        print("再分析対象なし。entities.jsonのみ再構築します。")
    else:
        for n, idx in enumerate(target_indices, 1):
            a = articles[idx]
            title = a.get("title", "")
            desc  = a.get("description", "")
            link  = a.get("link", "")
            print(f"  [{n:>3}/{len(target_indices)}] {title[:50]}...")

            result = analyze_article(gemini_api_key, title, desc, link)
            articles[idx].update({
                "summary":       result["summary"],
                "summary_error": result["summary_error"],
                "category":      result["category"],
                "all_entities":  result["all_entities"],
                "main_entities": result["main_entities"],
            })
            time.sleep(0.8)

    db["articles"] = articles
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    print(f"\n記事DB更新完了: {len(target_indices)}件を再処理\n")

    # entities.json 再構築
    print("企業名マスタ再構築中...")
    entities_db = {"entities": {}, "last_updated": None, "total_count": 0}
    now_jst = datetime.now(JST)
    entities_db = update_entities_db(entities_db, articles, now_jst)
    save_entities_db(entities_db)

    top = sorted(entities_db["entities"].values(),
                 key=lambda e: e["article_count"], reverse=True)[:10]
    print(f"企業名マスタ: {entities_db['total_count']} 社\n--- 上位10社 ---")
    for e in top:
        print(f"  {e['name']:<25} 記事:{e['article_count']:>3}件  主体:{e['as_main_count']:>3}回")


if __name__ == "__main__":
    main()
