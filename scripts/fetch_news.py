"""
暗号資産ニュース 自動取得・要約スクリプト v12

APIキーセキュリティ強化:
  - キーをURLパラメータでなくHTTPヘッダー(x-goog-api-key)で渡す
    → エラーログ・デバッグ出力にURLが出てもキーが露出しない
  - キーをログに一切出力しない（print/loggingでの露出を防止）
  - キーの存在確認は len() で行い、値自体を表示しない
  - エラーメッセージにURLを含めない
"""

import html
import json
import os
import re
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

# ── 定数 ─────────────────────────────────────────────────────────────────
JST           = timezone(timedelta(hours=9))
DATA_FILE     = Path(__file__).parent.parent / "docs" / "data" / "news.json"
ENTITIES_FILE = Path(__file__).parent.parent / "docs" / "data" / "entities.json"
MAX_RETRIES   = 3
RETRY_DELAY   = 5

# Gemini APIエンドポイント（キーはURLに含めない）
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"

# ── 取得対象ソース ────────────────────────────────────────────────────────
SOURCES = [
    {"name": "NADA NEWS",        "top_url": "https://www.nadanews.com/",  "rss_url": "https://www.nadanews.com/feed/",      "color": "#0f6e56"},
    {"name": "CoinPost",         "top_url": "https://coinpost.jp/",       "rss_url": "https://coinpost.jp/?feed=rss2",      "color": "#1d4ed8"},
    {"name": "あたらしい経済",   "top_url": "https://www.neweconomy.jp/", "rss_url": "https://www.neweconomy.jp/feed/",     "color": "#7c3aed"},
    {"name": "CoinTelegraph JP", "top_url": "https://cointelegraph.jp/",  "rss_url": "https://cointelegraph.jp/rss",        "color": "#b45309"},
]

# ── カテゴリ定義 ──────────────────────────────────────────────────────────
CATEGORIES = [
    "Blockchain", "DeFi", "障害・攻撃", "分析・レポート",
    "Stablecoin", "NFT", "Tokenized Deposit", "Security Token",
    "暗号資産ETF", "ビジネス", "マーケット", "規制・法律", "イベント・人事",
]

KEYWORD_RULES = [
    ("障害・攻撃",       ["ハック","ハッキング","流出","被害","エクスプロイト","exploit","攻撃","詐欺","フィッシング","障害","盗難","不正アクセス","凍結","drain","breach","hack","stolen","scam","vulnerability","脆弱性"]),
    ("Blockchain",       ["EIP","BIP","ハードフォーク","ソフトフォーク","アップグレード","プロトコル更新","コンセンサス","バリデータ","merge","upgrade","fork","consensus","validator","ロールアップ","rollup"]),
    ("DeFi",             ["DeFi","defi","分散型金融","Uniswap","Aave","Compound","Curve","MakerDAO","流動性プール","AMM","DEX","レンディング","TVL"]),
    ("Stablecoin",       ["ステーブルコイン","stablecoin","USDT","USDC","JPYC","CBDC","デジタル円","安定通貨","Tether","EURC","PYUSD","RLUSD"]),
    ("NFT",              ["NFT","nft","デジタルアート","メタバース","OpenSea","Blur","コレクション"]),
    ("Tokenized Deposit",["トークン化預金","預金トークン","Tokenized Deposit","tokenized deposit","デジタル預金"]),
    ("Security Token",   ["セキュリティトークン","Security Token","STO","RWA","トークン化株式","トークン化国債","トークン化MMF","現実資産","株式トークン","国債トークン"]),
    ("暗号資産ETF",      ["ETF","ビットコインETF","イーサリアムETF","上場投資信託","IBIT","FBTC","GBTC","現物ETF","資金流入"]),
    ("分析・レポート",   ["IMF","BIS","世界銀行","金融庁","FSB","IOSCO","FRB","ECB","レポート","報告書","声明","統計","オンチェーン分析"]),
    ("規制・法律",       ["規制","法案","法律","ライセンス","当局","SEC","CFTC","財務省","訴訟","逮捕","摘発","禁止","regulation","compliance","AML","KYC"]),
    ("マーケット",       ["価格","相場","急騰","急落","上昇","下落","高値","安値","ビットコイン価格","ATH","最高値","bull","bear"]),
    ("イベント・人事",   ["カンファレンス","イベント","展示会","ハッカソン","人事","CEO","退任","就任","conference","summit"]),
    ("ビジネス",         ["提携","資金調達","シリーズ","ラウンド","買収","上場","サービス開始","ローンチ","パートナー","取引所","ウォレット","決済","融資"]),
]

