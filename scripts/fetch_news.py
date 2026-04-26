"""
暗号資産ニュース 自動取得・要約スクリプト v9

API残高不要の変更:
  - 企業名抽出をClaude APIから「辞書マッチング」に変更
  - 要約はRSSのdescription（本文リード文）をそのまま使用
  - カテゴリはキーワードマッチングで分類
  → API残高ゼロでもentities・category・summaryが正常に出力される
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

# ── 定数 ─────────────────────────────────────────────────────────────────
JST         = timezone(timedelta(hours=9))
DATA_FILE   = Path(__file__).parent.parent / "docs" / "data" / "news.json"

# ── 取得対象ソース ────────────────────────────────────────────────────────
SOURCES = [
    {"name": "NADA NEWS",        "top_url": "https://www.nadanews.com/",  "rss_url": "https://www.nadanews.com/feed/",      "color": "#0f6e56"},
    {"name": "CoinPost",         "top_url": "https://coinpost.jp/",       "rss_url": "https://coinpost.jp/?feed=rss2",      "color": "#1d4ed8"},
    {"name": "あたらしい経済",   "top_url": "https://www.neweconomy.jp/", "rss_url": "https://www.neweconomy.jp/feed/",     "color": "#7c3aed"},
    {"name": "CoinTelegraph JP", "top_url": "https://cointelegraph.jp/",  "rss_url": "https://cointelegraph.jp/rss",        "color": "#b45309"},
]

# ── カテゴリ定義 ──────────────────────────────────────────────────────────
KEYWORD_RULES = [
    ("障害・攻撃",       ["ハック","ハッキング","流出","被害","エクスプロイト","exploit","攻撃","詐欺","フィッシング","障害","盗難","不正アクセス","凍結","drain","breach","hack","stolen","scam","vulnerability","脆弱性","ラグプル"]),
    ("Blockchain",       ["EIP","BIP","ハードフォーク","ソフトフォーク","アップグレード","プロトコル更新","コンセンサス","バリデータ","merge","upgrade","fork","consensus","validator","シャーディング","レイヤー2","Layer2","ロールアップ","rollup"]),
    ("DeFi",             ["DeFi","defi","分散型金融","Uniswap","Aave","Compound","Curve","カーブ","MakerDAO","流動性プール","AMM","DEX","レンディング","イールド","ガバナンス提案","Liquidity","TVL"]),
    ("Stablecoin",       ["ステーブルコイン","stablecoin","USDT","USDC","JPYC","CBDC","デジタル円","安定通貨","Tether","USD Coin","EURC","PYUSD","RLUSD","預金型","ペッグ"]),
    ("NFT",              ["NFT","nft","デジタルアート","メタバース","OpenSea","Blur","コレクション","トークンアート","ゲームアイテム","デジタル所有権"]),
    ("Tokenized Deposit",["トークン化預金","預金トークン","Tokenized Deposit","tokenized deposit","デジタル預金","決済トークン","銀行トークン"]),
    ("Security Token",   ["セキュリティトークン","Security Token","STO","RWA","トークン化株式","トークン化国債","トークン化MMF","現実資産","不動産トークン","株式トークン","国債トークン","tokenized bond","tokenized equity","tokenized fund","tokenized real"]),
    ("暗号資産ETF",      ["ETF","ビットコインETF","イーサリアムETF","上場投資信託","IBIT","FBTC","GBTC","現物ETF","先物ETF","資金流入","運用残高","純資産"]),
    ("分析・レポート",   ["IMF","BIS","世界銀行","金融庁","FSB","IOSCO","FRB","ECB","レポート","報告書","調査報告","声明","統計","オンチェーン分析","市場調査","research","report"]),
    ("規制・法律",       ["規制","法案","法律","ライセンス","当局","SEC","CFTC","金融庁","財務省","訴訟","逮捕","摘発","禁止","regulation","legal","compliance","enforcement","AML","KYC","制裁"]),
    ("マーケット",       ["価格","相場","急騰","急落","上昇","下落","高値","安値","ビットコイン価格","ATH","最高値","最安値","強気","弱気","bull","bear","market cap"]),
    ("イベント・人事",   ["カンファレンス","イベント","展示会","ハッカソン","人事","CEO","CTO","CFO","退任","就任","開催","登壇","conference","summit","hackathon","Consensus","DevCon"]),
    ("ビジネス",         ["提携","資金調達","シリーズ","ラウンド","買収","M&A","上場","サービス開始","ローンチ","リリース","パートナー","取引所","ウォレット","決済","融資","出資","投資"]),
]


def keyword_classify(title: str, description: str) -> str:
    """キーワードマッチングでカテゴリを判定する"""
    text = (title + " " + description).lower()
    for category, keywords in KEYWORD_RULES:
        for kw in keywords:
            if kw.lower() in text:
                return category
    return "ビジネス"


# ── 企業名辞書（既知企業・団体・プロトコル名） ────────────────────────────
# タプル: (表示名, [マッチキーワード], 重要度)
# 重要度1=主要企業候補, 重要度2=関連企業
ENTITY_DICT = [
    # 取引所・カストディ
    ("Coinbase",          ["Coinbase","コインベース"],                    1),
    ("Binance",           ["Binance","バイナンス"],                       1),
    ("Kraken",            ["Kraken","クラーケン"],                        1),
    ("OKX",               ["OKX","OKEx"],                                 1),
    ("Bybit",             ["Bybit","バイビット"],                         1),
    ("bitFlyer",          ["bitFlyer","ビットフライヤー"],                 1),
    ("GMOコイン",         ["GMOコイン","GMO Coin"],                       1),
    ("SBI VC Trade",      ["SBI VC","SBIVCトレード","SBI VC Trade"],       1),
    ("楽天ウォレット",    ["楽天ウォレット","Rakuten Wallet"],             1),
    ("マネックスグループ",["マネックス","Monex"],                         1),
    ("Gemini",            ["Gemini","ジェミナイ"],                        1),
    ("Crypto.com",        ["Crypto.com","クリプトドットコム"],             1),
    ("HTX",               ["HTX","Huobi","フォビ"],                       1),
    ("KuCoin",            ["KuCoin","クーコイン"],                        1),
    ("Gate.io",           ["Gate.io","ゲートアイオー"],                   1),
    ("bitbank",           ["bitbank","ビットバンク"],                     1),
    ("Liquid",            ["Liquid","リキッド"],                          2),
    ("Bitstamp",          ["Bitstamp","ビットスタンプ"],                  2),
    ("Upbit",             ["Upbit","アップビット"],                       2),

    # ブロックチェーン・プロトコル
    ("Ethereum Foundation",["Ethereum Foundation","イーサリアム財団"],     1),
    ("Bitcoin Core",      ["Bitcoin Core"],                               1),
    ("Solana Foundation", ["Solana","ソラナ"],                            1),
    ("Polygon",           ["Polygon","ポリゴン","MATIC"],                  1),
    ("Avalanche",         ["Avalanche","アバランチ","AVAX"],               1),
    ("Cardano",           ["Cardano","カルダノ","ADA"],                    1),
    ("Polkadot",          ["Polkadot","ポルカドット","DOT"],               1),
    ("Ripple",            ["Ripple","リップル","XRP"],                     1),
    ("Chainlink",         ["Chainlink","チェーンリンク","LINK"],           2),
    ("Cosmos",            ["Cosmos","コスモス","ATOM"],                    2),
    ("Near Protocol",     ["NEAR","Near Protocol"],                       2),
    ("Sui",               ["Sui","スイ"],                                  2),
    ("Aptos",             ["Aptos","アプトス"],                            2),
    ("Toncoin",           ["TON","Toncoin","トンコイン"],                  2),
    ("Base",              ["Base","ベース（Coinbase）"],                   2),
    ("Arbitrum",          ["Arbitrum","アービトラム"],                     2),
    ("Optimism",          ["Optimism","オプティミズム"],                   2),

    # DeFiプロトコル
    ("Uniswap",           ["Uniswap","ユニスワップ"],                     1),
    ("Aave",              ["Aave","アーベ"],                              1),
    ("Compound",          ["Compound","コンパウンド"],                    2),
    ("MakerDAO",          ["MakerDAO","メイカーDAO","Maker","Sky Protocol"],1),
    ("Curve Finance",     ["Curve","カーブ"],                             2),
    ("Lido",              ["Lido","リド"],                                1),
    ("EigenLayer",        ["EigenLayer","アイゲンレイヤー"],              2),

    # ステーブルコイン発行体
    ("Tether",            ["Tether","テザー","USDT"],                     1),
    ("Circle",            ["Circle","サークル","USDC"],                   1),
    ("日本円ステーブルコイン",["JPYC","円建てステーブル"],               2),

    # ETF・資産運用
    ("BlackRock",         ["BlackRock","ブラックロック"],                 1),
    ("Fidelity",          ["Fidelity","フィデリティ"],                    1),
    ("Grayscale",         ["Grayscale","グレースケール","GBTC"],          1),
    ("Franklin Templeton",["Franklin Templeton","フランクリン"],          2),
    ("ARK Invest",        ["ARK Invest","ARK","アーク"],                  2),
    ("VanEck",            ["VanEck","バンエック"],                        2),

    # 銀行・金融機関
    ("JPMorgan",          ["JPMorgan","JPモルガン","J.P.Morgan"],         1),
    ("Goldman Sachs",     ["Goldman Sachs","ゴールドマン"],               1),
    ("Morgan Stanley",    ["Morgan Stanley","モルガン・スタンレー"],      1),
    ("Visa",              ["Visa","ビザ"],                                 2),
    ("Mastercard",        ["Mastercard","マスターカード"],                 2),
    ("PayPal",            ["PayPal","ペイパル"],                          1),
    ("三菱UFJ銀行",       ["三菱UFJ","MUFG","三菱UFJ銀行"],              1),
    ("みずほ銀行",        ["みずほ","Mizuho"],                            1),
    ("三井住友銀行",      ["三井住友","SMBC"],                            1),
    ("SBI Holdings",      ["SBIホールディングス","SBI Holdings","SBI"],   1),
    ("野村ホールディングス",["野村","Nomura"],                            2),
    ("HSBC",              ["HSBC","エイチエスビーシー"],                  2),
    ("Standard Chartered",["Standard Chartered","スタンダードチャータード"],2),
    ("Deutsche Bank",     ["Deutsche Bank","ドイツ銀行"],                 2),

    # テック企業
    ("MicroStrategy",     ["MicroStrategy","マイクロストラテジー","Strategy"],1),
    ("Tesla",             ["Tesla","テスラ"],                             1),
    ("Microsoft",         ["Microsoft","マイクロソフト"],                 2),
    ("Google",            ["Google","Alphabet","グーグル"],               2),
    ("Amazon",            ["Amazon","アマゾン","AWS"],                    2),
    ("Meta",              ["Meta","Facebook","メタ"],                     2),
    ("Apple",             ["Apple","アップル"],                           2),

    # 規制当局・政府機関
    ("SEC",               ["SEC","証券取引委員会"],                       1),
    ("CFTC",              ["CFTC","商品先物取引委員会"],                  1),
    ("金融庁",            ["金融庁","FSA"],                               1),
    ("財務省",            ["財務省"],                                     1),
    ("FRB",               ["FRB","Federal Reserve","連邦準備"],           1),
    ("ECB",               ["ECB","欧州中央銀行"],                         1),
    ("IMF",               ["IMF","国際通貨基金"],                         1),
    ("BIS",               ["BIS","国際決済銀行"],                         1),
    ("世界銀行",          ["世界銀行","World Bank"],                      1),
    ("FSB",               ["FSB","金融安定理事会"],                       1),
    ("財務省（米国）",    ["U.S. Treasury","米財務省"],                   1),
    ("ホワイトハウス",    ["White House","ホワイトハウス"],               1),

    # VC・投資
    ("a16z",              ["a16z","Andreessen Horowitz","アンドリーセン"], 2),
    ("Paradigm",          ["Paradigm","パラダイム"],                      2),
    ("Sequoia",           ["Sequoia","セコイア"],                         2),
    ("Pantera Capital",   ["Pantera","パンテラ"],                         2),

    # NFT・ゲーム
    ("OpenSea",           ["OpenSea","オープンシー"],                     1),
    ("Blur",              ["Blur","ブラー"],                              2),
    ("Axie Infinity",     ["Axie Infinity","アクシー"],                   2),
    ("The Sandbox",       ["Sandbox","サンドボックス"],                   2),
    ("Decentraland",      ["Decentraland","ディセントラランド"],           2),

    # その他主要企業
    ("Animoca Brands",    ["Animoca","アニモカ"],                         2),
    ("Fireblocks",        ["Fireblocks","ファイアブロックス"],            2),
    ("Chainalysis",       ["Chainalysis","チェイナリシス"],               2),
    ("Ledger",            ["Ledger","レジャー"],                          2),
    ("Trezor",            ["Trezor","トレザー"],                          2),
    ("Metamask",          ["MetaMask","メタマスク"],                      2),
    ("Alchemy",           ["Alchemy","アルケミー"],                       2),
    ("Infura",            ["Infura","インフラ"],                          2),
    ("Lightning Network", ["Lightning Network","ライトニング"],            2),
]


def extract_entities(title: str, description: str) -> tuple[list[str], list[str]]:
    """
    企業名辞書でタイトルと本文をスキャンして企業名を抽出。
    重要度1かつ複数回登場 → main_entities
    それ以外 → related_entities
    """
    text = title + " " + description

    found: list[tuple[str, int, int]] = []  # (表示名, 重要度, 登場回数)

    for display_name, keywords, importance in ENTITY_DICT:
        count = 0
        for kw in keywords:
            # 大小文字無視でカウント
            count += len(re.findall(re.escape(kw), text, re.IGNORECASE))
        if count > 0:
            found.append((display_name, importance, count))

    if not found:
        return [], []

    # タイトルに登場する企業を優先してmainに
    title_entities = set()
    for display_name, keywords, importance in ENTITY_DICT:
        for kw in keywords:
            if re.search(re.escape(kw), title, re.IGNORECASE):
                title_entities.add(display_name)

    # main: タイトルに登場 かつ 重要度1、または本文で2回以上登場の重要度1
    main_candidates = [
        name for name, imp, cnt in found
        if imp == 1 and (name in title_entities or cnt >= 2)
    ]

    # related: main以外のすべて（登場したもの）
    main_set = set(main_candidates[:3])  # mainは最大3件
    related = [
        name for name, imp, cnt in found
        if name not in main_set
    ]

    # mainが空でfoundがある場合、重要度1の先頭をmainに
    if not main_candidates and found:
        top = sorted(found, key=lambda x: (-x[1], -x[2]))
        main_candidates = [top[0][0]]
        related = [name for name, _, _ in top[1:]]

    return list(dict.fromkeys(main_candidates[:3])), list(dict.fromkeys(related[:10]))


# ── テキストクリーニング ──────────────────────────────────────────────────
def clean_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw)
    text = html.unescape(text)
    text = re.sub(r"\[…\]|\[&#8230;\]|\[&hellip;\]|\[\.{3}\]|…+|\.{3,}", "", text)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


# ── RSS 取得 ──────────────────────────────────────────────────────────────
def fetch_rss(source: dict) -> list[dict]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CryptoNewsBot/9.0)"}
    resp = None
    for attempt in range(3):
        try:
            resp = requests.get(source["rss_url"], headers=headers, timeout=30)
            resp.raise_for_status()
            break
        except requests.RequestException as e:
            if attempt == 2:
                print(f"  ✗ [{source['name']}] RSS取得失敗: {e}")
                return []
            time.sleep(5)
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
        print(f"[4/4] キーワード分析中（{len(new_items)} 件）...")
        for i, item in enumerate(new_items, 1):
            title = item["title"]
            desc  = item["description"]

            # カテゴリ（キーワードマッチング）
            category = keyword_classify(title, desc)

            # 要約（descriptionをそのまま使用、最大400字）
            summary = desc[:400] if len(desc) >= 30 else title

            # 企業名抽出（辞書マッチング・APIなし）
            main_ents, related_ents = extract_entities(title, desc)

            item["summary"]          = summary
            item["category"]         = category
            item["main_entities"]    = main_ents
            item["related_entities"] = related_ents
            item["fetched_at"]       = now_jst.isoformat()

            print(f"  [{i:>2}/{len(new_items)}] {title[:45]}...")
            print(f"       カテゴリ: {category}")
            print(f"       主体: {main_ents}")
            print(f"       関連: {related_ents[:4]}")
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
