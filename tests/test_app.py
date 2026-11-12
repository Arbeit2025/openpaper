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
