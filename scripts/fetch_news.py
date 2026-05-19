"""
暗号資産ニュース 自動取得・要約スクリプト v14

変更点:
  1. 記事本文の取得: RSSリンク先HTMLから本文を取得してGeminiへ渡す
  2. 要約400字: Gemini APIで400字要約。失敗時はsummary_error=trueフラグ
  3. 企業名2段階: all_entities(全登場企業) → main_entities(中心企業)
  4. 更新モード: mode=today なら当日09:00 JST以降のみ取得
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

JST           = timezone(timedelta(hours=9))
DATA_FILE     = Path(__file__).parent.parent / "docs" / "data" / "news.json"
ENTITIES_FILE = Path(__file__).parent.parent / "docs" / "data" / "entities.json"
MAX_RETRIES   = 3
RETRY_DELAY   = 5
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

SOURCES = [
    {"name": "NADA NEWS",        "top_url": "https://www.nadanews.com/",  "rss_url": "https://www.nadanews.com/feed/",      "color": "#0f6e56"},
    {"name": "CoinPost",         "top_url": "https://coinpost.jp/",       "rss_url": "https://coinpost.jp/?feed=rss2",      "color": "#1d4ed8"},
    {"name": "あたらしい経済",   "top_url": "https://www.neweconomy.jp/", "rss_url": "https://www.neweconomy.jp/feed/",     "color": "#7c3aed"},
    {"name": "CoinTelegraph JP", "top_url": "https://cointelegraph.jp/",  "rss_url": "https://cointelegraph.jp/rss",        "color": "#b45309"},
]

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
    ("Coinbase",             [r"\bCoinbase\b","コインベース"],                    1),
    ("Binance",              [r"\bBinance\b","バイナンス"],                       1),
    ("Kraken",               [r"\bKraken\b","クラーケン"],                        1),
    ("OKX",                  [r"\bOKX\b",r"\bOKEx\b"],                            1),
    ("Bybit",                [r"\bBybit\b","バイビット"],                         1),
    ("bitFlyer",             [r"\bbitFlyer\b","ビットフライヤー"],                 1),
    ("GMOコイン",            ["GMOコイン",r"\bGMO Coin\b"],                       1),
    ("SBI VC Trade",         [r"\bSBI VC\b","SBIVCトレード"],                     1),
    ("楽天ウォレット",       ["楽天ウォレット",r"\bRakuten Wallet\b"],             1),
    ("マネックスグループ",   ["マネックス",r"\bMonex\b"],                         1),
    ("bitbank",              [r"\bbitbank\b","ビットバンク"],                     1),
    ("BitTrade",             [r"\bBitTrade\b","ビットトレード"],                  1),
    ("HTX",                  [r"\bHTX\b",r"\bHuobi\b","フォビ"],                  1),
    ("KuCoin",               [r"\bKuCoin\b","クーコイン"],                        1),
    ("Upbit",                [r"\bUpbit\b","アップビット"],                       1),
    ("MARA Holdings",        [r"\bMARA\b","MARA Holdings"],                       1),
    ("Riot Platforms",       [r"\bRiot Platforms\b","ライオットプラットフォームズ"], 1),
    ("IREN",                 [r"\bIREN\b"],                                        1),
    ("Core Scientific",      [r"\bCore Scientific\b"],                            1),
    ("Ethereum Foundation",  [r"\bEthereum Foundation\b","イーサリアム財団"],     1),
    ("Solana",               [r"\bSolana\b","ソラナ"],                            1),
    ("Polygon",              [r"\bPolygon\b","ポリゴン",r"\bMATIC\b"],             1),
    ("Ripple",               [r"\bRipple\b","リップル",r"\bXRP\b"],               1),
    ("Avalanche",            [r"\bAvalanche\b","アバランチ",r"\bAVAX\b"],         1),
    ("Cardano",              [r"\bCardano\b","カルダノ"],                          2),
    ("Near Protocol",        [r"\bNEAR\b","Near Protocol"],                       2),
    ("Sui",                  [r"\bSui\b","スイ"],                                  1),
    ("Aptos",                [r"\bAptos\b","アプトス"],                            1),
    ("Toncoin",              [r"\bTON\b","Toncoin","トンコイン"],                  2),
    ("Arbitrum",             [r"\bArbitrum\b","アービトラム"],                    2),
    ("Optimism",             [r"\bOptimism\b","オプティミズム"],                  2),
    ("Uniswap",              [r"\bUniswap\b","ユニスワップ"],                     1),
    ("Aave",                 [r"\bAave\b","アーベ"],                              1),
    ("MakerDAO",             [r"\bMakerDAO\b",r"\bMaker\b","Sky Protocol"],       1),
    ("Lido",                 [r"\bLido\b","リド"],                                 1),
    ("Hyperliquid",          [r"\bHyperliquid\b","ハイパーリキッド"],             1),
    ("ThorChain",            [r"\bThorChain\b",r"\bTHORCHAIN\b","ソアチェーン"],  1),
    ("Tether",               [r"\bTether\b","テザー",r"\bUSDT\b"],                1),
    ("Circle",               [r"\bCircle\b","サークル",r"\bUSDC\b"],              1),
    ("JPYC",                 [r"\bJPYC\b"],                                        1),
    ("BlackRock",            [r"\bBlackRock\b","ブラックロック"],                 1),
    ("Fidelity",             [r"\bFidelity\b","フィデリティ"],                    1),
    ("Grayscale",            [r"\bGrayscale\b","グレースケール",r"\bGBTC\b"],     1),
    ("ARK Invest",           [r"\bARK Invest\b","ARKインベスト"],                 2),
    ("VanEck",               [r"\bVanEck\b","バンエック"],                        2),
    ("JPMorgan",             [r"\bJPMorgan\b","JPモルガン","J.P.Morgan"],         1),
    ("Goldman Sachs",        ["Goldman Sachs","ゴールドマン・サックス"],          1),
    ("Morgan Stanley",       ["Morgan Stanley","モルガン・スタンレー"],           1),
    ("PayPal",               [r"\bPayPal\b","ペイパル"],                          1),
    ("三菱UFJ銀行",          ["三菱UFJ","MUFG","三菱UFJ銀行"],                   1),
    ("みずほ銀行",           ["みずほ銀行","Mizuho"],                             1),
    ("三井住友銀行",         ["三井住友","SMBC"],                                  1),
    ("SBI Holdings",         ["SBIホールディングス","SBI Holdings","SBIグループ"], 1),
    ("野村ホールディングス", ["野村ホールディングス","野村証券",r"\bNomura\b"],   2),
    ("MicroStrategy",        [r"\bMicroStrategy\b", r"\bStrategy\b", "マイクロストラテジー"], 1),
    ("Tesla",                [r"\bTesla\b","テスラ"],                             1),
    ("Microsoft",            [r"\bMicrosoft\b","マイクロソフト"],                 2),
    ("Google",               [r"\bGoogle\b",r"\bAlphabet\b","グーグル"],          2),
    ("NVIDIA",               [r"\bNVIDIA\b","エヌビディア"],                      2),
    ("SEC",                  [r"\bSEC\b","証券取引委員会"],                       1),
    ("CFTC",                 [r"\bCFTC\b","商品先物取引委員会"],                  1),
    ("金融庁",               ["金融庁",r"\bFSA\b"],                               1),
    ("財務省",               ["財務省（日本）"],                                  1),
    ("米財務省",             ["米財務省","U.S. Treasury"],                        1),
    ("FRB",                  [r"\bFRB\b","Federal Reserve","連邦準備"],           1),
    ("ECB",                  [r"\bECB\b","欧州中央銀行"],                         1),
    ("IMF",                  [r"\bIMF\b","国際通貨基金"],                         1),
    ("BIS",                  [r"\bBIS\b","国際決済銀行"],                         1),
    ("世界銀行",             ["世界銀行","World Bank"],                           1),
    ("FSB",                  [r"\bFSB\b","金融安定理事会"],                       1),
    ("OpenSea",              [r"\bOpenSea\b","オープンシー"],                     1),
    ("Fireblocks",           [r"\bFireblocks\b","ファイアブロックス"],            2),
    ("Chainalysis",          [r"\bChainalysis\b","チェイナリシス"],               2),
    ("MetaMask",             [r"\bMetaMask\b","メタマスク"],                      2),
    ("Animoca Brands",       ["Animoca","アニモカ"],                              2),
    ("Galaxy Digital",       ["Galaxy Digital","ギャラクシーデジタル"],           2),
    ("Pantera Capital",      ["Pantera Capital","パンテラ"],                      2),
    ("a16z",                 [r"\ba16z\b","Andreessen Horowitz"],                 2),
    ("ConsenSys",            [r"\bConsenSys\b","コンセンシス"],                   2),
    ("B2C2",                 [r"\bB2C2\b"],                                        1),
    ("HIVE Digital",         [r"\bHIVE\b"],                                        1),
    ("Bakkt",                [r"\bBakkt\b","バックト"],                           2),
]


def _make_pattern(keyword: str) -> re.Pattern:
    if keyword.startswith(r"\b") or keyword.startswith("\\b"):
        return re.compile(keyword, re.IGNORECASE)
    return re.compile(re.escape(keyword), re.IGNORECASE)


def clean_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw)
    text = html.unescape(text)
    text = re.sub(r"\[…\]|\[&#8230;\]|\[&hellip;\]|\[\.{3}\]|…+|\.{3,}", "", text)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


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


# ── ① 記事本文の取得 ─────────────────────────────────────────────────────
def fetch_article_body(url: str, timeout: int = 15) -> str:
    """
    記事URLからHTMLを取得し、本文テキストを抽出する。
    失敗した場合は空文字を返す。
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; CryptoNewsBot/14.0)"}
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        body_html = resp.text

        # article / main / .content 等の本文らしいブロックを優先抽出
        # まずarticleタグを探す
        article_match = re.search(
            r'<article[^>]*>(.*?)</article>',
            body_html, re.DOTALL | re.IGNORECASE
        )
        if article_match:
            body_html = article_match.group(1)

        # HTMLタグ除去・クリーニング
        text = clean_text(body_html)
        # 短すぎる場合は失敗とみなす
        return text[:3000] if len(text) > 100 else ""
    except Exception:
        return ""


