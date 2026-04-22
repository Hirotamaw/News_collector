"""
暗号資産ニュース 自動取得・要約スクリプト v5
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

# ── 取得対象ソース ────────────────────────────────────────────────────────
SOURCES = [
    {"name": "NADA NEWS",        "top_url": "https://www.nadanews.com/",   "rss_url": "https://www.nadanews.com/feed/",       "color": "#0f6e56"},
    {"name": "CoinPost",         "top_url": "https://coinpost.jp/",        "rss_url": "https://coinpost.jp/?feed=rss2",       "color": "#1d4ed8"},
    {"name": "あたらしい経済",   "top_url": "https://www.neweconomy.jp/",  "rss_url": "https://www.neweconomy.jp/feed/",      "color": "#7c3aed"},
    {"name": "CoinTelegraph JP", "top_url": "https://cointelegraph.jp/",   "rss_url": "https://cointelegraph.jp/rss",         "color": "#b45309"},
]

# ── カテゴリ定義（番号付きリスト形式でAIに渡す）────────────────────────
# AIがこのリストから番号を選択 → カテゴリ名に変換
CATEGORY_LIST = [
    ("1", "Blockchain",          "ビットコイン・イーサリアム等のブロックチェーン本体のアップデート、EIP/BIP等の改善提案、ハードフォーク・ソフトフォーク"),
    ("2", "DeFi",                "Uniswap・Aave・Compound等DeFiプロトコルのアップデート・新機能・ガバナンス提案・TVL動向"),
    ("3", "障害・攻撃",          "ブロックチェーンのネットワーク障害、取引所・DeFiのハッキング・資金流出、フィッシング・詐欺・51%攻撃"),
    ("4", "分析・レポート",      "IMF・世界銀行・金融庁・BIS等の国際組織や金融機関による声明・レポート、調査会社・アナリストの市場分析・統計データ"),
    ("5", "Stablecoin",          "USDT・USDC・JPYC・CBDC等ステーブルコインの発行・運用・規制・採用"),
    ("6", "NFT",                 "NFTの発行・売買・マーケットプレイス・デジタルアート・ゲームアイテム・メタバース"),
    ("7", "Tokenized Deposit",   "トークン化預金・預金トークン（Tokenized Deposit）の発行・実証実験・銀行間決済・導入事例"),
    ("8", "Security Token",      "セキュリティトークン・トークン化株式・トークン化国債・トークン化MMF・不動産トークン化・RWA（現実資産トークン化）"),
    ("9", "暗号資産ETF",         "ビットコインETF・イーサリアムETF等の暗号資産ETFの申請・承認・運用・資金流入"),
    ("10", "ビジネス",           "上記のどのアセット区分にも当てはまらない企業活動（資金調達・M&A・提携・新サービス・取引所運営・ウォレット等）"),
    ("11", "マーケット",         "暗号資産の価格動向・相場分析・出来高・市場センチメント（企業ニュースではなく純粋な相場情報）"),
    ("12", "規制・法律",         "各国の暗号資産規制・法整備・当局の発表・ライセンス申請・訴訟"),
    ("13", "イベント・人事",     "カンファレンス・ハッカソン・展示会・人事異動・コミュニティイベント"),
    ("14", "その他",             "上記のどのカテゴリにも明確に当てはまらない場合のみ"),
]

# AIへ渡す番号付きリスト文字列
CATEGORY_PROMPT_LIST = "\n".join(
    f"{num}. {name}：{desc}" for num, name, desc in CATEGORY_LIST
)

# 番号→カテゴリ名の変換辞書
NUM_TO_CATEGORY = {num: name for num, name, _ in CATEGORY_LIST}
# カテゴリ名→カテゴリ名（正規化用）
NAME_TO_CATEGORY = {name.lower(): name for _, name, _ in CATEGORY_LIST}


def resolve_category(raw: str) -> str:
    """AIの返答（番号またはカテゴリ名）を正規のカテゴリ名に変換する"""
    raw = raw.strip()
    # 番号で返ってきた場合
    if raw in NUM_TO_CATEGORY:
        return NUM_TO_CATEGORY[raw]
    # 「1. Blockchain」「1:Blockchain」のような形式
    m = re.match(r"^(\d+)[.\s:：]", raw)
    if m and m.group(1) in NUM_TO_CATEGORY:
        return NUM_TO_CATEGORY[m.group(1)]
    # カテゴリ名で返ってきた場合（大小文字・スペース無視）
    normalized = raw.lower().strip()
    if normalized in NAME_TO_CATEGORY:
        return NAME_TO_CATEGORY[normalized]
    # 部分一致
    for key, val in NAME_TO_CATEGORY.items():
        if key in normalized or normalized in key:
            return val
    return "その他"


def clean_text(raw: str) -> str:
    """RSSテキストのクリーニング"""
    text = re.sub(r"<[^>]+>", "", raw)
    text = html.unescape(text)
    text = re.sub(r"\[…\]|\[&#8230;\]|\[&hellip;\]|\[\.{3}\]", "", text)
    text = re.sub(r"…+", "", text)
    text = re.sub(r"\.{3,}", "", text)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


# ── RSS 取得 ──────────────────────────────────────────────────────────────
def fetch_rss(source: dict) -> list[dict]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CryptoNewsBot/5.0)"}
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
        description = clean_text(description_raw)[:1200]

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
回答はJSONのみで返してください（説明文・コードブロック記号は一切不要）。

【タイトル】
{title}

【本文・リード文】
{description}

【カテゴリ選択肢】（番号で答えること）
{category_list}

【出力JSON】
{{
  "summary": "150〜200字で記事の要約。何が起きたか・誰が主体か・数値や固有名詞を含めて具体的に。省略記号(…)禁止。文章を完結させること。",
  "category": "上記リストの番号（1〜14のいずれか1つ）",
  "main_entities": ["記事の主語・主体の企業・団体名（1〜3件、個人名除外）"],
  "related_entities": ["記事中に登場するその他の企業・団体・プロトコル名（最大8件、個人名除外）"]
}}

【重要ルール】
- categoryは必ず1〜14の数字1つだけで答えること（カテゴリ名は不要）
- summaryは150字以上200字以内で、省略せず完結した文章で書くこと
- JSONのみ返答すること
"""


