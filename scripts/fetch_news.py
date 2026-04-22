"""
暗号資産ニュース 自動取得・要約スクリプト v6
カテゴリ分類の信頼性を大幅改善:
  1. AIには「カテゴリ名そのもの」を返させる（番号方式廃止）
  2. AI失敗時はキーワードマッチングで強制分類するフォールバックを実装
  3. 「その他」への逃げ道を最小化
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

# ── カテゴリ定義 ──────────────────────────────────────────────────────────
# カテゴリ名は日本語・英語混在で短く明確に
CATEGORIES = [
    "Blockchain",           # ブロックチェーン本体のアップデート・改善提案
    "DeFi",                 # DeFiプロトコルのアップデート・提案
    "障害・攻撃",           # ハック・流出・ネットワーク障害
    "分析・レポート",       # 国際機関・金融機関・調査会社のレポート・声明
    "Stablecoin",           # ステーブルコイン関連ビジネス
    "NFT",                  # NFT・デジタルアート・ゲームアイテム
    "Tokenized Deposit",    # トークン化預金・預金トークン
    "Security Token",       # セキュリティトークン・RWA・トークン化株式/国債/MMF
    "暗号資産ETF",          # 暗号資産ETFの申請・承認・資金動向
    "ビジネス",             # 上記に当てはまらない企業活動全般
    "マーケット",           # 価格動向・相場情報
    "規制・法律",           # 各国規制・当局動向・法整備
    "イベント・人事",       # カンファレンス・展示会・人事異動
]

# AIへ渡すカテゴリリスト（改行区切り）
CATEGORY_PROMPT = "\n".join(f"- {c}" for c in CATEGORIES)

# ── キーワードフォールバック分類器 ──────────────────────────────────────
# (カテゴリ, キーワードリスト) の順で評価。先にマッチしたものを採用。
KEYWORD_RULES = [
    ("障害・攻撃", [
        "ハック", "ハッキング", "流出", "被害", "エクスプロイト", "exploit",
        "攻撃", "詐欺", "フィッシング", "障害", "盗難", "不正", "凍結",
        "drain", "breach", "hack", "stolen", "scam", "vulnerability",
    ]),
    ("Blockchain", [
        "EIP", "BIP", "ハードフォーク", "ソフトフォーク", "アップグレード",
        "イーサリアム改善", "プロトコル更新", "コンセンサス", "バリデータ",
        "merge", "upgrade", "fork", "consensus", "validator",
    ]),
    ("DeFi", [
        "DeFi", "defi", "分散型金融", "Uniswap", "Aave", "Compound",
        "Curve", "カーブ", "MakerDAO", "流動性", "プール", "AMM",
        "DEX", "レンディング", "イールド", "ガバナンス提案",
    ]),
    ("Stablecoin", [
        "ステーブルコイン", "stablecoin", "USDT", "USDC", "JPYC", "CBDC",
        "デジタル円", "円建て", "安定通貨", "USD Coin", "Tether",
        "JPYSC", "EURC", "PYUSD",
    ]),
    ("NFT", [
        "NFT", "nft", "非代替", "デジタルアート", "メタバース",
        "ゲームアイテム", "OpenSea", "Blur", "コレクション",
    ]),
    ("Tokenized Deposit", [
        "トークン化預金", "預金トークン", "Tokenized Deposit",
        "tokenized deposit", "デジタル預金", "銀行間決済", "決済トークン",
    ]),
    ("Security Token", [
        "セキュリティトークン", "Security Token", "STO", "RWA",
        "トークン化株式", "トークン化国債", "トークン化MMF", "現実資産",
        "不動産トークン", "株式トークン", "国債トークン",
        "tokenized bond", "tokenized equity", "tokenized fund",
    ]),
    ("暗号資産ETF", [
        "ETF", "ビットコインETF", "イーサリアムETF", "上場投資信託",
        "BlackRock", "IBIT", "FBTC", "資金流入", "運用残高",
    ]),
    ("分析・レポート", [
        "IMF", "BIS", "世界銀行", "金融庁", "FSB", "IOSCO",
        "レポート", "報告書", "声明", "調査", "分析", "統計",
        "オンチェーン", "データ", "research", "report",
    ]),
    ("規制・法律", [
        "規制", "法案", "法律", "ライセンス", "当局", "SEC", "CFTC",
        "金融庁", "財務省", "訴訟", "逮捕", "摘発", "禁止",
        "regulation", "legal", "compliance", "enforcement",
    ]),
    ("マーケット", [
        "価格", "相場", "急騰", "急落", "上昇", "下落", "高値",
        "安値", "ドル", "円", "BTC", "ETH", "ビットコイン価格",
        "market", "price", "bull", "bear", "ATH",
    ]),
    ("イベント・人事", [
        "カンファレンス", "イベント", "展示会", "ハッカソン",
        "人事", "CEO", "退任", "就任", "開催", "登壇",
        "conference", "summit", "hackathon",
    ]),
    ("ビジネス", [
        "提携", "資金調達", "ラウンド", "買収", "M&A", "上場",
        "サービス開始", "発表", "リリース", "パートナー",
        "取引所", "ウォレット", "決済", "ローン",
    ]),
]


def keyword_classify(title: str, description: str) -> str:
    """タイトルと本文のキーワードからカテゴリを強制判定する"""
    text = (title + " " + description).lower()
    for category, keywords in KEYWORD_RULES:
        for kw in keywords:
            if kw.lower() in text:
                return category
    return "ビジネス"  # 最終フォールバックは「その他」でなく「ビジネス」


def normalize_category(raw: str) -> str:
    """AIの返答を正規カテゴリ名に正規化する"""
    raw = raw.strip()
    # 完全一致
    if raw in CATEGORIES:
        return raw
    # 大小文字無視の完全一致
    raw_lower = raw.lower()
    for cat in CATEGORIES:
        if cat.lower() == raw_lower:
            return cat
    # 部分一致（カテゴリ名がAIの返答に含まれる）
    for cat in CATEGORIES:
        if cat.lower() in raw_lower:
            return cat
    # AIの返答にカテゴリ名が含まれる
    for cat in CATEGORIES:
        if raw_lower in cat.lower():
            return cat
    return ""  # 一致なし → 呼び出し元でキーワード分類に切り替え


# ── テキストクリーニング ──────────────────────────────────────────────────
def clean_text(raw: str) -> str:
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
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CryptoNewsBot/6.0)"}
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
        title       = clean_text(item.findtext("title") or "")
        link        = (item.findtext("link") or "").strip()
        pub_date_str= (item.findtext("pubDate") or "").strip()
        description = clean_text(item.findtext("description") or "")[:1200]
        cats        = [el.text.strip() for el in item.findall("category") if el.text]
        category_raw= cats[0] if cats else ""

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


def filter_recent(items: list[dict], hours: int = 24) -> list[dict]:
    now    = datetime.now(JST)
    cutoff = now - timedelta(hours=hours)
    return [it for it in items
            if it["pub_date"] and datetime.fromisoformat(it["pub_date"]) >= cutoff]


# ── AI分析プロンプト ──────────────────────────────────────────────────────
ANALYSIS_PROMPT = """\
暗号資産ニュース記事を分析し、以下のJSONのみを返してください（コードブロック・説明文は不要）。