# ── ② 企業名2段階抽出 ────────────────────────────────────────────────────
def extract_all_entities(title: str, text: str) -> list[str]:
    """
    タイトル+本文から登場するすべての企業名を列挙する（all_entities）。
    単語境界マッチングで誤マッチを防ぐ。
    """
    full = title + " " + text
    found = []
    for display_name, keywords, _ in ENTITY_DICT:
        for kw in keywords:
            if _make_pattern(kw).search(full):
                found.append(display_name)
                break
    return list(dict.fromkeys(found))  # 重複除去・順序保持


def determine_main_entities(title: str, all_ents: list[str]) -> list[str]:
    """
    all_entitiesの中からタイトルに登場する企業をmain_entitiesとする。
    タイトルに企業がなければ、all_entitiesの先頭（重要度順）をmainとする。
    """
    title_ents = []
    for display_name, keywords, _ in ENTITY_DICT:
        for kw in keywords:
            if _make_pattern(kw).search(title) and display_name in all_ents:
                if display_name not in title_ents:
                    title_ents.append(display_name)
                break

    if title_ents:
        return title_ents[:3]

    # タイトルに企業名がない場合: 重要度1の企業を優先
    imp1 = [n for n in all_ents if any(
        d == n and imp == 1 for d, _, imp in ENTITY_DICT
    )]
    return imp1[:1] if imp1 else (all_ents[:1] if all_ents else [])