def analyze_article(
    client: anthropic.Anthropic,
    title: str,
    description: str,
) -> dict:
    content = description if len(description) >= 30 else title

    prompt = ANALYSIS_PROMPT.format(
        title=title,
        description=content,
        category_list=CATEGORY_PROMPT_LIST,
    )

    fallback = {
        "summary":          content[:200] if len(content) >= 30 else title,
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
            raw = re.sub(r"^```(?:json)?\s*\n?", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\n?```\s*$", "", raw)
            raw = raw.strip()

            data = json.loads(raw)

            summary = clean_text(str(data.get("summary", ""))).strip()
            if len(summary) < 50:
                summary = content[:200] if len(content) >= 50 else title
            summary = re.sub(r"…+|\.{3,}", "", summary).strip()

            category_raw = str(data.get("category", "14")).strip()
            category = resolve_category(category_raw)

            return {
                "summary":          summary,
                "category":         category,
                "main_entities":    [str(e).strip() for e in data.get("main_entities", []) if str(e).strip()],
                "related_entities": [str(e).strip() for e in data.get("related_entities", []) if str(e).strip()],
            }

        except json.JSONDecodeError:
            print(f"    JSONパースエラー（attempt {attempt+1}）raw={raw[:80]}")
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

    print("[1/4] RSSフィードを取得中...")
    all_items: list[dict] = []
    for source in SOURCES:
        items = fetch_rss(source)
        print(f"  ✓ {source['name']}: {len(items)} 件")
        all_items.extend(items)
    print(f"  合計: {len(all_items)} 件\n")

    print("[2/4] 過去24時間以内の記事を抽出中...")
    recent = filter_recent(all_items, hours=24)
    print(f"  対象: {len(recent)} 件\n")

    if not recent:
        print("  対象記事なし。last_updatedのみ更新。")
        db = load_db()
        db["last_updated"] = now_jst.isoformat()
        save_db(db)
        return

    print("[3/4] データベースを確認中...")
    db             = load_db()
    existing_links = {a["link"] for a in db["articles"]}
    new_items      = [it for it in recent if it["link"] not in existing_links]
    print(f"  新規: {len(new_items)} 件 / スキップ: {len(recent)-len(new_items)} 件\n")

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
            print(f"       → カテゴリ: {result['category']} | 主体: {result['main_entities']}")
            time.sleep(0.5)
        print()
    else:
        print("[4/4] 新規記事なし。スキップ。\n")

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
