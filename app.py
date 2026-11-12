"""openpaper - オープンアクセス論文ファインダー

合法的に公開されているオープンアクセス(OA)論文だけを探して案内する検索ツール。
有料論文を不正に無料で読ませる仕組み(Sci-Hub 等)へのリンクや誘導は一切含まない。
OA版が存在しない論文については、DOI公式ページへのリンクのみを表示する。

データソース: OpenAlex API (https://api.openalex.org) ※APIキー不要
"""

import os
import re

import requests
from flask import Flask, render_template_string, request

app = Flask(__name__)

OPENALEX_WORKS = "https://api.openalex.org/works"
PER_PAGE = 10
TIMEOUT = 10
HEADERS = {"User-Agent": "openpaper/1.0 (open-access paper finder)"}

API_ERROR_MESSAGE = (
    "検索サービス(OpenAlex)に接続できませんでした。"
    "しばらく時間をおいてから、もう一度お試しください。"
)

_DOI_BODY_RE = re.compile(r"10\.\d{4,9}/\S+")


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
# OpenAlex 応答のパース (テスト可能な独立関数)
# ---------------------------------------------------------------------------
def parse_work(work):
    """OpenAlex の work 1件を画面表示用の辞書に変換する。

    無料リンク(oa_url)は、合法的なOA版の置き場所(best_oa_location)に
    PDF またはランディングページがある場合のみ設定する。
    """
    if not isinstance(work, dict):
        return None

    title = work.get("title") or work.get("display_name") or "(タイトル不明)"

    authors = []
    for authorship in work.get("authorships") or []:
        author = (authorship or {}).get("author") or {}
        name = author.get("display_name")
        if name:
            authors.append(name)
    if len(authors) > 5:
        authors_text = "、".join(authors[:5]) + " ほか"
    else:
        authors_text = "、".join(authors)

    journal = None
    primary = work.get("primary_location") or {}
    source = primary.get("source") or {}
    journal = source.get("display_name")

    oa_info = work.get("open_access") or {}
    is_oa = bool(oa_info.get("is_oa"))

    oa_url = None
    best = work.get("best_oa_location") or {}
    if is_oa and best:
        oa_url = best.get("pdf_url") or best.get("landing_page_url")

    return {
        "title": title,
        "authors": authors_text,
        "year": work.get("publication_year"),
        "journal": journal,
        "is_oa": is_oa,
        "oa_url": oa_url,
        "doi_url": work.get("doi"),  # https://doi.org/... 形式の公式URL
    }


# ---------------------------------------------------------------------------
# OpenAlex API 呼び出し
# ---------------------------------------------------------------------------
def search_works(query):
    """キーワード検索。(結果リスト, エラーメッセージ) を返す。"""
    try:
        resp = requests.get(
            OPENALEX_WORKS,
            params={"search": query, "per-page": PER_PAGE},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return [], API_ERROR_MESSAGE

    results = []
    for work in data.get("results") or []:
        parsed = parse_work(work)
        if parsed:
            results.append(parsed)
    return results, None


def fetch_by_doi(doi):
    """DOI で1件取得。(結果 or None, エラーメッセージ) を返す。"""
    try:
        resp = requests.get(
            f"{OPENALEX_WORKS}/doi:{doi}",
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        if resp.status_code == 404:
            return None, None
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None, API_ERROR_MESSAGE
    return parse_work(data), None


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

  {% for r in results %}
    <article class="card">
      <h2>
        {% if r.doi_url %}<a href="{{ r.doi_url }}">{{ r.title }}</a>{% else %}{{ r.title }}{% endif %}
      </h2>
      {% if r.authors %}<p class="meta">{{ r.authors }}</p>{% endif %}
      <p class="meta">
        {% if r.year %}{{ r.year }}年{% endif %}
        {% if r.journal %} / {{ r.journal }}{% endif %}
      </p>
      {% if r.is_oa %}
        <span class="badge oa">オープンアクセス</span>
      {% else %}
        <span class="badge">オープンアクセス版なし</span>
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
        {% if r.is_oa %}
          <p class="notfound-note">オープンアクセスですが、無料で読めるリンクが見つかりませんでした。</p>
        {% else %}
          <p class="notfound-note">オープンアクセス版は見つかりませんでした。</p>
        {% endif %}
      {% endif %}
    </article>
  {% endfor %}

  <footer>
    データ: OpenAlex API / 合法的なオープンアクセス版のみを案内します。
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
            results, error = search_works(q)
            if not error and not results:
                notice = "該当する論文が見つかりませんでした。"

    return render_template_string(
        TEMPLATE, q=q, results=results, error=error, notice=notice
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
