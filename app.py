"""openpaper - オープンアクセス論文ファインダー

合法的に公開されているオープンアクセス(OA)論文だけを探して案内する検索ツール。
有料論文を不正に無料で読ませる仕組み(Sci-Hub 等)へのリンクや誘導は一切含まない。
OA版が存在しない論文については、DOI公式ページへのリンクのみを表示する。

キーワード検索のデータソース(いずれもAPIキー不要、複数併用):
  - Crossref     : 書誌メタデータ(タイトル・著者・年・掲載誌・DOI)の主ソース
  - Europe PMC   : OA判定と無料フルテキストリンク
  - arXiv        : プレプリント(PDFは常に無料で読める)
  - DOAJ         : OAジャーナル掲載論文
  - OpenAlex     : OA状態・best_oa_location の補完
OpenAlex の検索エンドポイントはキーなしアクセスの日次予算制で枯渇しやすいため、
単独ソースに依存せず、1ソースが失敗しても他ソースの結果で検索を成立させる。
"""

import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import requests
from flask import Flask, render_template_string, request

app = Flask(__name__)

OPENALEX_WORKS = "https://api.openalex.org/works"
CROSSREF_WORKS = "https://api.crossref.org/works"
EUROPEPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
ARXIV_API = "https://export.arxiv.org/api/query"
DOAJ_SEARCH = "https://api.doaj.org/search/articles"

CROSSREF_ROWS = 20
EUROPEPMC_ROWS = 20
ARXIV_ROWS = 10
DOAJ_ROWS = 10
OPENALEX_ROWS = 10
MAX_RESULTS = 30
ENRICH_LIMIT = 8  # OA補完のために OpenAlex へ個別照会する上限件数
TIMEOUT = 8
HEADERS = {
    "User-Agent": (
        "openpaper/1.1 (open-access paper finder; "
        "+https://openpaper-ruei.onrender.com)"
    )
}

API_ERROR_MESSAGE = (
    "検索サービスに接続できませんでした。"
    "しばらく時間をおいてから、もう一度お試しください。"
)

_DOI_BODY_RE = re.compile(r"10\.\d{4,9}/\S+")

# OA状態: "oa"=OA版あり(確認済み) / "closed"=OA版なし(確認済み) / "unknown"=未確認
OA_STATES = ("oa", "closed", "unknown")


# ---------------------------------------------------------------------------
# DOI の判定・正規化
# ---------------------------------------------------------------------------
def normalize_doi(q):
    """入力文字列から DOI 本体(10.xxxx/...)を取り出す。"""
    if not q:
        return ""
    q = q.strip()
    q = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", q, flags=re.IGNORECASE)
    q = re.sub(r"^doi:\s*", "", q, flags=re.IGNORECASE)
    return q.strip()


def is_doi(q):
    """入力が DOI らしいかどうかを判定する。"""
    if not q:
        return False
    return _DOI_BODY_RE.fullmatch(normalize_doi(q)) is not None


# ---------------------------------------------------------------------------
# 共通ヘルパー
# ---------------------------------------------------------------------------
def format_authors(names):
    """著者名リストを表示用文字列にする(6名以上は先頭5名+「ほか」)。"""
    names = [n for n in (names or []) if n]
    if len(names) > 5:
        return "、".join(names[:5]) + " ほか"
    return "、".join(names)


def make_item(
    title,
    authors="",
    year=None,
    journal=None,
    doi=None,
    oa_state="unknown",
    oa_url=None,
    source="",
):
    """各ソース共通の結果dictを作る。"""
    doi = normalize_doi(doi) if doi else None
    if doi and not _DOI_BODY_RE.fullmatch(doi):
        doi = None
    if oa_state not in OA_STATES:
        oa_state = "unknown"
    if oa_state != "oa":
        oa_url = None
    return {
        "title": title or "(タイトル不明)",
        "authors": authors or "",
        "year": year,
        "journal": journal,
        "doi": doi,
        "doi_url": f"https://doi.org/{doi}" if doi else None,
        "is_oa": oa_state == "oa",
        "oa_state": oa_state,
        "oa_url": oa_url,
        "source": source,
    }


