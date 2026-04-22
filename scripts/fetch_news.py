"""
暗号資産ニュース 自動取得・要約スクリプト v3
対象サイト:
  - NADA NEWS        https://www.nadanews.com/
  - CoinPost         https://coinpost.jp/
  - あたらしい経済   https://www.neweconomy.jp/
  - CoinTelegraph JP https://cointelegraph.jp/
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
JST       = timezone(timedelta(hours=9))
DATA_FILE = Path(__file__).parent.parent / "docs" / "data" / "news.json"
MAX_RETRIES  = 3
RETRY_DELAY  = 5

# ── カテゴリ定義（新体系）────────────────────────────────────────────────
# AIへの指示に使う。コイン種別カテゴリは廃止し、具体的分類に統一
CATEGORY_LIST = (
    "マーケット"          # 価格変動・相場・市場動向
    " / Proposal"         # アップデート・改善提案・EIP/BIP等
    " / 攻撃・障害"       # ハック・脆弱性・サービス障害・詐欺
    " / 規制・法律"       # 各国規制・法整備・当局動向
    " / DeFi"             # 分散型金融プロトコル
    " / NFT・メタバース"  # NFT・ゲーム・仮想空間
    " / Web3インフラ"     # ブロックチェーン基盤・L1/L2・ブリッジ
    " / ステーブルコイン" # USDT/USDC/CBDC等
    " / ETF・機関投資"    # ETF申請・機関投資家動向・上場企業
    " / 取引所・カストディ" # CEX/DEX運営・資産管理サービス
    " / ウォレット・決済" # ウォレット・決済インフラ
    " / トークン発行・IEO" # ICO/IEO/エアドロップ・新規発行
    " / DAO・ガバナンス"  # 分散自律組織・投票・コミュニティ
    " / AI・Web3融合"     # AI×ブロックチェーン
    " / 政策・産業動向"   # 国家・業界団体・業界全体の動向
    " / イベント・人事"   # カンファレンス・人事・提携
    " / その他"
)

# ── 取得対象ソース ────────────────────────────────────────────────────────
SOURCES = [
    {"name": "NADA NEWS",        "top_url": "https://www.nadanews.com/",    "rss_url": "https://www.nadanews.com/feed/",        "color": "#0f6e56"},
    {"name": "CoinPost",         "top_url": "https://coinpost.jp/",         "rss_url": "https://coinpost.jp/?feed=rss2",        "color": "#1d4ed8"},
    {"name": "あたらしい経済",   "top_url": "https://www.neweconomy.jp/",   "rss_url": "https://www.neweconomy.jp/feed/",       "color": "#7c3aed"},
    {"name": "CoinTelegraph JP", "top_url": "https://cointelegraph.jp/",    "rss_url": "https://cointelegraph.jp/rss",          "color": "#b45309"},
]


# ── RSS 取得 ──────────────────────────────────────────────────────────────
def fetch_rss(source: dict) -> list[dict]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CryptoNewsBot/3.0)"}
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

        description = re.sub(r"<[^>]+>", "", description_raw).strip()
        description = re.sub(r"\s+", " ", description).strip()[:800]

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


# ── Claude API: 要約 + カテゴリ + 企業名抽出 ─────────────────────────────
ANALYSIS_PROMPT = """\
以下の暗号資産ニュース記事を分析し、JSON のみで返答してください（前後の説明・コードブロック不要）。

【タイトル】
{title}

【本文抜粋】
{description}

【出力 JSON 形式】
{{
  "summary": "記事の要点を3〜5文・150〜200字程度の自然な日本語で。価格・数値・固有名詞を含め具体的に書く。",
  "category": "次のカテゴリから最も適切な1つを選択: {categories}",
  "main_entities": ["記事の主語・主体となっている団体・企業名を1〜3件（最重要のみ）"],
  "related_entities": ["記事中に登場するその他の団体・企業・プロトコル名（main_entitiesは除く、最大8件）"]
}}

