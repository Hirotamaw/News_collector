"""
暗号資産ニュース 自動取得・要約スクリプト v8

entities取得を独立した関数に分離し、確実に動作するよう修正。
- summarize_article() : summary + category を取得
- extract_entities()  : main_entities + related_entities を取得（シンプルなプロンプト）
- analyze_article()   : 上記2つを呼び出してまとめる
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
    {"name": "NADA NEWS",        "top_url": "https://www.nadanews.com/",  "rss_url": "https://www.nadanews.com/feed/",      "color": "#0f6e56"},
    {"name": "CoinPost",         "top_url": "https://coinpost.jp/",       "rss_url": "https://coinpost.jp/?feed=rss2",      "color": "#1d4ed8"},
    {"name": "あたらしい経済",   "top_url": "https://www.neweconomy.jp/", "rss_url": "https://www.neweconomy.jp/feed/",     "color": "#7c3aed"},
    {"name": "CoinTelegraph JP", "top_url": "https://cointelegraph.jp/",  "rss_url": "https://cointelegraph.jp/rss",        "color": "#b45309"},
]

# ── カテゴリ ──────────────────────────────────────────────────────────────
CATEGORIES = [
    "Blockchain", "DeFi", "障害・攻撃", "分析・レポート",
    "Stablecoin", "NFT", "Tokenized Deposit", "Security Token",
    "暗号資産ETF", "ビジネス", "マーケット", "規制・法律", "イベント・人事",
]
CATEGORY_PROMPT = "\n".join(f"- {c}" for c in CATEGORIES)

KEYWORD_RULES = [
    ("障害・攻撃",       ["ハック","ハッキング","流出","被害","エクスプロイト","exploit","攻撃","詐欺","フィッシング","障害","盗難","不正","凍結","drain","breach","hack","stolen","scam"]),
    ("Blockchain",       ["EIP","BIP","ハードフォーク","ソフトフォーク","アップグレード","イーサリアム改善","プロトコル更新","コンセンサス","バリデータ","merge","upgrade","fork"]),
    ("DeFi",             ["DeFi","defi","分散型金融","Uniswap","Aave","Compound","Curve","カーブ","MakerDAO","流動性","AMM","DEX","レンディング"]),
    ("Stablecoin",       ["ステーブルコイン","stablecoin","USDT","USDC","JPYC","CBDC","デジタル円","安定通貨","Tether"]),
    ("NFT",              ["NFT","nft","デジタルアート","メタバース","OpenSea","Blur"]),
    ("Tokenized Deposit",["トークン化預金","預金トークン","Tokenized Deposit","tokenized deposit","デジタル預金"]),
    ("Security Token",   ["セキュリティトークン","Security Token","STO","RWA","トークン化株式","トークン化国債","トークン化MMF","現実資産"]),
    ("暗号資産ETF",      ["ETF","ビットコインETF","イーサリアムETF","上場投資信託","BlackRock","IBIT"]),
    ("分析・レポート",   ["IMF","BIS","世界銀行","金融庁","FSB","IOSCO","レポート","報告書","声明","オンチェーン"]),
    ("規制・法律",       ["規制","法案","法律","ライセンス","当局","SEC","CFTC","訴訟","逮捕","禁止","regulation","compliance"]),
    ("マーケット",       ["価格","相場","急騰","急落","上昇","下落","高値","安値","ビットコイン価格","ATH"]),
    ("イベント・人事",   ["カンファレンス","イベント","展示会","ハッカソン","人事","退任","就任","conference","summit"]),
    ("ビジネス",         ["提携","資金調達","ラウンド","買収","上場","サービス開始","リリース","パートナー","取引所","ウォレット","決済"]),
]


def keyword_classify(title: str, description: str) -> str:
    text = (title + " " + description).lower()
    for category, keywords in KEYWORD_RULES:
        for kw in keywords:
            if kw.lower() in text:
                return category
    return "ビジネス"


def normalize_category(raw: str) -> str:
    raw = raw.strip()
    if raw in CATEGORIES:
        return raw
    rl = raw.lower()
    for cat in CATEGORIES:
        if cat.lower() == rl or cat.lower() in rl or rl in cat.lower():
            return cat
    return ""


def clean_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw)
    text = html.unescape(text)
    text = re.sub(r"\[…\]|\[&#8230;\]|\[&hellip;\]|\[\.{3}\]|…+|\.{3,}", "", text)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


# ── RSS 取得 ──────────────────────────────────────────────────────────────
def fetch_rss(source: dict) -> list[dict]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CryptoNewsBot/8.0)"}
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
        title        = clean_text(item.findtext("title") or "")
        link         = (item.findtext("link") or "").strip()
        pub_date_str = (item.findtext("pubDate") or "").strip()
        description  = clean_text(item.findtext("description") or "")[:1200]
        cats         = [el.text.strip() for el in item.findall("category") if el.text]

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
            "category_raw": cats[0] if cats else "",
            "source_name":  source["name"],
            "source_url":   source["top_url"],
            "source_color": source["color"],
        })
    return items


def filter_recent(items: list[dict], hours: int = 24) -> list[dict]:
    now    = datetime.now(JST)
    cutoff = now - timedelta(hours=hours)
    return [it for it in items
            if it["pub_date"] and datetime.fromisoformat(it["pub_date"]) >= cutoff]


# ── AI呼び出し①: 要約 + カテゴリ ─────────────────────────────────────────
SUMMARY_PROMPT = """\
以下の暗号資産ニュース記事を読んで、JSONのみを返してください（コードブロック不要）。

