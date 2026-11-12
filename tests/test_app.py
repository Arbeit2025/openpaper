"""openpaper のテスト。

実APIには接続せず、OpenAlex 応答のフィクスチャと requests.get のモックで完結する。
"""

import pytest
import requests

import app as app_module

# --- OpenAlex 応答フィクスチャ -------------------------------------------

OA_WORK = {
    "id": "https://openalex.org/W2741809807",
    "doi": "https://doi.org/10.1038/s41586-020-2649-2",
    "title": "Open Access Example Paper",
    "publication_year": 2020,
    "authorships": [
        {"author": {"display_name": "Alice Tanaka"}},
        {"author": {"display_name": "Bob Suzuki"}},
    ],
    "primary_location": {"source": {"display_name": "Nature"}},
    "open_access": {"is_oa": True, "oa_status": "gold"},
    "best_oa_location": {
        "pdf_url": "https://example.org/paper.pdf",
        "landing_page_url": "https://example.org/paper",
    },
}

OA_WORK_LANDING_ONLY = {
    "id": "https://openalex.org/W1111111111",
    "doi": "https://doi.org/10.1371/journal.pone.0000001",
    "title": "Landing Page Only Paper",
    "publication_year": 2021,
    "authorships": [{"author": {"display_name": "Eve Yamada"}}],
    "primary_location": {"source": {"display_name": "PLOS ONE"}},
    "open_access": {"is_oa": True, "oa_status": "gold"},
    "best_oa_location": {
        "pdf_url": None,
        "landing_page_url": "https://example.org/landing",
    },
}

CLOSED_WORK = {
    "id": "https://openalex.org/W2222222222",
    "doi": "https://doi.org/10.1016/j.example.2019.01.001",
    "title": "Closed Access Example Paper",
    "publication_year": 2019,
    "authorships": [{"author": {"display_name": "Carol Sato"}}],
    "primary_location": {"source": {"display_name": "Journal of Examples"}},
    "open_access": {"is_oa": False, "oa_status": "closed"},
    "best_oa_location": None,
}

LIST_RESPONSE = {"results": [OA_WORK, CLOSED_WORK]}


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def make_fake_get(doi_payload=None, list_payload=None, doi_status=200):
    """OpenAlex 用の requests.get フェイクを作る。"""

    def fake_get(url, params=None, headers=None, timeout=None):
        if "/doi:" in url:
            return FakeResponse(doi_payload, status_code=doi_status)
        return FakeResponse(list_payload)

    return fake_get


@pytest.fixture
def client():
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as c:
        yield c


# --- parse_work ------------------------------------------------------------

def test_parse_oa_work_has_free_link():
    parsed = app_module.parse_work(OA_WORK)
    assert parsed["is_oa"] is True
    assert parsed["oa_url"] == "https://example.org/paper.pdf"
    assert parsed["title"] == "Open Access Example Paper"
    assert parsed["year"] == 2020
    assert parsed["journal"] == "Nature"
    assert "Alice Tanaka" in parsed["authors"]
    assert parsed["doi_url"] == "https://doi.org/10.1038/s41586-020-2649-2"


def test_parse_oa_work_falls_back_to_landing_page():
    parsed = app_module.parse_work(OA_WORK_LANDING_ONLY)
    assert parsed["is_oa"] is True
    assert parsed["oa_url"] == "https://example.org/landing"


def test_parse_closed_work_has_no_free_link():
    parsed = app_module.parse_work(CLOSED_WORK)
    assert parsed["is_oa"] is False
    assert parsed["oa_url"] is None
    # DOI公式ページへのリンクは残る
    assert parsed["doi_url"] == "https://doi.org/10.1016/j.example.2019.01.001"


def test_parse_work_rejects_non_dict():
    assert app_module.parse_work(None) is None
    assert app_module.parse_work("not a dict") is None


# --- DOI 判定 --------------------------------------------------------------