# ── ③ Gemini API（400字要約 + カテゴリ + 企業名）────────────────────────
GEMINI_PROMPT = """\
以下の暗号資産ニュース記事を分析し、JSONのみを返してください（コードブロック・説明文は不要）。

タイトル: {title}
本文: {body}

カテゴリ選択肢（1つ選びcategoryにそのまま入れること）:
- Blockchain / DeFi / 障害・攻撃 / 分析・レポート / Stablecoin / NFT
- Tokenized Deposit / Security Token / 暗号資産ETF / ビジネス
- マーケット / 規制・法律 / イベント・人事

出力JSON（必ずこの形式のみ）:
{{"summary": "350〜400字で記事の要約。何が起きたか・誰が主体か・数値や固有名詞を含め具体的に。省略記号(…)禁止。完結した日本語文章で書くこと。", "category": "カテゴリ名をそのまま", "all_entities": ["記事中に登場するすべての企業・団体・プロトコル名のリスト（個人名除外、重複なし）"], "main_entities": ["上記all_entitiesの中でタイトルで主役として取り上げられている企業・団体（1〜3件）"]}}"""


def call_gemini(api_key: str, title: str, body: str) -> dict | None:
    """APIキーはヘッダーで送信。エラー時はNoneを返す。"""
    prompt  = GEMINI_PROMPT.format(title=title, body=body[:2500])
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1200, "topP": 0.8}
    }).encode("utf-8")
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}

    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(
                GEMINI_ENDPOINT, data=payload, headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            raw = result["candidates"][0]["content"]["parts"][0]["text"].strip()
            raw = re.sub(r"^```(?:json)?\s*\n?", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\n?```\s*$", "", raw).strip()
            data = json.loads(raw)

            summary = re.sub(r"…+|\.{3,}", "", (data.get("summary") or "")).strip()
            if len(summary) < 80:
                return None

            cat       = normalize_category(str(data.get("category", ""))) or keyword_classify(title, body)
            all_ents  = [str(e).strip() for e in (data.get("all_entities")  or []) if str(e).strip()]
            main_ents = [str(e).strip() for e in (data.get("main_entities") or []) if str(e).strip()]
            # mainはall_entitiesの中にある企業のみ
            main_ents = [e for e in main_ents if e in all_ents][:3]
            if not main_ents and all_ents:
                main_ents = all_ents[:1]

            return {
                "summary":      summary,
                "category":     cat,
                "all_entities": all_ents,
                "main_entities":main_ents,
                "summary_error":False,
            }

        except urllib.error.HTTPError as e:
            print(f"    Gemini HTTP {e.code} (attempt {attempt+1})")
            if e.code in (400, 403):
                return None
            time.sleep(RETRY_DELAY * (attempt + 1))
        except Exception:
            print(f"    Gemini エラー (attempt {attempt+1})")
            if attempt == MAX_RETRIES - 1:
                return None
            time.sleep(RETRY_DELAY)
    return None


def analyze_article(api_key: str | None, title: str, description: str, link: str) -> dict:
    """
    1. 記事本文を取得
    2. Geminiで400字要約 + カテゴリ + 全企業名 + 主要企業名
    3. Gemini失敗 → summary_error=True + キーワードフォールバック
    """
    # 記事本文取得（RSSのdescriptionより長い本文を優先）
    body = fetch_article_body(link) if link else ""
    content = body if len(body) > len(description) else description
    content = content if content else title

    if api_key:
        result = call_gemini(api_key, title, content)
        if result:
            print(f"       [Gemini✓] {result['category']} / {len(result['summary'])}字 / all:{len(result['all_entities'])}社 / main:{result['main_entities']}")
            return result
        # Gemini失敗フラグを立てる
        print(f"       [Gemini✗] API失敗 → キーワードフォールバック")
        summary_error = True
    else:
        summary_error = False

    # キーワードフォールバック
    all_ents  = extract_all_entities(title, content)
    main_ents = determine_main_entities(title, all_ents)
    cat       = keyword_classify(title, content)
    print(f"       [Keyword] {cat} / all:{len(all_ents)}社 / main:{main_ents}")
    return {
        "summary":       content[:400],
        "category":      cat,
        "all_entities":  all_ents,
        "main_entities": main_ents,
        "summary_error": summary_error,
    }


# ── 企業名マスタDB ────────────────────────────────────────────────────────
def load_entities_db() -> dict:
    if ENTITIES_FILE.exists():
        with open(ENTITIES_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"entities": {}, "last_updated": None, "total_count": 0}


def update_entities_db(entities_db: dict, articles: list[dict], now_jst: datetime) -> dict:
    ents = entities_db.get("entities", {})
    for article in articles:
        pub, title, link = article.get("pub_date",""), article.get("title",""), article.get("link","")
        all_e  = article.get("all_entities",  []) or []
        main_e = article.get("main_entities", []) or []
        main_s = set(main_e)
        for name in all_e:
            if name not in ents:
                ents[name] = {"name":name,"article_count":0,"as_main_count":0,
                              "as_related_count":0,"recent_articles":[],"first_seen":pub,"last_seen":pub}
            e = ents[name]
            if link not in [a["link"] for a in e["recent_articles"]]:
                e["article_count"] += 1
                if name in main_s: e["as_main_count"]    += 1
                else:              e["as_related_count"] += 1
                e["last_seen"] = max(e["last_seen"], pub) if e["last_seen"] else pub
                e["recent_articles"].insert(0, {"title":title,"link":link,"pub_date":pub})
                e["recent_articles"] = e["recent_articles"][:5]
    entities_db.update({"entities":ents,"last_updated":now_jst.isoformat(),"total_count":len(ents)})
    return entities_db


def save_entities_db(db: dict) -> None:
    ENTITIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ENTITIES_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


# ── RSS 取得 ──────────────────────────────────────────────────────────────
def fetch_rss(source: dict) -> list[dict]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CryptoNewsBot/14.0)"}
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
            try: pub_date_jst = parsedate_to_datetime(pub_date_str).astimezone(JST)
            except: pass
        if not title or not link: continue
        items.append({
            "title":title,"link":link,
            "pub_date":     pub_date_jst.isoformat() if pub_date_jst else None,
            "pub_date_utc": pub_date_jst.astimezone(timezone.utc).isoformat() if pub_date_jst else None,
            "description":description,"category_raw":cats[0] if cats else "",
            "source_name":source["name"],"source_url":source["top_url"],"source_color":source["color"],
        })
    return items


def filter_articles(items: list[dict], mode: str = "24h") -> list[dict]:
    """
    mode="24h"   : 過去24時間以内
    mode="today" : 当日09:00 JST以降（更新ボタン用）
    """
    now_jst = datetime.now(JST)
    if mode == "today":
        # 当日の09:00 JSTを起点
        cutoff = now_jst.replace(hour=9, minute=0, second=0, microsecond=0)
        if now_jst < cutoff:
            # 09:00前ならば前日09:00から
            cutoff -= timedelta(days=1)
    else:
        cutoff = now_jst - timedelta(hours=24)

    return [it for it in items if it["pub_date"] and datetime.fromisoformat(it["pub_date"]) >= cutoff]


def load_db() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE, encoding="utf-8") as f: return json.load(f)
    return {"articles":[],"last_updated":None,"total_count":0,"sources":[s["name"] for s in SOURCES]}


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
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    # モード: "24h"(デフォルト) or "today"(当日09:00以降)
    mode = os.environ.get("FETCH_MODE", "24h")

    print("✓ Gemini API 有効" if gemini_api_key else "⚠ キーなし → キーワードフォールバック")
    print(f"取得モード: {mode}")

    now_jst = datetime.now(JST)
    print(f"=== 取得開始 ({now_jst.strftime('%Y-%m-%d %H:%M JST')}) ===\n")

    print("[1/5] RSS取得中...")
    all_items: list[dict] = []
    for source in SOURCES:
        items = fetch_rss(source)
        print(f"  ✓ {source['name']}: {len(items)} 件")
        all_items.extend(items)
    print(f"  合計: {len(all_items)} 件\n")

    print(f"[2/5] フィルタ（mode={mode}）...")
    recent = filter_articles(all_items, mode=mode)
    print(f"  対象: {len(recent)} 件\n")

    print("[3/5] DB確認...")
    db             = load_db()
    existing_links = {a["link"] for a in db["articles"]}
    new_items      = [it for it in recent if it["link"] not in existing_links]
    print(f"  新規: {len(new_items)} 件 / スキップ: {len(recent)-len(new_items)} 件\n")

    if new_items:
        print(f"[4/5] 記事分析中（{len(new_items)} 件）...")
        for i, item in enumerate(new_items, 1):
            print(f"  [{i:>2}/{len(new_items)}] {item['title'][:50]}...")
            result = analyze_article(gemini_api_key, item["title"], item["description"], item["link"])
            item.update({
                "summary":       result["summary"],
                "summary_error": result["summary_error"],
                "category":      result["category"],
                "all_entities":  result["all_entities"],
                "main_entities": result["main_entities"],
                "manually_edited": False,
                "fetched_at":    now_jst.isoformat(),
            })
            time.sleep(0.8)
        print()

    added = merge_articles(db, new_items)
    db.update({"last_updated":now_jst.isoformat(),"total_count":len(db["articles"]),
               "sources":[s["name"] for s in SOURCES]})
    save_db(db)

    print("[5/5] 企業名マスタ更新...")
    entities_db = update_entities_db(load_entities_db(), db["articles"], now_jst)
    save_entities_db(entities_db)
    print(f"  企業名マスタ: {entities_db['total_count']} 社\n")
    print(f"=== 完了: 新規{added}件 / 累計{db['total_count']}件 ===")


if __name__ == "__main__":
    main()
