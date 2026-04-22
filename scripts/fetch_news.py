"""
暗号資産ニュース 自動取得・要約スクリプト v3.1
"""

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
import anthropic

# ── 定数 ─────────────────────────────────────────────────────────────────
JST         = timezone(timedelta(hours=9))
DATA_FILE   = Path(__file__).parent.parent / "docs" / "data" / "news.json"
MAX_RETRIES = 3
RETRY_DELAY = 5

# ── カテゴリ定義 ──────────────────────────────────────────────────────────
#
# 【技術系】
#   ブロックチェーン・アップデート : チェーンのアップデート、EIP/BIP等の改善提案、フォーク
#   障害・攻撃                     : ネットワーク障害、ハック、流出、詐欺、セキュリティ事故
#
# 【アセット系】
#   ステーブルコイン       : USDT/USDC/JPYC/CBDC等の安定価値通貨
#   NFT                    : NFT、デジタルアート、ゲームアイテム
#   トークン化預金         : Tokenized Deposit、預金トークン
#   セキュリティトークン   : Security Token、株式/国債/MMFのトークン化
#   暗号資産ETF            : ビットコインETF、イーサリアムETF等の上場商品
#
# 【市場・規制】
#   マーケット   : 価格変動、相場分析、市場動向、トレード
#   規制・法律   : 各国規制、当局動向、法整備
#
# 【その他ビジネス】
#   ビジネス   : 上記に当てはまらない企業活動、提携、資金調達、経営
#   分析・レポート : 調査レポート、アナリスト分析、統計データ
#   イベント・人事 : カンファレンス、展示会、人事異動
#   その他         : 上記のどれにも該当しない記事
#
CATEGORY_CHOICES = (
    "ブロックチェーン・アップデート"
    " / 障害・攻撃"
    " / ステーブルコイン"
    " / NFT"
    " / トークン化預金"
    " / セキュリティトークン"
    " / 暗号資産ETF"
    " / マーケット"
    " / 規制・法律"
    " / ビジネス"
    " / 分析・レポート"
    " / イベント・人事"
    " / その他"
)

# ── 取得対象ソース ────────────────────────────────────────────────────────
SOURCES = [
    {"name": "NADA NEWS",        "top_url": "https://www.nadanews.com/",   "rss_url": "https://www.nadanews.com/feed/",       "color": "#0f6e56"},
    {"name": "CoinPost",         "top_url": "https://coinpost.jp/",        "rss_url": "https://coinpost.jp/?feed=rss2",       "color": "#1d4ed8"},
    {"name": "あたらしい経済",   "top_url": "https://www.neweconomy.jp/",  "rss_url": "https://www.neweconomy.jp/feed/",      "color": "#7c3aed"},
    {"name": "CoinTelegraph JP", "top_url": "https://cointelegraph.jp/",   "rss_url": "https://cointelegraph.jp/rss",         "color": "#b45309"},
]


# ── RSS 取得 ──────────────────────────────────────────────────────────────
def fetch_rss(source: dict) -> list[dict]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CryptoNewsBot/3.1)"}
    resp = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(source["rss_url"], headers=headers, timeout=30)
            resp.raise_for_status()
            break
        except requests.RequestException as e:
            if attempt == MAX_RETRIES - 1:
                print(f"  ✗ [{source['name']}] RSS 取得失敗: {e}")
                return []
            time.sleep(RETRY_DELAY)

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        print(f"  ✗ [{source['name']}] XML パースエラー: {e}")
        return []

    items = []
    for item in root.findall(".//item"):
        title           = (item.findtext("title") or "").strip()
        link            = (item.findtext("link")  or "").strip()
        pub_date_str    = (item.findtext("pubDate") or "").strip()
        description_raw = (item.findtext("description") or "").strip()
        cats            = [el.text.strip() for el in item.findall("category") if el.text]
        category_raw    = cats[0] if cats else ""

        # HTMLタグ除去・空白正規化
        description = re.sub(r"<[^>]+>", "", description_raw).strip()
        description = re.sub(r"\s+", " ", description).strip()
        # 要約に使えるよう十分な文字数を確保（1000字まで）
        description = description[:1000]

        pub_date_jst = None
        if pub_date_str:
            try:
                pub_date_jst = parsedate_to_datetime(pub_date_str).astimezone(JST)
            except Exception:
                pass

        if not title or not link:
            continue

        items.append({
            "title":        title,
            "link":         link,
            "pub_date":     pub_date_jst.isoformat() if pub_date_jst else None,
            "pub_date_utc": pub_date_jst.astimezone(timezone.utc).isoformat() if pub_date_jst else None,
            "description":  description,
            "category_raw": category_raw,
            "source_name":  source["name"],
            "source_url":   source["top_url"],
            "source_color": source["color"],
        })
    return items