def normalize_title_key(title):
    """重複除去用にタイトルを正規化する。"""
    if not title:
        return ""
    return re.sub(r"[^0-9a-zぁ-んァ-ヶ一-龠]+", "", title.lower())


# ---------------------------------------------------------------------------
# OpenAlex 応答のパース
# ---------------------------------------------------------------------------
def parse_work(work):
    """OpenAlex の work 1件を画面表示用の辞書に変換する。

    無料リンク(oa_url)は、合法的なOA版の置き場所(best_oa_location)に
    PDF またはランディングページがある場合のみ設定する。
    """
    if not isinstance(work, dict):
        return None

    title = work.get("title") or work.get("display_name") or "(タイトル不明)"

    names = []
    for authorship in work.get("authorships") or []:
        author = (authorship or {}).get("author") or {}
        name = author.get("display_name")
        if name:
            names.append(name)

    primary = work.get("primary_location") or {}
    source = primary.get("source") or {}
    journal = source.get("display_name")

    oa_info = work.get("open_access") or {}
    is_oa = bool(oa_info.get("is_oa"))

    oa_url = None
    best = work.get("best_oa_location") or {}
    if is_oa and best:
        oa_url = best.get("pdf_url") or best.get("landing_page_url")

    return make_item(
        title=title,
        authors=format_authors(names),
        year=work.get("publication_year"),
        journal=journal,
        doi=work.get("doi"),
        oa_state="oa" if is_oa else "closed",
        oa_url=oa_url,
        source="OpenAlex",
    )


# ---------------------------------------------------------------------------
# Crossref 応答のパース
# ---------------------------------------------------------------------------
def _crossref_year(item):
    for key in ("published", "published-print", "published-online",
                "issued", "created"):
        date = item.get(key) or {}
        parts = date.get("date-parts") or []
        if parts and parts[0] and isinstance(parts[0][0], int):
            return parts[0][0]
    return None


def parse_crossref_item(item):
    """Crossref の work 1件を変換する。OA状態は Crossref からは判定できない
    ため "unknown" とし、後段の OpenAlex 補完に委ねる。"""
    if not isinstance(item, dict):
        return None

    titles = item.get("title") or []
    title = titles[0] if titles else None

    names = []
    for author in item.get("author") or []:
        if not isinstance(author, dict):
            continue
        if author.get("name"):
            names.append(author["name"])
        else:
            full = " ".join(
                p for p in (author.get("given"), author.get("family")) if p
            )
            if full:
                names.append(full)

    containers = item.get("container-title") or []
    journal = containers[0] if containers else None

    return make_item(
        title=title,
        authors=format_authors(names),
        year=_crossref_year(item),
        journal=journal,
        doi=item.get("DOI"),
        oa_state="unknown",
        source="Crossref",
    )


# ---------------------------------------------------------------------------
# Europe PMC 応答のパース
# ---------------------------------------------------------------------------
def _europepmc_free_url(rec):
    """fullTextUrlList から無料で読めるURLを1つ選ぶ(PDF優先)。"""
    urls = ((rec.get("fullTextUrlList") or {}).get("fullTextUrl")) or []
    free = []
    for entry in urls:
        if not isinstance(entry, dict) or not entry.get("url"):
            continue
        code = (entry.get("availabilityCode") or "").upper()
        label = (entry.get("availability") or "").lower()
        if code in ("OA", "F") or label in ("open access", "free"):
            free.append(entry)
    if not free:
        return None
    for entry in free:
        if (entry.get("documentStyle") or "").lower() == "pdf":
            return entry["url"]
    return free[0]["url"]