ENTITY_DICT = [
    ("Coinbase",             ["Coinbase","コインベース"],                    1),
    ("Binance",              ["Binance","バイナンス"],                       1),
    ("Kraken",               ["Kraken","クラーケン"],                        1),
    ("OKX",                  ["OKX","OKEx"],                                 1),
    ("Bybit",                ["Bybit","バイビット"],                         1),
    ("bitFlyer",             ["bitFlyer","ビットフライヤー"],                 1),
    ("GMOコイン",            ["GMOコイン","GMO Coin"],                       1),
    ("SBI VC Trade",         ["SBI VC","SBIVCトレード"],                     1),
    ("楽天ウォレット",       ["楽天ウォレット","Rakuten Wallet"],             1),
    ("マネックスグループ",   ["マネックス","Monex"],                         1),
    ("Gemini Exchange",      ["Gemini Exchange","Gemini（取引所）"],          1),
    ("HTX",                  ["HTX","Huobi","フォビ"],                       1),
    ("KuCoin",               ["KuCoin","クーコイン"],                        1),
    ("bitbank",              ["bitbank","ビットバンク"],                     1),
    ("Ethereum Foundation",  ["Ethereum Foundation","イーサリアム財団"],     1),
    ("Solana Foundation",    ["Solana","ソラナ"],                            1),
    ("Polygon",              ["Polygon","ポリゴン","MATIC"],                  1),
    ("Ripple",               ["Ripple","リップル","XRP"],                     1),
    ("Avalanche",            ["Avalanche","アバランチ","AVAX"],               1),
    ("Uniswap",              ["Uniswap","ユニスワップ"],                     1),
    ("Aave",                 ["Aave","アーベ"],                              1),
    ("MakerDAO",             ["MakerDAO","Maker","Sky Protocol"],            1),
    ("Lido",                 ["Lido","リド"],                                1),
    ("Tether",               ["Tether","テザー","USDT"],                     1),
    ("Circle",               ["Circle","サークル","USDC"],                   1),
    ("BlackRock",            ["BlackRock","ブラックロック"],                 1),
    ("Fidelity",             ["Fidelity","フィデリティ"],                    1),
    ("Grayscale",            ["Grayscale","グレースケール","GBTC"],          1),
    ("JPMorgan",             ["JPMorgan","JPモルガン","J.P.Morgan"],         1),
    ("Goldman Sachs",        ["Goldman Sachs","ゴールドマン"],               1),
    ("Morgan Stanley",       ["Morgan Stanley","モルガン・スタンレー"],      1),
    ("Visa",                 ["Visa","ビザ"],                                 2),
    ("Mastercard",           ["Mastercard","マスターカード"],                 2),
    ("PayPal",               ["PayPal","ペイパル"],                          1),
    ("三菱UFJ銀行",          ["三菱UFJ","MUFG","三菱UFJ銀行"],              1),
    ("みずほ銀行",           ["みずほ","Mizuho"],                            1),
    ("三井住友銀行",         ["三井住友","SMBC"],                            1),
    ("SBI Holdings",         ["SBIホールディングス","SBI Holdings","SBIグループ"], 1),
    ("野村ホールディングス", ["野村","Nomura"],                              2),
    ("HSBC",                 ["HSBC"],                                       2),
    ("MicroStrategy",        ["MicroStrategy","マイクロストラテジー","Strategy"], 1),
    ("Tesla",                ["Tesla","テスラ"],                             1),
    ("Microsoft",            ["Microsoft","マイクロソフト"],                 2),
    ("Google",               ["Google","Alphabet","グーグル"],               2),
    ("Meta",                 ["Meta","Facebook","メタ"],                     2),
    ("SEC",                  ["SEC","証券取引委員会"],                       1),
    ("CFTC",                 ["CFTC","商品先物取引委員会"],                  1),
    ("金融庁",               ["金融庁","FSA"],                               1),
    ("財務省",               ["財務省"],                                     1),
    ("FRB",                  ["FRB","Federal Reserve","連邦準備"],           1),
    ("ECB",                  ["ECB","欧州中央銀行"],                         1),
    ("IMF",                  ["IMF","国際通貨基金"],                         1),
    ("BIS",                  ["BIS","国際決済銀行"],                         1),
    ("世界銀行",             ["世界銀行","World Bank"],                      1),
    ("FSB",                  ["FSB","金融安定理事会"],                       1),
    ("a16z",                 ["a16z","Andreessen Horowitz"],                 2),
    ("OpenSea",              ["OpenSea","オープンシー"],                     1),
    ("Chainalysis",          ["Chainalysis","チェイナリシス"],               2),
    ("Ledger",               ["Ledger","レジャー"],                          2),
    ("MetaMask",             ["MetaMask","メタマスク"],                      2),
    ("Fireblocks",           ["Fireblocks","ファイアブロックス"],            2),
    ("Animoca Brands",       ["Animoca","アニモカ"],                         2),
    ("Arbitrum",             ["Arbitrum","アービトラム"],                     2),
    ("Optimism",             ["Optimism","オプティミズム"],                   2),
]