# ── 期間フィルタ ──────────────────────────────────────────────────────────
def filter_recent(items: list[dict], hours: int = 24) -> list[dict]:
    now    = datetime.now(JST)
    cutoff = now - timedelta(hours=hours)
    return [
        it for it in items
        if it["pub_date"] and datetime.fromisoformat(it["pub_date"]) >= cutoff
    ]


# ── AI分析プロンプト ──────────────────────────────────────────────────────
ANALYSIS_PROMPT = """\
以下の暗号資産ニュース記事を分析してください。
結果はJSONのみで返答してください（説明文・コードブロック記号は不要）。

【タイトル】
{title}

【本文・リード文】
{description}

【出力フォーマット】
{{
  "summary": "【記事要約】ここに記事の内容を250〜350字程度で詳しく要約してください。何が起きたのか、誰が関与しているか、どのような影響があるか、具体的な数値・固有名詞を含め、この要約だけで記事の内容が把握できるように書いてください。省略せず文章として完結させてください。",
  "category": "次の選択肢から最も適切な1つだけ選んでください: {categories}",
  "main_entities": ["記事の主語・主体となっている企業・団体名を1〜3件（最重要のみ、個人名は除く）"],
  "related_entities": ["記事中に登場するその他の企業・団体・プロトコル名（main_entitiesに含めたものは除く、最大10件、個人名は除く）"]
}}

【重要な注意事項】
- summaryは必ず250字以上で書いてください。途中で終わらせず、完結した文章にしてください
- summaryには具体的な数値（金額、枚数、割合等）や固有名詞を必ず含めてください
- categoryは必ず上記の選択肢の中から1つだけ選んでください
- main_entitiesは記事で主役となっている組織・企業のみ（1〜3件）
- related_entitiesは記事に登場する関連組織（main_entitiesは除外）
"""