def parse_europepmc_result(rec):
    """Europe PMC の result 1件を変換する。
    isOpenAccess=Y、または無料フルテキストURLがあれば OA として扱う。"""
    if not isinstance(rec, dict):
        return None

    free_url = _europepmc_free_url(rec)
    is_oa_flag = (rec.get("isOpenAccess") or "").upper() == "Y"
    if is_oa_flag or free_url:
        oa_state, oa_url = "oa", free_url
    else:
        oa_state, oa_url = "closed", None

    year = rec.get("pubYear")
    try:
        year = int(year) if year else None
    except (TypeError, ValueError):
        year = None

    journal = ((rec.get("journalInfo") or {}).get("journal") or {}).get("title")

    author_string = rec.get("authorString") or ""
    names = [n.strip() for n in author_string.rstrip(".").split(",") if n.strip()]

    return make_item(
        title=rec.get("title"),
        authors=format_authors(names),
        year=year,
        journal=journal,
        doi=rec.get("doi"),
        oa_state=oa_state,
        oa_url=oa_url,
        source="Europe PMC",
    )


# ---------------------------------------------------------------------------
# arXiv (Atom) 応答のパース
# ---------------------------------------------------------------------------
_ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}


def parse_arxiv_entry(entry):
    """arXiv Atom の entry 1件を変換する。プレプリントのPDFは常に
    無料で読めるため OA として案内する。"""
    if entry is None:
        return None

    def text_of(tag):
        el = entry.find(f"atom:{tag}", _ATOM_NS)
        return (el.text or "").strip() if el is not None and el.text else None

    title = text_of("title")
    if title:
        title = re.sub(r"\s+", " ", title)

    names = []
    for author in entry.findall("atom:author", _ATOM_NS):
        name_el = author.find("atom:name", _ATOM_NS)
        if name_el is not None and name_el.text:
            names.append(name_el.text.strip())

    published = text_of("published") or ""
    year = None
    if len(published) >= 4 and published[:4].isdigit():
        year = int(published[:4])

    pdf_url = None
    abs_url = None
    for link in entry.findall("atom:link", _ATOM_NS):
        href = link.get("href")
        if not href:
            continue
        if link.get("title") == "pdf" or link.get("type") == "application/pdf":
            pdf_url = href
        elif link.get("rel") == "alternate":
            abs_url = href

    doi = None
    doi_el = entry.find("arxiv:doi", _ATOM_NS)
    if doi_el is not None and doi_el.text:
        doi = doi_el.text.strip()

    return make_item(
        title=title,
        authors=format_authors(names),
        year=year,
        journal="arXiv",
        doi=doi,
        oa_state="oa",
        oa_url=pdf_url or abs_url,
        source="arXiv",
    )


def parse_arxiv_feed(xml_text):
    """arXiv Atom フィード全体をパースして結果リストを返す。"""
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    results = []
    for entry in root.findall("atom:entry", _ATOM_NS):
        parsed = parse_arxiv_entry(entry)
        if parsed:
            results.append(parsed)
    return results


# ---------------------------------------------------------------------------
# DOAJ 応答のパース
# ---------------------------------------------------------------------------
def parse_doaj_article(rec):
    """DOAJ の検索結果1件を変換する。DOAJ 収録誌はすべてOA誌のため、
    掲載論文は OA として扱い、fulltext リンクがあれば案内する。"""
    if not isinstance(rec, dict):
        return None
    bib = rec.get("bibjson") or {}
    if not isinstance(bib, dict):
        return None

    names = []
    for author in bib.get("author") or []:
        if isinstance(author, dict) and author.get("name"):
            names.append(author["name"])

    year = bib.get("year")
    try:
        year = int(year) if year else None
    except (TypeError, ValueError):
        year = None

    doi = None
    for ident in bib.get("identifier") or []:
        if isinstance(ident, dict) and ident.get("type") == "doi":
            doi = ident.get("id")
            break

    fulltext_url = None
    for link in bib.get("link") or []:
        if isinstance(link, dict) and link.get("type") == "fulltext":
            fulltext_url = link.get("url")
            break

    journal = (bib.get("journal") or {}).get("title")

    return make_item(
        title=bib.get("title"),
        authors=format_authors(names),
        year=year,
        journal=journal,
        doi=doi,
        oa_state="oa",
        oa_url=fulltext_url,
        source="DOAJ",
    )


