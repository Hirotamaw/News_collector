"""
暗号資産ニュース 自動取得・要約スクリプト
対象サイト:
  - NADA NEWS       https://www.nadanews.com/
  - CoinPost        https://coinpost.jp/
  - あたらしい経済  https://www.neweconomy.jp/
  - CoinTelegraph JP https://cointelegraph.jp/

各記事に以下のメタ情報を付与して data/news.json に蓄積する:
  - source_name  : メディア名
  - source_url   : メディアTOP URL
  - category     : カテゴリ（RSSから取得 or Claude が判定）
  - link         : 記事URL
  - summary      : Claude APIによる日本語要約
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
JST = timezone(timedelta(hours=9))
DATA_FILE = Path(__file__).parent.parent / "data" / "news.json"
MAX_RETRIES = 3
RETRY_DELAY = 5

# ── 取得対象ソース定義 ────────────────────────────────────────────────────
SOURCES = [
    {
        "name": "NADA NEWS",
        "top_url": "https://www.nadanews.com/",
        "rss_url": "https://www.nadanews.com/feed/",
        "color": "#0f6e56",   # UI 表示用カラー
    },
    {
        "name": "CoinPost",
        "top_url": "https://coinpost.jp/",
        "rss_url": "https://coinpost.jp/?feed=rss2",
        "color": "#1d4ed8",
    },
    {
        "name": "あたらしい経済",
        "top_url": "https://www.neweconomy.jp/",
        "rss_url": "https://www.neweconomy.jp/feed/",
        "color": "#7c3aed",
    },
    {
        "name": "CoinTelegraph JP",
        "top_url": "https://cointelegraph.jp/",
        "rss_url": "https://cointelegraph.jp/rss",
        "color": "#b45309",
    },
]

# ── カテゴリ正規化マッピング ──────────────────────────────────────────────
CATEGORY_MAP = {
    # 英語 → 日本語
    "bitcoin": "ビットコイン",
    "ethereum": "イーサリアム",
    "markets": "マーケット",
    "market": "マーケット",
    "business": "ビジネス",
    "technology": "テクノロジー",
    "tech": "テクノロジー",
    "regulation": "規制・法律",
    "defi": "DeFi",
    "nft": "NFT",
    "web3": "Web3",
    "altcoin": "アルトコイン",
    "opinion": "オピニオン",
    "analysis": "分析",
    "education": "学習",
    "press release": "プレスリリース",
    "sponsored": "スポンサード",
    "event": "イベント",
    "interview": "インタビュー",
    # 日本語はそのまま保持
    "ニュース": "ニュース",
    "マーケット": "マーケット",
    "ビジネス": "ビジネス",
    "テクノロジー": "テクノロジー",
    "規制": "規制・法律",
    "分析": "分析",
}


def normalize_category(raw: str) -> str:
    """カテゴリ文字列を正規化する"""
    if not raw:
        return "その他"
    lower = raw.strip().lower()
    for key, val in CATEGORY_MAP.items():
        if key in lower:
            return val
    # 日本語カテゴリはそのまま返す
    return raw.strip() if raw.strip() else "その他"


# ── RSS 取得 ──────────────────────────────────────────────────────────────
def fetch_rss(source: dict) -> list[dict]:
    """RSS フィードを取得してアイテムリストを返す"""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; CryptoNewsBot/2.0; +https://github.com)"
    }
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(source["rss_url"], headers=headers, timeout=30)
            resp.raise_for_status()
            break
        except requests.RequestException as e:
            if attempt == MAX_RETRIES - 1:
                print(f"  ✗ [{source['name']}] RSS 取得失敗: {e}")
                return []
            print(f"  リトライ {attempt+1}/{MAX_RETRIES}...")
            time.sleep(RETRY_DELAY)

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        print(f"  ✗ [{source['name']}] XML パースエラー: {e}")
        return []

    items = []
    for item in root.findall(".//item"):
        title       = (item.findtext("title") or "").strip()
        link        = (item.findtext("link") or "").strip()
        pub_date_str = (item.findtext("pubDate") or "").strip()
        description_raw = (item.findtext("description") or "").strip()

        # 複数カテゴリ収集
        categories = [el.text.strip() for el in item.findall("category") if el.text]
        category_raw = categories[0] if categories else ""

        # HTML タグ除去
        description = re.sub(r"<[^>]+>", "", description_raw).strip()[:600]
        description = re.sub(r"\s+", " ", description).strip()

        # 日付パース
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
            # source メタ情報
            "source_name":  source["name"],
            "source_url":   source["top_url"],
            "source_color": source["color"],
        })

    return items


# ── 期間フィルタ ──────────────────────────────────────────────────────────
def filter_recent(items: list[dict], hours: int = 24) -> list[dict]:
    now = datetime.now(JST)
    cutoff = now - timedelta(hours=hours)
    return [
        item for item in items
        if item["pub_date"] and datetime.fromisoformat(item["pub_date"]) >= cutoff
    ]


# ── Claude API: 要約 + カテゴリ判定 ──────────────────────────────────────
def summarize_and_categorize(
    client: anthropic.Anthropic, title: str, description: str, category_raw: str
) -> tuple[str, str]:
    """
    Returns: (summary, category_ja)
    """
    prompt = f"""以下の暗号資産ニュース記事について、2つの情報を JSON で返してください。