【注意】
- summary は抽象的にならず、記事の具体的な内容を忠実に反映すること
- category はリストの選択肢から必ず1つだけ選ぶこと
- main_entities は記事の主語になっている組織・企業のみ（個人名は除く）
- related_entities は記事中で言及された組織・企業・プロトコル名（個人名は除く）
"""


def analyze_article(
    client: anthropic.Anthropic,
    title: str,
    description: str,
) -> dict:
    """要約・カテゴリ・企業名を一括で返す"""
    prompt = ANALYSIS_PROMPT.format(
        title=title,
        description=description,
        categories=CATEGORY_LIST,
    )

    for attempt in range(MAX_RETRIES):
        try:
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()
            # コードブロック除去
            raw = re.sub(r"^```[^\n]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw.strip())
            data = json.loads(raw)
            return {
                "summary":          str(data.get("summary", "")).strip(),
                "category":         str(data.get("category", "その他")).strip(),
                "main_entities":    [str(e).strip() for e in data.get("main_entities", []) if e],
                "related_entities": [str(e).strip() for e in data.get("related_entities", []) if e],
            }
        except json.JSONDecodeError:
            # JSONパース失敗 → テキストをそのままsummaryに
            return {
                "summary":          raw[:200] if raw else "要約を生成できませんでした。",
                "category":         "その他",
                "main_entities":    [],
                "related_entities": [],
            }
        except anthropic.APIError as e:
            if attempt == MAX_RETRIES - 1:
                print(f"    API エラー: {e}")
                return {
                    "summary":          "要約を生成できませんでした。",
                    "category":         "その他",
                    "main_entities":    [],
                    "related_entities": [],
                }
            time.sleep(RETRY_DELAY * (attempt + 1))
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                print(f"    予期しないエラー: {e}")
                return {
                    "summary":          "要約を生成できませんでした。",
                    "category":         "その他",
                    "main_entities":    [],
                    "related_entities": [],
                }
            time.sleep(RETRY_DELAY)

    return {
        "summary": "要約を生成できませんでした。",
        "category": "その他",
        "main_entities": [],
        "related_entities": [],
    }


# ── JSON DB 読み書き ──────────────────────────────────────────────────────
def load_db() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"articles": [], "last_updated": None, "total_count": 0,
            "sources": [s["name"] for s in SOURCES]}


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
    db["articles"].sort(key=lambda a: a.get("pub_date") or "1970-01-01", reverse=True)
    return added


# ── メイン ────────────────────────────────────────────────────────────────
def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY が設定されていません")

    client  = anthropic.Anthropic(api_key=api_key)
    now_jst = datetime.now(JST)

    print(f"=== 暗号資産ニュース取得開始 ({now_jst.strftime('%Y-%m-%d %H:%M JST')}) ===\n")

    # 1. RSS 取得
    print("[1/4] RSS フィードを取得中...")
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
        print("  対象記事なし。last_updated のみ更新。")
        db = load_db()
        db["last_updated"] = now_jst.isoformat()
        save_db(db)
        return

    # 3. 重複チェック
    print("[3/4] データベースを確認中...")
    db            = load_db()
    existing_links = {a["link"] for a in db["articles"]}
    new_items     = [it for it in recent if it["link"] not in existing_links]
    print(f"  新規: {len(new_items)} 件 / スキップ: {len(recent)-len(new_items)} 件\n")

    # 4. AI 分析（要約・カテゴリ・企業名）
    if new_items:
        print(f"[4/4] Claude API で分析中（{len(new_items)} 件）...")
        for i, item in enumerate(new_items, 1):
            print(f"  [{i:>2}/{len(new_items)}] [{item['source_name']}] {item['title'][:50]}...")
            result = analyze_article(client, item["title"], item["description"])
            item["summary"]          = result["summary"]
            item["category"]         = result["category"]
            item["main_entities"]    = result["main_entities"]
            item["related_entities"] = result["related_entities"]
            item["fetched_at"]       = now_jst.isoformat()
            time.sleep(0.3)  # レート制限対策
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
