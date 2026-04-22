"""
暗号資産ニュース 自動取得・要約スクリプト v4
"""

import html
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
# 技術系:
#   アップデート       : ブロックチェーンやDeFiのアップデート、改善提案(EIP/BIP等)、フォーク
#   障害・攻撃         : ネットワーク障害、ハック・攻撃、取引所での流出事例、詐欺
#
# アセット系ビジネス:
#   ステーブルコイン   : USDT/USDC/JPYC/CBDC等の安定価値通貨に関するビジネス
#   NFT                : NFT、デジタルアート、ゲームアイテムに関するビジネス
#   トークン化預金     : Tokenized Deposit、預金トークンに関するビジネス
#   セキュリティトークン : ST、トークン化MMF/株式/国債、セキュリティトークンに関するビジネス
#   暗号資産ETF        : BTC/ETH ETFなど上場投資商品に関するビジネス
#
# その他:
#   ビジネス       : 上記アセット区分に当てはまらない企業・業界のビジネスニュース
#   分析・レポート : 調査レポート、アナリスト分析、統計・データ
#   マーケット     : 価格変動・相場・市場動向（ビジネスや分析を含まない純粋な相場情報）
#   規制・法律     : 各国の規制動向、当局の発表、法整備
#   イベント・人事 : カンファレンス、展示会、人事異動、提携発表
#   その他         : 上記のどれにも当てはまらないニュース
#
CATEGORY_CHOICES = (
    "アップデート"
    " / 障害・攻撃"
    " / ステーブルコイン"
    " / NFT"
    " / トークン化預金"
    " / セキュリティトークン"
    " / 暗号資産ETF"
    " / ビジネス"
    " / 分析・レポート"
    " / マーケット"
    " / 規制・法律"
    " / イベント・人事"
    " / その他"
)

# カテゴリ判定の補助説明（プロンプトに埋め込む）
CATEGORY_GUIDE = """
カテゴリの選び方:
- アップデート: ブロックチェーン・DeFiのプロトコル更新、EIP/BIP提案、ハードフォーク/ソフトフォーク
- 障害・攻撃: ハック被害、資金流出、DDoS、バグによる障害、詐欺・フィッシング
- ステーブルコイン: USDT/USDC/JPYC/CBDC等の発行・運用・規制に関するビジネス
- NFT: NFTの発行・売買・マーケットプレイス・ゲームアイテムに関するビジネス
- トークン化預金: Tokenized Deposit・預金トークンの発行・実証実験・導入
- セキュリティトークン: ST・トークン化MMF/株式/国債/不動産の発行・取引・制度
- 暗号資産ETF: ビットコインETF・イーサリアムETF等の申請・承認・運用
- ビジネス: 上記アセット区分に当てはまらない企業の資金調達・提携・経営・M&A
- 分析・レポート: 調査会社・アナリストのレポート、オンチェーンデータ分析
- マーケット: 価格・相場・出来高など純粋な市場動向（企業ニュースでなく相場情報）
- 規制・法律: 各国当局の規制・法整備・ライセンス・訴訟
- イベント・人事: カンファレンス・展示会・人事異動・コミュニティイベント
- その他: 上記のどれにも当てはまらない場合のみ選択
"""

# ── 取得対象ソース ────────────────────────────────────────────────────────
SOURCES = [
    {"name": "NADA NEWS",        "top_url": "https://www.nadanews.com/",   "rss_url": "https://www.nadanews.com/feed/",       "color": "#0f6e56"},
    {"name": "CoinPost",         "top_url": "https://coinpost.jp/",        "rss_url": "https://coinpost.jp/?feed=rss2",       "color": "#1d4ed8"},
    {"name": "あたらしい経済",   "top_url": "https://www.neweconomy.jp/",  "rss_url": "https://www.neweconomy.jp/feed/",      "color": "#7c3aed"},
    {"name": "CoinTelegraph JP", "top_url": "https://cointelegraph.jp/",   "rss_url": "https://cointelegraph.jp/rss",         "color": "#b45309"},
]


def clean_text(raw: str) -> str:
    """
    RSSのテキストをクリーニングする。
    - HTMLタグ除去
    - HTMLエンティティをデコード（&amp; → & , &#8230; → … 等）
    - 省略記号（…）と [&hellip;] 類似表現を除去
    - 連続空白・改行を正規化
    """
    # HTMLタグ除去
    text = re.sub(r"<[^>]+>", "", raw)
    # HTMLエンティティをデコード（&#8230; → … など）
    text = html.unescape(text)
    # 省略記号や「[…]」「[&hellip;]」「&#8230;」の残渣を除去
    text = re.sub(r"\[…\]|\[&#8230;\]|\[&hellip;\]|\[\.{3}\]", "", text)
    text = re.sub(r"…+", "", text)
    text = re.sub(r"\.{3,}", "", text)
    # 連続空白・改行を正規化
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