【タイトル】
{title}

【本文】
{description}

【カテゴリ選択肢（この中から最も適切な1つを選び、カテゴリ名をそのままcategoryに入れること）】
{category_list}

【選び方のポイント】
- Blockchain: イーサリアム・ビットコイン本体のアップグレード、EIP/BIP改善提案、フォーク
- DeFi: Uniswap・Aave・Curveなどのプロトコル更新・ガバナンス・提案
- 障害・攻撃: ハック・資金流出・エクスプロイト・詐欺・ネットワーク障害
- 分析・レポート: IMF・BIS・金融庁・調査会社のレポート・声明・オンチェーン分析
- Stablecoin: USDT・USDC・CBDC・円建てステーブルコインの発行・運用・採用
- NFT: NFT発行・売買・マーケットプレイス・デジタルアート・ゲーム
- Tokenized Deposit: 銀行のトークン化預金・預金トークンの実証・導入
- Security Token: ST・RWA・トークン化株式/国債/MMF/不動産
- 暗号資産ETF: ビットコインETF・イーサリアムETFの申請・承認・資金動向
- ビジネス: 企業の資金調達・提携・新サービス・取引所・ウォレット・決済
- マーケット: 価格動向・相場・市場センチメント（純粋な相場情報）
- 規制・法律: 各国規制・当局動向・ライセンス・訴訟
- イベント・人事: カンファレンス・展示会・人事異動