@pytest.mark.parametrize(
    "value",
    [
        "10.1038/s41586-020-2649-2",
        "doi:10.1038/s41586-020-2649-2",
        "https://doi.org/10.1038/s41586-020-2649-2",
        "http://dx.doi.org/10.1016/j.example.2019.01.001",
        "  10.1371/journal.pone.0000001  ",
    ],
)
def test_is_doi_true(value):
    assert app_module.is_doi(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "machine learning",
        "自己調整学習",
        "10.1038",
        "https://example.com/paper",
        "",
    ],
)
def test_is_doi_false(value):
    assert app_module.is_doi(value) is False


def test_normalize_doi():
    assert (
        app_module.normalize_doi("https://doi.org/10.1038/abc")
        == "10.1038/abc"
    )
    assert app_module.normalize_doi("doi: 10.1038/abc") == "10.1038/abc"
    assert app_module.normalize_doi("10.1038/abc") == "10.1038/abc"


# --- 画面描画 ---------------------------------------------------------------

def test_index_page_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "openpaper" in html
    assert "オープンアクセス" in html
    # 合法OAのみという線引きが画面に出ている
    assert "合法的に公開されているオープンアクセス" in html


def test_keyword_search_renders_results(client, monkeypatch):
    monkeypatch.setattr(
        requests, "get", make_fake_get(list_payload=LIST_RESPONSE)
    )
    resp = client.get("/?q=example")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # OA論文: 無料で読むボタンとPDFリンク
    assert "無料で読む" in html
    assert "https://example.org/paper.pdf" in html
    # 非OA論文: タイトルは出るが無料リンクはなく、明示文が出る
    assert "Closed Access Example Paper" in html
    assert "オープンアクセス版は見つかりませんでした" in html


def test_doi_search_renders_single_result(client, monkeypatch):
    monkeypatch.setattr(
        requests, "get", make_fake_get(doi_payload=OA_WORK)
    )
    resp = client.get("/?q=10.1038/s41586-020-2649-2")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Open Access Example Paper" in html
    assert "無料で読む" in html


def test_doi_not_found_shows_notice(client, monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        make_fake_get(doi_payload={}, doi_status=404),
    )
    resp = client.get("/?q=10.9999/does-not-exist")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "そのDOIの論文が見つかりませんでした" in html


def test_keyword_search_no_results_shows_notice(client, monkeypatch):
    monkeypatch.setattr(
        requests, "get", make_fake_get(list_payload={"results": []})
    )
    resp = client.get("/?q=zzzz-no-hit")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "該当する論文が見つかりませんでした" in html


# --- API失敗時 ---------------------------------------------------------------

def test_api_failure_shows_friendly_error_not_500(client, monkeypatch):
    def failing_get(url, params=None, headers=None, timeout=None):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(requests, "get", failing_get)
    resp = client.get("/?q=example")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "接続できませんでした" in html


def test_api_failure_on_doi_search_shows_friendly_error(client, monkeypatch):
    def failing_get(url, params=None, headers=None, timeout=None):
        raise requests.Timeout("timed out")

    monkeypatch.setattr(requests, "get", failing_get)
    resp = client.get("/?q=10.1038/s41586-020-2649-2")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "接続できませんでした" in html


# ===========================================================================
# 複数ソース化 (Crossref / Europe PMC / arXiv / DOAJ / OpenAlex補完) のテスト
# ===========================================================================

# --- 新ソースのフィクスチャ -------------------------------------------------

CROSSREF_ITEM_A = {
    "DOI": "10.1038/s41586-020-2649-2",
    "title": ["Crossref Example Paper"],
    "author": [{"given": "Dave", "family": "Yamada"}],
    "published-print": {"date-parts": [[2022, 5, 1]]},
    "container-title": ["Journal of Crossref"],
    "type": "journal-article",
}

CROSSREF_ITEM_B = {
    "DOI": "10.1016/j.example.2019.01.001",
    "title": ["Crossref Closed Paper"],
    "author": [{"given": "Erin", "family": "Kato"}],
    "issued": {"date-parts": [[2019]]},
    "container-title": ["Closed Journal"],
    "type": "journal-article",
}

CROSSREF_LIST_RESPONSE = {
    "message": {"total-results": 2, "items": [CROSSREF_ITEM_A, CROSSREF_ITEM_B]}
}