# ── RSS 取得 ──────────────────────────────────────────────────────────────
def fetch_rss(source: dict) -> list[dict]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CryptoNewsBot/4.0)"}
    resp = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(source["rss_url"], headers=headers, timeout=30)
            resp.raise_for_status()
            break
        except requests.RequestException as e:
            if attempt == MAX_RETRIES - 1:
                print(f"  ✗ [{source['name']}] RSS取得失敗: {e}")
                return []
            time.sleep(RETRY_DELAY)

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        print(f"  ✗ [{source['name']}] XMLパースエラー: {e}")
        return []

    items = []
    for item in root.findall(".//item"):
        title_raw       = item.findtext("title") or ""
        link            = (item.findtext("link") or "").strip()
        pub_date_str    = (item.findtext("pubDate") or "").strip()
        description_raw = item.findtext("description") or ""
        cats            = [el.text.strip() for el in item.findall("category") if el.text]
        category_raw    = cats[0] if cats else ""

        title       = clean_text(title_raw)
        description = clean_text(description_raw)

        # 要約に十分な文字数を確保（1200字まで）
        description = description[:1200]

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
以下の暗号資産ニュース記事を分析し、JSONのみで返答してください（説明文・コードブロック記号は一切不要）。

【タイトル】
{title}

【本文・リード文】
{description}

{category_guide}

【出力JSON】
{{
  "summary": "ここに記事の要約を150〜200字で書く。何が起きたか・誰が関与しているか・どんな影響があるかを具体的に。省略記号(…)は絶対に使わない。必ず文章を完結させる。",
  "category": "上記カテゴリの選び方を参考に最も適切な1つを選択: {categories}",
  "main_entities": ["記事の主語・主体となっている企業・団体名（1〜3件、個人名は除外）"],
  "related_entities": ["記事中に登場するその他の企業・団体・プロトコル名（最大8件、個人名は除外、main_entitiesと重複不可）"]
}}

【必須ルール】
- summaryは必ず150字以上200字以内で書くこと
- summaryに省略記号（…や...）を使わないこと
- summaryは完結した文章で終わること（途中で切らない）
- categoryは選択肢の中から必ず1つだけ選ぶこと
- JSONのみ返答し、前後に説明文を付けないこと
"""


def analyze_article(
    client: anthropic.Anthropic,
    title: str,
    description: str,
) -> dict:
    """要約・カテゴリ・企業名を一括取得する"""

    # descriptionが短すぎる場合はtitleを補完
    if len(description) < 30:
        content = f"{title}"
    else:
        content = description

    prompt = ANALYSIS_PROMPT.format(
        title=title,
        description=content,
        category_guide=CATEGORY_GUIDE,
        categories=CATEGORY_CHOICES,
    )

    fallback = {
        "summary":          description[:200] if len(description) >= 30 else title,
        "category":         "その他",
        "main_entities":    [],
        "related_entities": [],
    }

    for attempt in range(MAX_RETRIES):
        try:
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=900,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()

            # コードブロック除去
            raw = re.sub(r"^```(?:json)?\s*\n?", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\n?```\s*$", "", raw)
            raw = raw.strip()

            data = json.loads(raw)

            summary = clean_text(str(data.get("summary", ""))).strip()

            # summaryが短すぎる・省略されている場合はdescriptionで代替
            if len(summary) < 50:
                summary = description[:200] if len(description) >= 50 else title

            # 万が一残っている省略記号を除去
            summary = re.sub(r"…+|\.{3,}", "", summary).strip()

            return {
                "summary":          summary,
                "category":         str(data.get("category", "その他")).strip(),
                "main_entities":    [str(e).strip() for e in data.get("main_entities", []) if str(e).strip()],
                "related_entities": [str(e).strip() for e in data.get("related_entities", []) if str(e).strip()],
            }

        except json.JSONDecodeError:
            print(f"    JSONパースエラー（attempt {attempt+1}）")
            if attempt == MAX_RETRIES - 1:
                return fallback
            time.sleep(RETRY_DELAY)

        except anthropic.RateLimitError:
            wait = RETRY_DELAY * (attempt + 2)
            print(f"    レート制限。{wait}秒待機...")
            time.sleep(wait)

        except anthropic.APIStatusError as e:
            print(f"    APIエラー({e.status_code}): {e.message}")
            if attempt == MAX_RETRIES - 1:
                return fallback
            time.sleep(RETRY_DELAY)

        except Exception as e:
            print(f"    エラー: {type(e).__name__}: {e}")
            if attempt == MAX_RETRIES - 1:
                return fallback
            time.sleep(RETRY_DELAY)

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
            print(f"       カテゴリ: {result['category']} | 主体: {result['main_entities']}")
            print(f"       要約({len(result['summary'])}字): {result['summary'][:60]}...")
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