# ── テキストクリーニング ──────────────────────────────────────────────────
def clean_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw)
    text = html.unescape(text)
    text = re.sub(r"\[…\]|\[&#8230;\]|\[&hellip;\]|\[\.{3}\]|…+|\.{3,}", "", text)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


# ── フォールバック: キーワードマッチング ─────────────────────────────────
def keyword_classify(title: str, description: str) -> str:
    text = (title + " " + description).lower()
    for category, keywords in KEYWORD_RULES:
        for kw in keywords:
            if kw.lower() in text:
                return category
    return "ビジネス"


def extract_entities_by_keyword(title: str, description: str) -> tuple[list, list]:
    text = title + " " + description
    found: list[tuple[str, int, int]] = []
    for display_name, keywords, importance in ENTITY_DICT:
        count = sum(len(re.findall(re.escape(kw), text, re.IGNORECASE)) for kw in keywords)
        if count > 0:
            found.append((display_name, importance, count))
    if not found:
        return [], []
    title_ents = {
        name for name, kws, _ in ENTITY_DICT
        if any(re.search(re.escape(kw), title, re.IGNORECASE) for kw in kws)
    }
    main_c = [n for n, imp, cnt in found if imp == 1 and (n in title_ents or cnt >= 2)]
    main_s = set(main_c[:3])
    related = [n for n, _, _ in found if n not in main_s]
    if not main_c and found:
        top = sorted(found, key=lambda x: (-x[1], -x[2]))
        main_c  = [top[0][0]]
        related = [n for n, _, _ in top[1:]]
    return list(dict.fromkeys(main_c[:3])), list(dict.fromkeys(related[:10]))


def normalize_category(raw: str) -> str:
    raw = raw.strip()
    if raw in CATEGORIES:
        return raw
    rl = raw.lower()
    for cat in CATEGORIES:
        if cat.lower() == rl or cat.lower() in rl or rl in cat.lower():
            return cat
    return ""


# ── Gemini API 呼び出し ───────────────────────────────────────────────────
# セキュリティ設計:
#   1. APIキーはURLパラメータではなくHTTPヘッダー(x-goog-api-key)で送信
#      → エラーログ・デバッグ出力にURLが出てもキーが露出しない
#   2. キーの値をログに一切print/raiseしない
#   3. エラーメッセージにはURLもキーも含めない（ステータスコードのみ）
#   4. キーの存在確認はbool(api_key)で行い、値自体を参照しない

GEMINI_PROMPT = """\
以下の暗号資産ニュース記事を分析し、JSONのみを返してください（コードブロック・説明文は不要）。

タイトル: {title}
本文: {description}

カテゴリ選択肢（1つ選びcategoryにそのまま入れること）:
- Blockchain / DeFi / 障害・攻撃 / 分析・レポート / Stablecoin / NFT
- Tokenized Deposit / Security Token / 暗号資産ETF / ビジネス
- マーケット / 規制・法律 / イベント・人事

カテゴリ選び方:
- Blockchain: ブロックチェーン本体のアップグレード・EIP/BIP改善提案・フォーク
- DeFi: DeFiプロトコルの更新・ガバナンス提案
- 障害・攻撃: ハック・資金流出・エクスプロイト・詐欺・ネットワーク障害
- 分析・レポート: IMF・BIS・金融庁等の国際機関レポート・市場分析
- Stablecoin: ステーブルコインの発行・運用・採用
- NFT: NFT発行・売買・マーケットプレイス
- Tokenized Deposit: トークン化預金・預金トークン
- Security Token: ST・RWA・トークン化株式/国債/MMF
- 暗号資産ETF: ETFの申請・承認・資金動向
- ビジネス: 企業の資金調達・提携・新サービス等
- マーケット: 価格動向・相場・市場センチメント
- 規制・法律: 各国規制・当局動向・訴訟
- イベント・人事: カンファレンス・展示会・人事異動

出力JSON:
{{"summary": "200〜400字で記事の要約。何が起きたか・誰が主体か・数値や固有名詞を含め具体的に。省略記号(…)禁止。完結した文章で。", "category": "カテゴリ名をそのまま", "main_entities": ["記事の主語・主体となる企業や団体（1〜3件、個人名除外）"], "related_entities": ["記事中に登場するその他の企業・団体・プロトコル名（最大8件、個人名除外）"]}}"""