タイトル: {title}
本文: {description}

カテゴリ選択肢:
{category_list}

カテゴリ選び方:
- Blockchain: ブロックチェーン本体のアップグレード、EIP/BIP改善提案、フォーク
- DeFi: DeFiプロトコルの更新・ガバナンス提案
- 障害・攻撃: ハック・資金流出・詐欺・ネットワーク障害
- 分析・レポート: IMF・BIS・金融庁等の国際機関レポート・調査会社分析
- Stablecoin: ステーブルコインの発行・運用・採用
- NFT: NFT発行・売買・マーケットプレイス
- Tokenized Deposit: トークン化預金・預金トークン
- Security Token: ST・RWA・トークン化株式/国債/MMF
- 暗号資産ETF: 暗号資産ETFの申請・承認・資金動向
- ビジネス: 企業の資金調達・提携・新サービス・取引所・決済
- マーケット: 価格動向・相場（純粋な市場情報）
- 規制・法律: 各国規制・当局動向・訴訟
- イベント・人事: カンファレンス・人事異動

出力JSON:
{{"summary": "200〜400字で記事の要約。具体的な数値・固有名詞を含め省略記号なしで完結させる", "category": "カテゴリ名をそのまま"}}"""


def summarize_article(client: anthropic.Anthropic, title: str, description: str) -> tuple[str, str]:
    """要約とカテゴリを返す"""
    content = description if len(description) >= 30 else title
    prompt  = SUMMARY_PROMPT.format(
        title=title, description=content, category_list=CATEGORY_PROMPT
    )
    for attempt in range(MAX_RETRIES):
        try:
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}],
            )
            if msg.stop_reason == "max_tokens":
                raise ValueError("max_tokens到達")
            raw  = re.sub(r"^```(?:json)?\s*\n?", "", msg.content[0].text.strip(), flags=re.IGNORECASE)
            raw  = re.sub(r"\n?```\s*$", "", raw).strip()
            data = json.loads(raw)
            summary  = clean_text(str(data.get("summary", ""))).strip()
            summary  = re.sub(r"…+|\.{3,}", "", summary).strip()
            if len(summary) < 50:
                summary = content[:400]
            cat_raw  = str(data.get("category", "")).strip()
            category = normalize_category(cat_raw) or keyword_classify(title, content)
            return summary, category
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                return content[:400], keyword_classify(title, content)
            time.sleep(RETRY_DELAY)
    return content[:400], keyword_classify(title, content)


# ── AI呼び出し②: 企業名抽出（シンプルなプロンプト）─────────────────────
ENTITY_PROMPT = """\
以下のニュース記事に登場する企業名・団体名・プロトコル名を抽出してください。
個人名は含めないでください。JSONのみを返してください（コードブロック不要）。

タイトル: {title}
本文: {description}

出力JSON（必ずこの形式で）:
{{"main_entities": ["ニュースの中心となる主要企業・団体名（1〜3件）"], "related_entities": ["記事内に登場するすべての企業・団体・プロトコル名（main_entitiesと重複しない）"]}}

ルール:
- main_entitiesは記事の主語・主体となっている組織（最重要1〜3件のみ）
- related_entitiesは記事中で言及されたその他すべての企業・団体・プロトコル名
- 個人名（人名）は除外する
- 企業・団体が見つからない場合は空配列 [] にする"""


def extract_entities(client: anthropic.Anthropic, title: str, description: str) -> tuple[list, list]:
    """企業名を抽出してmain/relatedに分けて返す"""
    content = description if len(description) >= 30 else title
    prompt  = ENTITY_PROMPT.format(title=title, description=content)

    for attempt in range(MAX_RETRIES):
        try:
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}],
            )
            raw  = re.sub(r"^```(?:json)?\s*\n?", "", msg.content[0].text.strip(), flags=re.IGNORECASE)
            raw  = re.sub(r"\n?```\s*$", "", raw).strip()
            data = json.loads(raw)

            main_ents    = [str(e).strip() for e in data.get("main_entities",    []) if str(e).strip()]
            related_ents = [str(e).strip() for e in data.get("related_entities", []) if str(e).strip()]

            # main_entsがrelated_entsに混入していたら除外
            related_ents = [e for e in related_ents if e not in main_ents]

            print(f"       entities → main:{main_ents} / related:{related_ents[:4]}")
            return main_ents, related_ents

        except json.JSONDecodeError:
            print(f"    entities JSONエラー（attempt {attempt+1}）: {msg.content[0].text[:100] if 'msg' in dir() else 'N/A'}")
            if attempt == MAX_RETRIES - 1:
                return [], []
            time.sleep(RETRY_DELAY)
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                print(f"    entities エラー: {e}")
                return [], []
            time.sleep(RETRY_DELAY)
    return [], []


# ── まとめて分析 ──────────────────────────────────────────────────────────
def analyze_article(client: anthropic.Anthropic, title: str, description: str) -> dict:
    """要約・カテゴリ・企業名を取得してまとめて返す"""
    summary, category            = summarize_article(client, title, description)
    main_entities, related_entities = extract_entities(client, title, description)
    return {
        "summary":          summary,
        "category":         category,
        "main_entities":    main_entities,
        "related_entities": related_entities,
    }


# ── JSON DB ───────────────────────────────────────────────────────────────
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
            print(f"       → カテゴリ:{result['category']} / summary:{len(result['summary'])}字")
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