【出力JSON】
{{
  "summary": "150〜200字で記事の要約。具体的な数値・企業名・出来事を含め、省略記号(…)を使わず完結した文章で。",
  "category": "上記選択肢のカテゴリ名をそのままコピーして入れること",
  "main_entities": ["記事の主体となる企業・団体名（1〜3件、個人名除外）"],
  "related_entities": ["記事中に登場するその他の企業・団体・プロトコル名（最大8件、個人名除外）"]
}}
"""


def analyze_article(client: anthropic.Anthropic, title: str, description: str) -> dict:
    content = description if len(description) >= 30 else title
    prompt = ANALYSIS_PROMPT.format(
        title=title,
        description=content,
        category_list=CATEGORY_PROMPT,
    )

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

            # サマリー処理
            summary = clean_text(str(data.get("summary", ""))).strip()
            summary = re.sub(r"…+|\.{3,}", "", summary).strip()
            if len(summary) < 50:
                summary = content[:200] if len(content) >= 50 else title

            # カテゴリ処理: AI返答 → 正規化 → 失敗ならキーワード分類
            cat_raw  = str(data.get("category", "")).strip()
            category = normalize_category(cat_raw)
            if not category:
                # AIが正しいカテゴリ名を返せなかった → キーワードで強制分類
                category = keyword_classify(title, content)
                print(f"    ⚠ カテゴリ正規化失敗('{cat_raw}') → キーワード分類: {category}")

            return {
                "summary":          summary,
                "category":         category,
                "main_entities":    [str(e).strip() for e in data.get("main_entities", []) if str(e).strip()],
                "related_entities": [str(e).strip() for e in data.get("related_entities", []) if str(e).strip()],
            }

        except json.JSONDecodeError:
            print(f"    JSONパースエラー（attempt {attempt+1}）")
            if attempt == MAX_RETRIES - 1:
                # JSON失敗 → サマリーなしでキーワード分類だけ実行
                return {
                    "summary":          content[:200] if len(content) >= 50 else title,
                    "category":         keyword_classify(title, content),
                    "main_entities":    [],
                    "related_entities": [],
                }
            time.sleep(RETRY_DELAY)

        except anthropic.RateLimitError:
            wait = RETRY_DELAY * (attempt + 2)
            print(f"    レート制限。{wait}秒待機...")
            time.sleep(wait)

        except anthropic.APIStatusError as e:
            print(f"    APIエラー({e.status_code})")
            if attempt == MAX_RETRIES - 1:
                return {
                    "summary":          content[:200] if len(content) >= 50 else title,
                    "category":         keyword_classify(title, content),
                    "main_entities":    [],
                    "related_entities": [],
                }
            time.sleep(RETRY_DELAY)

        except Exception as e:
            print(f"    エラー: {type(e).__name__}: {e}")
            if attempt == MAX_RETRIES - 1:
                return {
                    "summary":          content[:200] if len(content) >= 50 else title,
                    "category":         keyword_classify(title, content),
                    "main_entities":    [],
                    "related_entities": [],
                }
            time.sleep(RETRY_DELAY)

    return {
        "summary":          content[:200] if len(content) >= 50 else title,
        "category":         keyword_classify(title, content),
        "main_entities":    [],
        "related_entities": [],
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
            print(f"       → {result['category']} | {result['main_entities']}")
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