def call_gemini(api_key: str, title: str, description: str) -> dict | None:
    """
    Gemini APIを呼び出す。
    【セキュリティ】APIキーはx-goog-api-keyヘッダーで送信。URLには含めない。
    """
    content = description if len(description) >= 30 else title
    prompt  = GEMINI_PROMPT.format(title=title, description=content)

    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature":     0.2,
            "maxOutputTokens": 1024,
            "topP":            0.8,
        }
    }).encode("utf-8")

    # ★ キーはヘッダーで渡す（URLに含めない）
    headers = {
        "Content-Type":   "application/json",
        "x-goog-api-key": api_key,       # ← ここがポイント
    }

    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(
                GEMINI_ENDPOINT,   # URLにキーなし
                data=payload,
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            raw = result["candidates"][0]["content"]["parts"][0]["text"].strip()
            raw = re.sub(r"^```(?:json)?\s*\n?", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\n?```\s*$", "", raw).strip()

            data     = json.loads(raw)
            summary  = clean_text(str(data.get("summary", ""))).strip()
            summary  = re.sub(r"…+|\.{3,}", "", summary).strip()
            if len(summary) < 50:
                return None

            cat_raw  = str(data.get("category", "")).strip()
            category = normalize_category(cat_raw) or keyword_classify(title, content)

            main_ents    = [str(e).strip() for e in data.get("main_entities",    []) if str(e).strip()]
            related_ents = [str(e).strip() for e in data.get("related_entities", []) if str(e).strip()]
            related_ents = [e for e in related_ents if e not in main_ents]

            return {
                "summary":          summary,
                "category":         category,
                "main_entities":    main_ents[:3],
                "related_entities": related_ents[:8],
            }

        except urllib.error.HTTPError as e:
            # ★ エラーメッセージにURLやキーを含めない
            status = e.code
            print(f"    Gemini APIエラー: HTTP {status} (attempt {attempt+1})")
            if status in (429, 503):
                time.sleep(RETRY_DELAY * (attempt + 2))
            elif status in (400, 403):
                return None  # キー不正・クォータ超過
            else:
                if attempt == MAX_RETRIES - 1:
                    return None
                time.sleep(RETRY_DELAY)

        except json.JSONDecodeError:
            print(f"    Gemini レスポンスパースエラー (attempt {attempt+1})")
            if attempt == MAX_RETRIES - 1:
                return None
            time.sleep(RETRY_DELAY)

        except Exception:
            # ★ 例外の詳細（スタックトレース等）もキーを含む可能性があるため
            #    例外の型名のみ出力し、メッセージは出力しない
            print(f"    Gemini 予期しないエラー (attempt {attempt+1})")
            if attempt == MAX_RETRIES - 1:
                return None
            time.sleep(RETRY_DELAY)

    return None


def analyze_article(api_key: str | None, title: str, description: str) -> dict:
    """Gemini APIで分析。失敗時はキーワード辞書でフォールバック。"""
    content = description if len(description) >= 30 else title

    if api_key:
        result = call_gemini(api_key, title, description)
        if result:
            print(f"       [Gemini] {result['category']} / {len(result['summary'])}字 / 主体:{result['main_entities']}")
            return result
        print("       [Gemini→Fallback] キーワード分類に切り替え")

    main_ents, related_ents = extract_entities_by_keyword(title, description)
    category = keyword_classify(title, description)
    print(f"       [Keyword] {category} / 主体:{main_ents}")
    return {
        "summary":          content[:400],
        "category":         category,
        "main_entities":    main_ents,
        "related_entities": related_ents,
    }


# ── 企業名マスタDB 更新 ───────────────────────────────────────────────────
def load_entities_db() -> dict:
    if ENTITIES_FILE.exists():
        with open(ENTITIES_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"entities": {}, "last_updated": None, "total_count": 0}


def update_entities_db(entities_db: dict, articles: list[dict], now_jst: datetime) -> dict:
    ents = entities_db.get("entities", {})
    for article in articles:
        pub   = article.get("pub_date", "")
        title = article.get("title", "")
        link  = article.get("link", "")
        pairs = [(n, True)  for n in (article.get("main_entities",    []) or [])] + \
                [(n, False) for n in (article.get("related_entities", []) or [])]
        for name, is_main in pairs:
            if name not in ents:
                ents[name] = {"name": name, "article_count": 0,
                              "as_main_count": 0, "as_related_count": 0,
                              "recent_articles": [], "first_seen": pub, "last_seen": pub}
            e = ents[name]
            if link not in [a["link"] for a in e["recent_articles"]]:
                e["article_count"] += 1
                if is_main: e["as_main_count"]    += 1
                else:       e["as_related_count"] += 1
                e["last_seen"] = max(e["last_seen"], pub) if e["last_seen"] else pub
                e["recent_articles"].insert(0, {"title": title, "link": link, "pub_date": pub})
                e["recent_articles"] = e["recent_articles"][:5]
    entities_db.update({"entities": ents, "last_updated": now_jst.isoformat(), "total_count": len(ents)})
    return entities_db


def save_entities_db(db: dict) -> None:
    ENTITIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ENTITIES_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


# ── RSS 取得 ──────────────────────────────────────────────────────────────
def fetch_rss(source: dict) -> list[dict]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CryptoNewsBot/12.0)"}
    resp = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(source["rss_url"], headers=headers, timeout=30)
            resp.raise_for_status()
            break
        except requests.RequestException:
            if attempt == MAX_RETRIES - 1:
                print(f"  ✗ [{source['name']}] RSS取得失敗")
                return []
            time.sleep(RETRY_DELAY)
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        print(f"  ✗ [{source['name']}] XMLパースエラー")
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
            "title": title, "link": link,
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
    # ★ APIキーの値はログに出さない。存在確認のみ。
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    if gemini_api_key:
        print("✓ Gemini API 有効")
    else:
        print("⚠ GEMINI_API_KEY 未設定 → キーワードフォールバック動作")

    now_jst = datetime.now(JST)
    print(f"=== 取得開始 ({now_jst.strftime('%Y-%m-%d %H:%M JST')}) ===\n")

    print("[1/5] RSS取得中...")
    all_items: list[dict] = []
    for source in SOURCES:
        items = fetch_rss(source)
        print(f"  ✓ {source['name']}: {len(items)} 件")
        all_items.extend(items)
    print(f"  合計: {len(all_items)} 件\n")

    print("[2/5] 24時間フィルタ...")
    recent = filter_recent(all_items, hours=24)
    print(f"  対象: {len(recent)} 件\n")

    print("[3/5] DB確認...")
    db             = load_db()
    existing_links = {a["link"] for a in db["articles"]}
    new_items      = [it for it in recent if it["link"] not in existing_links]
    print(f"  新規: {len(new_items)} 件 / スキップ: {len(recent)-len(new_items)} 件\n")

    if new_items:
        print(f"[4/5] 記事分析中（{len(new_items)} 件）...")
        for i, item in enumerate(new_items, 1):
            print(f"  [{i:>2}/{len(new_items)}] {item['title'][:48]}...")
            result = analyze_article(gemini_api_key, item["title"], item["description"])
            item.update({
                "summary":         result["summary"],
                "category":        result["category"],
                "main_entities":   result["main_entities"],
                "related_entities":result["related_entities"],
                "manually_edited": False,
                "fetched_at":      now_jst.isoformat(),
            })
            time.sleep(0.5)
        print()

    added = merge_articles(db, new_items)
    db.update({"last_updated": now_jst.isoformat(),
               "total_count":  len(db["articles"]),
               "sources":      [s["name"] for s in SOURCES]})
    save_db(db)

    print("[5/5] 企業名マスタ更新...")
    entities_db = load_entities_db()
    entities_db = update_entities_db(entities_db, db["articles"], now_jst)
    save_entities_db(entities_db)
    print(f"  企業名マスタ: {entities_db['total_count']} 社\n")

    print(f"=== 完了: 新規{added}件 / 累計{db['total_count']}件 ===")


if __name__ == "__main__":
    main()