EPMC_OA = {
    "title": "Europe PMC Open Paper",
    "authorString": "Frank Abe, Grace Ito.",
    "pubYear": "2023",
    "journalInfo": {"journal": {"title": "EPMC Journal"}},
    "doi": "10.1186/s12345-023-00001-1",
    "isOpenAccess": "Y",
    "fullTextUrlList": {
        "fullTextUrl": [
            {
                "url": "https://doi.org/10.1186/s12345-023-00001-1",
                "availability": "Subscription required",
                "availabilityCode": "S",
                "documentStyle": "doi",
            },
            {
                "url": "https://europepmc.org/articles/PMC100?pdf=render",
                "availability": "Open access",
                "availabilityCode": "OA",
                "documentStyle": "pdf",
            },
        ]
    },
}

EPMC_CLOSED = {
    "title": "Europe PMC Closed Paper",
    "authorString": "Hiro Mori.",
    "pubYear": "2018",
    "journalInfo": {"journal": {"title": "EPMC Closed Journal"}},
    "doi": "10.1000/epmc.closed.2018",
    "isOpenAccess": "N",
    "fullTextUrlList": {
        "fullTextUrl": [
            {
                "url": "https://doi.org/10.1000/epmc.closed.2018",
                "availability": "Subscription required",
                "availabilityCode": "S",
                "documentStyle": "doi",
            }
        ]
    },
}

EPMC_LIST_RESPONSE = {"resultList": {"result": [EPMC_OA, EPMC_CLOSED]}}

ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2301.00001v1</id>
    <title>ArXiv Example Paper on Learning</title>
    <published>2023-01-02T00:00:00Z</published>
    <author><name>Ivan Petrov</name></author>
    <author><name>Jane Doe</name></author>
    <link href="https://arxiv.org/abs/2301.00001v1" rel="alternate" type="text/html"/>
    <link href="https://arxiv.org/pdf/2301.00001v1" rel="related" type="application/pdf" title="pdf"/>
    <arxiv:doi>10.48550/arXiv.2301.00001</arxiv:doi>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2301.00002v1</id>
    <title>Second ArXiv Paper</title>
    <published>2023-02-03T00:00:00Z</published>
    <author><name>Ken Watanabe</name></author>
    <link href="https://arxiv.org/abs/2301.00002v1" rel="alternate" type="text/html"/>
    <link href="https://arxiv.org/pdf/2301.00002v1" rel="related" type="application/pdf" title="pdf"/>
  </entry>