# ---------------------------------------------------------------------------
# 各ソースへの問い合わせ (失敗しても例外を外に出さず (結果, 成否) を返す)
# ---------------------------------------------------------------------------
def fetch_openalex_search(query):
    """OpenAlex キーワード検索。予算枯渇などで失敗しやすいので、
    失敗は (空リスト, False) として扱い、他ソースに委ねる。"""
    try:
        resp = requests.get(
            OPENALEX_WORKS,
            params={"search": query, "per-page": OPENALEX_ROWS},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return [], False
    if not isinstance(data, dict):
        return [], True
    results = []
    for work in data.get("results") or []:
        parsed = parse_work(work)
        if parsed:
            results.append(parsed)
    return results, True


def fetch_crossref_search(query):
    try:
        resp = requests.get(
            CROSSREF_WORKS,
            params={"query.bibliographic": query, "rows": CROSSREF_ROWS},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return [], False
    if not isinstance(data, dict):
        return [], True
    items = (data.get("message") or {}).get("items") or []
    results = []
    for item in items:
        parsed = parse_crossref_item(item)
        if parsed:
            results.append(parsed)
    return results, True


def fetch_europepmc_search(query):
    try:
        resp = requests.get(
            EUROPEPMC_SEARCH,
            params={
                "query": query,
                "format": "json",
                "pageSize": EUROPEPMC_ROWS,
                "resultType": "core",
            },
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return [], False
    if not isinstance(data, dict):
        return [], True
    records = (data.get("resultList") or {}).get("result") or []
    results = []
    for rec in records:
        parsed = parse_europepmc_result(rec)
        if parsed:
            results.append(parsed)
    return results, True


def fetch_arxiv_search(query):
    cleaned = query.replace('"', " ").strip()
    if not cleaned:
        return [], True
    if " " in cleaned:
        search_query = f'all:"{cleaned}"'
    else:
        search_query = f"all:{cleaned}"
    try:
        resp = requests.get(
            ARXIV_API,
            params={
                "search_query": search_query,
                "start": 0,
                "max_results": ARXIV_ROWS,
                "sortBy": "relevance",
            },
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        xml_text = resp.content.decode("utf-8", errors="replace")
    except Exception:
        return [], False
    return parse_arxiv_feed(xml_text), True


def fetch_doaj_search(query):
    try:
        resp = requests.get(
            f"{DOAJ_SEARCH}/{quote(query, safe='')}",
            params={"pageSize": DOAJ_ROWS},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return [], False
    if not isinstance(data, dict):
        return [], True
    results = []
    for rec in data.get("results") or []:
        parsed = parse_doaj_article(rec)
        if parsed:
            results.append(parsed)
    return results, True


# ---------------------------------------------------------------------------
# 統合・重複除去・OA補完
# ---------------------------------------------------------------------------
def _item_key(item):
    if item.get("doi"):
        return "doi:" + item["doi"].lower()
    key = normalize_title_key(item.get("title"))
    return "title:" + key if key else None


def _merge_duplicate(base, extra):
    """重複した2件を統合する。OA情報は「判明している側」を優先する。"""
    if base["oa_state"] == "unknown" and extra["oa_state"] != "unknown":
        base["oa_state"] = extra["oa_state"]
        base["is_oa"] = extra["is_oa"]
        base["oa_url"] = extra["oa_url"]
    elif base["oa_url"] is None and extra["oa_url"]:
        base["oa_url"] = extra["oa_url"]
    for field in ("authors", "journal", "year", "doi", "doi_url"):
        if not base.get(field) and extra.get(field):
            base[field] = extra[field]
    return base


def merge_results(groups):
    """ソース別の結果リスト群を、DOI/タイトルで重複除去して統合する。"""
    merged = {}
    order = []
    for group in groups:
        for item in group:
            key = _item_key(item)
            if key is None:
                continue
            if key in merged:
                _merge_duplicate(merged[key], item)
            else:
                merged[key] = dict(item)
                order.append(key)
    return [merged[k] for k in order]


def _lookup_openalex_oa(doi):
    """OpenAlex の DOI 個別照会で OA 状態を引く。
    (oa_state, oa_url) を返し、取得できない場合は None を返す。"""
    try:
        resp = requests.get(
            f"{OPENALEX_WORKS}/doi:{doi}",
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        parsed = parse_work(resp.json())
    except Exception:
        return None
    if not parsed:
        return None
    return parsed["oa_state"], parsed["oa_url"]


def enrich_items(items, limit=ENRICH_LIMIT):
    """OA状態が未確認(unknown)の結果を、上位 limit 件まで
    OpenAlex の DOI 個別照会で補完する(ベストエフォート)。"""
    targets = [
        it for it in items
        if it["oa_state"] == "unknown" and it.get("doi")
    ][:limit]
    if not targets:
        return items
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_lookup_openalex_oa, it["doi"]): it for it in targets}
        for future, item in futures.items():
            try:
                found = future.result()
            except Exception:
                found = None
            if found:
                item["oa_state"], item["oa_url"] = found
                item["is_oa"] = item["oa_state"] == "oa"
    return items


# ---------------------------------------------------------------------------
# キーワード検索 (複数ソース併用) / DOI検索
# ---------------------------------------------------------------------------
SOURCE_FETCHERS = [
    ("Crossref", fetch_crossref_search),
    ("Europe PMC", fetch_europepmc_search),
    ("arXiv", fetch_arxiv_search),
    ("DOAJ", fetch_doaj_search),
    ("OpenAlex", fetch_openalex_search),
]


def search_works(query):
    """キーワード検索。(結果リスト, ソース別統計, エラーメッセージ) を返す。
    ソース別統計は (ソース名, 件数 or None=取得失敗) のリスト。
    全ソースが失敗したときだけエラーメッセージを返す。"""
    groups = {}
    stats = []
    with ThreadPoolExecutor(max_workers=len(SOURCE_FETCHERS)) as pool:
        futures = {
            pool.submit(fetcher, query): name
            for name, fetcher in SOURCE_FETCHERS
        }
        for future, name in futures.items():
            try:
                items, ok = future.result()
            except Exception:
                items, ok = [], False
            groups[name] = items if ok else []
            stats.append((name, len(items) if ok else None))

    if all(count is None for _, count in stats):
        return [], stats, API_ERROR_MESSAGE

    merged = merge_results([groups[name] for name, _ in SOURCE_FETCHERS])
    enrich_items(merged)
    stats.sort(key=lambda pair: [n for n, _ in SOURCE_FETCHERS].index(pair[0]))
    return merged[:MAX_RESULTS], stats, None


def _openalex_by_doi(doi):
    """OpenAlex で DOI 1件取得。(item or None, 状態) を返す。
    状態は "ok" / "not_found" / "error"。"""
    try:
        resp = requests.get(
            f"{OPENALEX_WORKS}/doi:{doi}",
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        if resp.status_code == 404:
            return None, "not_found"
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None, "error"
    parsed = parse_work(data)
    if parsed is None:
        return None, "not_found"
    return parsed, "ok"


def _crossref_by_doi(doi):
    """Crossref で DOI 1件取得。(item or None, 状態) を返す。"""
    try:
        resp = requests.get(
            f"{CROSSREF_WORKS}/{quote(doi, safe='/')}",
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        if resp.status_code == 404:
            return None, "not_found"
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None, "error"
    if not isinstance(data, dict):
        return None, "not_found"
    message = data.get("message")
    if not isinstance(message, dict):
        return None, "not_found"
    parsed = parse_crossref_item(message)
    if parsed is None:
        return None, "not_found"
    return parsed, "ok"


def fetch_by_doi(doi):
    """DOI で1件取得。(結果 or None, エラーメッセージ) を返す。
    OpenAlex を主とし、失敗・未収録の場合は Crossref の書誌情報で
    フォールバックする(OA状態は「未確認」扱い)。"""
    item, status = _openalex_by_doi(doi)
    if status == "ok":
        return item, None
    c_item, c_status = _crossref_by_doi(doi)
    if c_status == "ok":
        return c_item, None
    if status == "not_found" and c_status == "not_found":
        return None, None
    return None, API_ERROR_MESSAGE


# ---------------------------------------------------------------------------
# 画面
# ---------------------------------------------------------------------------
TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>openpaper - オープンアクセス論文ファインダー</title>
<style>
  :root{
    --bg:#ffffff; --fg:#1b1b1b; --card:#f6f7f8; --border:#d8dde3;
    --accent:#0b5cad; --accent-fg:#ffffff; --muted:#5a6470;
    --oa:#137333; --err-bg:#fdecea; --err-fg:#8c1d18;
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#121417; --fg:#e8eaed; --card:#1d2026; --border:#33373f;
      --accent:#6ab0f5; --accent-fg:#0b1520; --muted:#9aa0a6;
      --oa:#5cc47a; --err-bg:#3a2320; --err-fg:#f2b8b5;
    }
  }
  [data-theme="dark"]{
    --bg:#121417; --fg:#e8eaed; --card:#1d2026; --border:#33373f;
    --accent:#6ab0f5; --accent-fg:#0b1520; --muted:#9aa0a6;
    --oa:#5cc47a; --err-bg:#3a2320; --err-fg:#f2b8b5;
  }
  [data-theme="light"]{
    --bg:#ffffff; --fg:#1b1b1b; --card:#f6f7f8; --border:#d8dde3;
    --accent:#0b5cad; --accent-fg:#ffffff; --muted:#5a6470;
    --oa:#137333; --err-bg:#fdecea; --err-fg:#8c1d18;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0}
  body{
    background:var(--bg); color:var(--fg);
    font-family:system-ui,-apple-system,"Segoe UI",sans-serif;
    line-height:1.6;
  }
  .wrap{max-width:880px;margin:0 auto;padding:16px}
  header{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
  h1{font-size:1.5rem;margin:8px 0}
  h1 .sub{font-size:.9rem;color:var(--muted);font-weight:normal}
  .theme-btn{
    margin-left:auto;background:var(--card);color:var(--fg);
    border:1px solid var(--border);border-radius:8px;padding:6px 12px;cursor:pointer;
  }
  .policy{
    background:var(--card);border:1px solid var(--border);border-radius:8px;
    padding:10px 14px;font-size:.9rem;color:var(--muted);
  }
  form.search{display:flex;gap:8px;margin:18px 0;flex-wrap:wrap}
  form.search input{
    flex:1 1 260px;min-width:0;padding:10px;border-radius:8px;
    border:1px solid var(--border);background:var(--bg);color:var(--fg);font-size:1rem;
  }
  form.search button{
    padding:10px 20px;border:none;border-radius:8px;cursor:pointer;
    background:var(--accent);color:var(--accent-fg);font-size:1rem;
  }
  .error{
    background:var(--err-bg);color:var(--err-fg);border-radius:8px;
    padding:12px 14px;margin:14px 0;overflow-wrap:anywhere;
  }
  .notice{color:var(--muted);margin:14px 0}
  .sources{color:var(--muted);font-size:.85rem;margin:10px 0}
  .card{
    background:var(--card);border:1px solid var(--border);border-radius:10px;
    padding:14px 16px;margin:12px 0;overflow-wrap:anywhere;
  }
  .card h2{font-size:1.1rem;margin:0 0 6px}
  .card h2 a{color:var(--fg)}
  .meta{color:var(--muted);font-size:.9rem;margin:2px 0}
  .badge{
    display:inline-block;font-size:.8rem;border-radius:999px;
    padding:2px 10px;border:1px solid var(--border);margin:6px 0;
  }
  .badge.oa{color:var(--oa);border-color:var(--oa);font-weight:bold}
  .actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
  .btn{
    display:inline-block;padding:8px 16px;border-radius:8px;text-decoration:none;
    border:1px solid var(--border);color:var(--fg);background:var(--bg);
  }
  .btn.read{background:var(--accent);color:var(--accent-fg);border-color:var(--accent)}
  .notfound-note{font-size:.9rem;color:var(--muted);margin:6px 0 0}
  footer{color:var(--muted);font-size:.85rem;margin:28px 0 10px}
  a{color:var(--accent)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>openpaper <span class="sub">オープンアクセス論文ファインダー</span></h1>
    <button type="button" class="theme-btn" id="themeToggle">ライト／ダーク切替</button>
  </header>

  <p class="policy">
    このツールは、<strong>合法的に公開されているオープンアクセス(OA)論文だけ</strong>を探して案内します。
    OA版がない論文については、DOIの公式ページへのリンクのみを表示します。
    有料論文を不正に読めるようにするサイトへのリンクは一切掲載しません。
  </p>

  <form class="search" method="get" action="/">
    <input type="text" name="q" value="{{ q }}" placeholder="キーワード または DOI（例: 10.1038/s41586-020-2649-2）">
    <button type="submit">検索</button>
  </form>

  {% if error %}
    <div class="error">{{ error }}</div>
  {% endif %}

  {% if notice %}
    <p class="notice">{{ notice }}</p>
  {% endif %}

  {% if source_stats %}
    <p class="sources">取得元:
      {% for name, count in source_stats %}{{ name }} {% if count is none %}取得失敗{% else %}{{ count }}件{% endif %}{% if not loop.last %} / {% endif %}{% endfor %}
    </p>
  {% endif %}

  {% for r in results %}
    <article class="card">
      <h2>
        {% if r.doi_url %}<a href="{{ r.doi_url }}">{{ r.title }}</a>{% else %}{{ r.title }}{% endif %}
      </h2>
      {% if r.authors %}<p class="meta">{{ r.authors }}</p>{% endif %}
      <p class="meta">
        {% if r.year %}{{ r.year }}年{% endif %}
        {% if r.journal %} / {{ r.journal }}{% endif %}
        {% if r.source %} / 出典: {{ r.source }}{% endif %}
      </p>
      {% if r.oa_state == "oa" %}
        <span class="badge oa">オープンアクセス</span>
      {% elif r.oa_state == "closed" %}
        <span class="badge">オープンアクセス版なし</span>
      {% else %}
        <span class="badge">OA版は未確認</span>
      {% endif %}
      <div class="actions">
        {% if r.oa_url %}
          <a class="btn read" href="{{ r.oa_url }}">無料で読む</a>
        {% endif %}
        {% if r.doi_url %}
          <a class="btn" href="{{ r.doi_url }}">DOI公式ページ</a>
        {% endif %}
      </div>
      {% if not r.oa_url %}
        {% if r.oa_state == "oa" %}
          <p class="notfound-note">オープンアクセスですが、無料で読めるリンクが見つかりませんでした。</p>
        {% elif r.oa_state == "closed" %}
          <p class="notfound-note">オープンアクセス版は見つかりませんでした。</p>
        {% else %}
          <p class="notfound-note">OA版の有無を確認できませんでした。</p>
        {% endif %}
      {% endif %}
    </article>
  {% endfor %}

  <footer>
    データ: Crossref / Europe PMC / arXiv / DOAJ / OpenAlex（いずれもAPIキー不要の公開API） / 合法的なオープンアクセス版のみを案内します。
  </footer>
</div>
<script>
  (function(){
    var saved = null;
    try { saved = localStorage.getItem("openpaper-theme"); } catch(e) {}
    if (saved) { document.documentElement.setAttribute("data-theme", saved); }
    document.getElementById("themeToggle").addEventListener("click", function(){
      var cur = document.documentElement.getAttribute("data-theme");
      var dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
      var next = (cur === "dark" || (!cur && dark)) ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("openpaper-theme", next); } catch(e) {}
    });
  })();
</script>
</body>
</html>
"""


@app.route("/")
def index():
    q = (request.args.get("q") or "").strip()
    results = []
    source_stats = []
    error = None
    notice = None

    if q:
        if is_doi(q):
            item, error = fetch_by_doi(normalize_doi(q))
            if item:
                results = [item]
            elif not error:
                notice = "そのDOIの論文が見つかりませんでした。"
        else:
            results, source_stats, error = search_works(q)
            if not error and not results:
                notice = "該当する論文が見つかりませんでした。"

    return render_template_string(
        TEMPLATE,
        q=q,
        results=results,
        source_stats=source_stats,
        error=error,
        notice=notice,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