def analyze_article(
    client: anthropic.Anthropic,
    title: str,
    description: str,
) -> dict:
    """要約・カテゴリ・企業名を一括で取得する"""

    # descriptionが短すぎる場合はtitleから補完
    content = description if len(description) > 50 else f"{title}。{description}"

    prompt = ANALYSIS_PROMPT.format(
        title=title,
        description=content,
        categories=CATEGORY_CHOICES,
    )

    fallback = {
        "summary":          "",
        "category":         "その他",
        "main_entities":    [],
        "related_entities": [],
    }

    for attempt in range(MAX_RETRIES):
        try:
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=800,   # 要約に十分なトークン数を確保
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()

            # コードブロック除去
            raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\s*```$", "", raw)
            raw = raw.strip()

            data = json.loads(raw)

            summary = str(data.get("summary", "")).strip()
            # summaryが短すぎる場合はdescriptionで補完
            if len(summary) < 50:
                summary = description[:300] if description else title

            return {
                "summary":          summary,
                "category":         str(data.get("category", "その他")).strip(),
                "main_entities":    [str(e).strip() for e in data.get("main_entities", []) if str(e).strip()],
                "related_entities": [str(e).strip() for e in data.get("related_entities", []) if str(e).strip()],
            }

        except json.JSONDecodeError:
            # JSONパース失敗 → raw全体をsummaryとして利用
            print(f"    JSONパースエラー。rawテキストをsummaryに使用。")
            fallback["summary"] = raw[:400] if raw else description[:300]
            return fallback

        except anthropic.RateLimitError:
            wait = RETRY_DELAY * (attempt + 2)
            print(f"    レート制限。{wait}秒待機...")
            time.sleep(wait)

        except anthropic.APIStatusError as e:
            print(f"    API ステータスエラー({e.status_code}): {e.message}")
            if attempt == MAX_RETRIES - 1:
                fallback["summary"] = description[:300] if description else title
                return fallback
            time.sleep(RETRY_DELAY)

        except Exception as e:
            print(f"    予期しないエラー: {type(e).__name__}: {e}")
            if attempt == MAX_RETRIES - 1:
                fallback["summary"] = description[:300] if description else title
                return fallback
            time.sleep(RETRY_DELAY)

    fallback["summary"] = description[:300] if description else title
    return fallback


# ── JSON DB 読み書き ──────────────────────────────────────────────────────
def load_db() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {
        "articles":     [],
        "last_updated": None,
        "total_count":  0,
        "sources":      [s["name"] for s in SOURCES],
    }


def save_db(db: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def merge_articles(db: dict, new_articles: list[dict]) -> int:
    existing = {a["link"] for a in db["articles"]}
    added = 0
    for art in new_articles:
        if art["link"] not in existing:
            db["articles"].append(art)
            existing.add(art["link"])
            added += 1
    db["articles"].sort(
        key=lambda a: a.get("pub_date") or "1970-01-01", reverse=True
    )
    return added


# ── メイン ────────────────────────────────────────────────────────────────
def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY が設定されていません")

    client  = anthropic.Anthropic(api_key=api_key)
    now_jst = datetime.now(JST)

    print(f"=== 暗号資産ニュース取得開始 ({now_jst.strftime('%Y-%m-%d %H:%M JST')}) ===\n")

    # 1. RSS取得
    print("[1/4] RSSフィードを取得中...")
    all_items: list[dict] = []
    for source in SOURCES:
        items = fetch_rss(source)
        print(f"  ✓ {source['name']}: {len(items)} 件")
        all_items.extend(items)
    print(f"  合計: {len(all_items)} 件\n")

    # 2. 24時間フィルタ
    print("[2/4] 過去24時間以内の記事を抽出中...")
    recent = filter_recent(all_items, hours=24)
    print(f"  対象: {len(recent)} 件\n")

    if not recent:
        print("  対象記事なし。last_updatedのみ更新。")
        db = load_db()
        db["last_updated"] = now_jst.isoformat()
        save_db(db)
        return

    # 3. 重複チェック
    print("[3/4] データベースを確認中...")
    db             = load_db()
    existing_links = {a["link"] for a in db["articles"]}
    new_items      = [it for it in recent if it["link"] not in existing_links]
    print(f"  新規: {len(new_items)} 件 / スキップ(既存): {len(recent)-len(new_items)} 件\n")

    # 4. AI分析
    if new_items:
        print(f"[4/4] Claude APIで分析中（{len(new_items)} 件）...")
        for i, item in enumerate(new_items, 1):
            print(f"  [{i:>2}/{len(new_items)}] [{item['source_name']}] {item['title'][:50]}...")
            result = analyze_article(client, item["title"], item["description"])
            item["summary"]          = result["summary"]
            item["category"]         = result["category"]
            item["main_entities"]    = result["main_entities"]
            item["related_entities"] = result["related_entities"]
            item["fetched_at"]       = now_jst.isoformat()
            print(f"       → カテゴリ: {result['category']} / 主体: {result['main_entities']}")
            time.sleep(0.5)
        print()
    else:
        print("[4/4] 新規記事なし。スキップ。\n")

    # 5. 保存
    added              = merge_articles(db, new_items)
    db["last_updated"] = now_jst.isoformat()
    db["total_count"]  = len(db["articles"])
    db["sources"]      = [s["name"] for s in SOURCES]
    save_db(db)

    print(f"=== 完了 ===")
    print(f"  新規追加 : {added} 件")
    print(f"  DB 総件数: {db['total_count']} 件")
    print(f"  保存先   : {DATA_FILE}")


if __name__ == "__main__":
    main()