</feed>
"""

ARXIV_EMPTY_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
)

DOAJ_REC = {
    "bibjson": {
        "title": "DOAJ Open Paper",
        "year": "2021",
        "author": [{"name": "Hana Mori"}],
        "journal": {"title": "DOAJ Journal"},
        "identifier": [
            {"type": "pissn", "id": "1234-5678"},
            {"type": "doi", "id": "10.9999/doaj.2021.001"},
        ],
        "link": [{"type": "fulltext", "url": "https://doaj.example.org/paper"}],
    }
}

DOAJ_LIST_RESPONSE = {"results": [DOAJ_REC], "total": 1}


class MultiFakeResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def make_multi_fake(
    crossref_payload=None,
    crossref_doi_payload=None,
    epmc_payload=None,
    arxiv_text="",
    doaj_payload=None,
    openalex_list=None,
    openalex_doi_map=None,
    fail_sources=(),
):
    """ソース別にURLで振り分ける requests.get フェイク。"""

    def fake_get(url, params=None, headers=None, timeout=None):
        if "api.crossref.org" in url:
            if "crossref" in fail_sources:
                raise requests.ConnectionError("crossref down")
            if "/works/" in url:
                if crossref_doi_payload is None:
                    return MultiFakeResponse({}, status_code=404)
                return MultiFakeResponse(crossref_doi_payload)
            return MultiFakeResponse(crossref_payload)
        if "europepmc" in url:
            if "europepmc" in fail_sources:
                raise requests.ConnectionError("epmc down")
            return MultiFakeResponse(epmc_payload)
        if "export.arxiv.org" in url:
            if "arxiv" in fail_sources:
                raise requests.ConnectionError("arxiv down")
            return MultiFakeResponse(None, text=arxiv_text)
        if "api.doaj.org" in url:
            if "doaj" in fail_sources:
                raise requests.ConnectionError("doaj down")
            return MultiFakeResponse(doaj_payload)
        if "api.openalex.org" in url:
            if "openalex" in fail_sources:
                raise requests.ConnectionError("openalex down")
            if "/doi:" in url:
                doi = url.rsplit("/doi:", 1)[1]
                payload = (openalex_doi_map or {}).get(doi)
                if payload is None:
                    return MultiFakeResponse({}, status_code=404)
                return MultiFakeResponse(payload)
            return MultiFakeResponse(openalex_list)
        raise AssertionError(f"unexpected URL: {url}")

    return fake_get


EMPTY_SOURCES = dict(
    crossref_payload={"message": {"items": []}},
    epmc_payload={"resultList": {"result": []}},
    arxiv_text=ARXIV_EMPTY_XML,
    doaj_payload={"results": []},
    openalex_list={"results": []},
)


# --- 新ソースのパース ---------------------------------------------------------

def test_parse_crossref_item_basic():
    parsed = app_module.parse_crossref_item(CROSSREF_ITEM_A)
    assert parsed["title"] == "Crossref Example Paper"
    assert parsed["authors"] == "Dave Yamada"
    assert parsed["year"] == 2022
    assert parsed["journal"] == "Journal of Crossref"
    assert parsed["doi"] == "10.1038/s41586-020-2649-2"
    assert parsed["doi_url"] == "https://doi.org/10.1038/s41586-020-2649-2"
    # Crossref からは OA 状態は判定できない
    assert parsed["oa_state"] == "unknown"
    assert parsed["oa_url"] is None
    assert parsed["source"] == "Crossref"


def test_parse_crossref_item_year_from_issued():
    parsed = app_module.parse_crossref_item(CROSSREF_ITEM_B)
    assert parsed["year"] == 2019


def test_parse_crossref_rejects_non_dict():
    assert app_module.parse_crossref_item(None) is None
    assert app_module.parse_crossref_item("x") is None


def test_parse_europepmc_oa_prefers_free_pdf():
    parsed = app_module.parse_europepmc_result(EPMC_OA)
    assert parsed["oa_state"] == "oa"
    assert parsed["oa_url"] == "https://europepmc.org/articles/PMC100?pdf=render"
    assert parsed["year"] == 2023
    assert parsed["journal"] == "EPMC Journal"
    assert "Frank Abe" in parsed["authors"]


def test_parse_europepmc_closed():
    parsed = app_module.parse_europepmc_result(EPMC_CLOSED)
    assert parsed["oa_state"] == "closed"
    assert parsed["oa_url"] is None
    assert parsed["doi_url"] == "https://doi.org/10.1000/epmc.closed.2018"


def test_parse_arxiv_feed():
    results = app_module.parse_arxiv_feed(ARXIV_XML)
    assert len(results) == 2
    first = results[0]
    assert first["title"] == "ArXiv Example Paper on Learning"
    assert first["oa_state"] == "oa"
    assert first["oa_url"] == "https://arxiv.org/pdf/2301.00001v1"
    assert first["year"] == 2023
    assert first["doi"] == "10.48550/arXiv.2301.00001"
    assert "Ivan Petrov" in first["authors"]
    assert results[1]["doi"] is None


def test_parse_arxiv_feed_invalid_xml():
    assert app_module.parse_arxiv_feed("not xml at all") == []
    assert app_module.parse_arxiv_feed("") == []


def test_parse_doaj_article():
    parsed = app_module.parse_doaj_article(DOAJ_REC)
    assert parsed["oa_state"] == "oa"
    assert parsed["oa_url"] == "https://doaj.example.org/paper"
    assert parsed["doi"] == "10.9999/doaj.2021.001"
    assert parsed["year"] == 2021
    assert parsed["journal"] == "DOAJ Journal"


# --- 統合・重複除去 -----------------------------------------------------------

def test_merge_results_dedupes_by_doi_and_upgrades_oa():
    crossref_item = app_module.parse_crossref_item(
        dict(CROSSREF_ITEM_A, DOI="10.1186/s12345-023-00001-1")
    )
    epmc_item = app_module.parse_europepmc_result(EPMC_OA)
    merged = app_module.merge_results([[crossref_item], [epmc_item]])
    assert len(merged) == 1
    # OA情報は判明している Europe PMC 側が採用される
    assert merged[0]["oa_state"] == "oa"
    assert merged[0]["oa_url"] == "https://europepmc.org/articles/PMC100?pdf=render"


def test_merge_results_dedupes_by_normalized_title():
    a = app_module.make_item(title="Deep Learning: A Survey!", source="Crossref")
    b = app_module.make_item(
        title="deep learning a survey", oa_state="oa",
        oa_url="https://example.org/dl.pdf", source="arXiv",
    )
    merged = app_module.merge_results([[a], [b]])
    assert len(merged) == 1
    assert merged[0]["oa_state"] == "oa"


# --- 補完・障害分離 -------------------------------------------------------------

def test_enrichment_upgrades_unknown_items(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        make_multi_fake(
            **dict(
                EMPTY_SOURCES,
                crossref_payload=CROSSREF_LIST_RESPONSE,
                openalex_doi_map={
                    "10.1038/s41586-020-2649-2": OA_WORK,
                    "10.1016/j.example.2019.01.001": CLOSED_WORK,
                },
            )
        ),
    )
    results, stats, error = app_module.search_works("example")
    assert error is None
    by_doi = {r["doi"]: r for r in results}
    assert by_doi["10.1038/s41586-020-2649-2"]["oa_state"] == "oa"
    assert (
        by_doi["10.1038/s41586-020-2649-2"]["oa_url"]
        == "https://example.org/paper.pdf"
    )
    assert by_doi["10.1016/j.example.2019.01.001"]["oa_state"] == "closed"


def test_enrichment_failure_keeps_unknown_wording(client, monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        make_multi_fake(
            **dict(
                EMPTY_SOURCES,
                crossref_payload={
                    "message": {"items": [CROSSREF_ITEM_A]}
                },
                fail_sources=("openalex",),
            )
        ),
    )
    resp = client.get("/?q=example")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Crossref Example Paper" in html
    # 補完できなかったものは「未確認」の文言で、非OA確定の文言とは区別される
    assert "OA版の有無を確認できませんでした" in html
    assert "オープンアクセス版は見つかりませんでした" not in html


def test_source_failure_isolated_and_stats_shown(client, monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        make_multi_fake(
            **dict(
                EMPTY_SOURCES,
                epmc_payload=EPMC_LIST_RESPONSE,
                fail_sources=("crossref",),
            )
        ),
    )
    resp = client.get("/?q=example")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # Crossref が落ちても Europe PMC の結果は返る
    assert "Europe PMC Open Paper" in html
    assert "無料で読む" in html
    # ソース別統計: Crossref は取得失敗、Europe PMC は件数表示
    assert "取得元:" in html
    assert "Crossref 取得失敗" in html
    assert "Europe PMC 2件" in html


def test_search_all_sources_fail_returns_error(monkeypatch):
    def failing_get(url, params=None, headers=None, timeout=None):
        raise requests.ConnectionError("all down")

    monkeypatch.setattr(requests, "get", failing_get)
    results, stats, error = app_module.search_works("example")
    assert results == []
    assert error is not None
    assert "接続できませんでした" in error
    assert all(count is None for _, count in stats)


# --- DOI検索の Crossref フォールバック -----------------------------------------

def test_doi_search_falls_back_to_crossref(client, monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        make_multi_fake(
            crossref_doi_payload={"message": CROSSREF_ITEM_A},
            fail_sources=("openalex",),
        ),
    )
    resp = client.get("/?q=10.1038/s41586-020-2649-2")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Crossref Example Paper" in html
    # Crossref フォールバック時は OA 状態は未確認の文言になる
    assert "OA版の有無を確認できませんでした" in html