タイトル: {title}
リード文: {description}
元カテゴリ: {category_raw}

返答は必ず以下の JSON のみ（```不要）:
{{
  "summary": "記事の概要を3〜4文・100〜150字で自然な日本語文章で",
  "category": "以下から最も適切な1つを選択: ビットコイン / イーサリアム / アルトコイン / マーケット / DeFi / NFT / Web3 / ビジネス / テクノロジー / 規制・法律 / オピニオン / 分析 / イベント / プレスリリース / 学習 / その他"
}}"""

    for attempt in range(MAX_RETRIES):
        try:
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",  # 速度重視
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            text = message.content[0].text.strip()
            # JSON パース
            text = re.sub(r"^```[^\n]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text)
            data = json.loads(text)
            return data.get("summary", ""), data.get("category", "その他")
        except json.JSONDecodeError:
            # フォールバック: テキストをそのまま summary に
            return text[:200], normalize_category(category_raw)
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                return "要約を生成できませんでした。", normalize_category(category_raw)
            time.sleep(RETRY_DELAY)

    return "", normalize_category(category_raw)


# ── JSON DB 読み書き ──────────────────────────────────────────────────────
def load_db() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {
        "articles": [],
        "last_updated": None,
        "total_count": 0,
        "sources": [s["name"] for s in SOURCES],
    }


def save_db(db: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def merge_articles(db: dict, new_articles: list[dict]) -> int:
    """重複排除（URL基準）して追加。追加件数を返す。"""
    existing = {a["link"] for a in db["articles"]}
    added = 0
    for art in new_articles:
        if art["link"] not in existing:
            db["articles"].append(art)
            existing.add(art["link"])
            added += 1
    # 公開日時 降順ソート
    db["articles"].sort(
        key=lambda a: a.get("pub_date") or "1970-01-01", reverse=True
    )
    return added


# ── メイン ────────────────────────────────────────────────────────────────
def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY が設定されていません")

    client = anthropic.Anthropic(api_key=api_key)
    now_jst = datetime.now(JST)

    print(f"=== 暗号資産ニュース ダイジェスト取得開始 ({now_jst.strftime('%Y-%m-%d %H:%M JST')}) ===\n")

    # ── 全ソースから RSS 取得 ──
    print("[1/4] 全ソースから RSS を取得中...")
    all_items: list[dict] = []
    for source in SOURCES:
        items = fetch_rss(source)
        print(f"  ✓ {source['name']}: {len(items)} 件取得")
        all_items.extend(items)
    print(f"  合計: {len(all_items)} 件\n")

    # ── 期間フィルタ（24時間以内）──
    print("[2/4] 過去24時間以内の記事を抽出中...")
    recent_items = filter_recent(all_items, hours=24)
    print(f"  対象: {len(recent_items)} 件\n")

    if not recent_items:
        print("  対象記事なし。last_updated のみ更新します。")
        db = load_db()
        db["last_updated"] = now_jst.isoformat()
        save_db(db)
        return

    # ── DB 確認・新規チェック ──
    print("[3/4] データベースを確認中...")
    db = load_db()
    existing_links = {a["link"] for a in db["articles"]}
    new_items = [it for it in recent_items if it["link"] not in existing_links]
    skip_count = len(recent_items) - len(new_items)
    print(f"  新規: {len(new_items)} 件 / スキップ（既存）: {skip_count} 件\n")

    # ── 要約・カテゴリ生成 ──
    if new_items:
        print(f"[4/4] Claude API で要約・カテゴリ判定中（{len(new_items)} 件）...")
        for i, item in enumerate(new_items, 1):
            print(f"  [{i:>2}/{len(new_items)}] [{item['source_name']}] {item['title'][:45]}...")
            summary, category = summarize_and_categorize(
                client, item["title"], item["description"], item.get("category_raw", "")
            )
            item["summary"]    = summary
            item["category"]   = category
            item["fetched_at"] = now_jst.isoformat()
            # category_raw は保持（デバッグ用）
            time.sleep(0.3)
        print()
    else:
        print("[4/4] 新規記事なし。要約生成をスキップ。\n")

    # ── DB 保存 ──
    added = merge_articles(db, new_items)
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
