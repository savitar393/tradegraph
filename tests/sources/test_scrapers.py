from io import BytesIO
import json

import pytest

from tradegraph.sources import acquisition, scrapers


KEMENKEU_SEARCH_HTML = """
<html><body>
  <a href="/dok/pmk-35-tahun-2023">PMK 35 TAHUN 2023</a>
  <a href="/dok/pmk-35-tahun-2023/summary">Duplicate link</a>
  <a href="/dok/pmk-81-pmk-04-2022">PMK 81/PMK.04/2022</a>
  <a href="/search?bentuk=Peraturan">Not a dok</a>
</body></html>
"""

KEMENKEU_PAYLOAD = {
    "data": {
        "Id": "a648108a-de1f-4dd7-0302-08db31bc9649",
        "Slug": "pmk-35-tahun-2023",
        "Nomor": "PMK 35 TAHUN 2023",
        "Judul": "Tata Cara Penyerahan Surat Keterangan Asal dan/atau Deklarasi Asal Barang",
        "TEU": "Kementerian Keuangan",
        "Status": "Approved",
        "Jenis": {"Id": 105, "Keterangan": "Peraturan Menteri"},
        "PUU": {
            "No": 35,
            "Tahun": 2023,
            "TglPenetapan": "$D2023-03-28T00:00:00.000Z",
            "TglPengundangan": "$D2023-03-29T00:00:00.000Z",
            "TglMulaiBerlaku": "$D2023-04-28T00:00:00.000Z",
        },
        "Label": [{"Jenis": 1, "Teks": "BARANG IMPOR"}, {"Jenis": 2, "Teks": "Bidang Bea Cukai"}],
        "File": [
            {
                "Jenis": 3,
                "Keterangan": "Abstrak PDF (Indonesia)",
                "MediaCatalog": {"Id": "abs-id", "Path": "35 TAHUN 2023.pdf", "Ext": ".pdf", "FileName": "35 TAHUN 2023.pdf"},
            },
            {
                "Jenis": 1,
                "Keterangan": "Fulltext PDF (Indonesia)",
                "MediaCatalog": {"Id": "full-id", "Path": "2023pmkeuangan035.pdf", "Ext": ".pdf", "FileName": "2023pmkeuangan035.pdf"},
            },
            {
                "Jenis": 1,
                "Keterangan": "Fulltext PDF (Indonesia)",
                "MediaCatalog": {"Id": "abs-id", "Path": "35 TAHUN 2023.pdf", "Ext": ".pdf", "FileName": "35 TAHUN 2023.pdf"},
            },
            {
                "Jenis": 4,
                "Keterangan": "Pratinjau",
                "MediaCatalog": {"Id": "img-id", "Path": "preview.jpg", "Ext": ".jpg", "FileName": "preview.jpg"},
            },
        ],
        "Relasi": [
            {
                "KodeRelasi": "MSB",
                "Nama": "Mencabut Sebagian   ",
                "TglMulaiBerlaku": "$D2023-04-28T00:00:00.000Z",
                "Nomor": "131/PMK.04/2020",
                "Judul": "Tata Cara Pengenaan Tarif Bea Masuk ATIGA",
                "Slug": "131-pmk-04-2020",
            }
        ],
    }
}

KEMENKEU_DETAIL_HTML = (
    "<html><body><script>"
    f'self.__next_f.push([1,{json.dumps("21:[" + json.dumps(KEMENKEU_PAYLOAD) + "]")}])'
    "</script></body></html>"
)

KEMENDAG_SEARCH_HTML = """
<html><body>
  <a href="https://jdih.kemendag.go.id/peraturan">Daftar</a>
  <a href="https://jdih.kemendag.go.id/peraturan/peraturan-menteri-perdagangan-republik-indonesia-nomor-18-tahun-2026-tentang-perubahan-kedua-atas-peraturan-menteri-perdagangan-nomor-16-tahun-2025">18 Tahun 2026</a>
  <a href="https://jdih.kemendag.go.id/peraturan/download/uuid/file_peraturan">Unduh</a>
</body></html>
"""

KEMENDAG_DETAIL_HTML = """
<html><head><title>Permendag 18 Tahun 2026</title></head><body>
<table>
  <tr><th>Meta</th><th>Keterangan</th></tr>
  <tr><td>Judul</td><td>Peraturan Menteri Perdagangan Republik Indonesia Nomor 18 Tahun 2026 tentang Perubahan Kedua atas Peraturan Menteri Perdagangan Nomor 16 Tahun 2025 tentang Kebijakan dan Pengaturan Impor</td></tr>
  <tr><td>T.E.U. Badan/Pengarang</td><td>Indonesia.Kementerian Perdagangan</td></tr>
  <tr><td>Nomor Peraturan</td><td>18</td></tr>
  <tr><td>Jenis / Bentuk Peraturan</td><td>Peraturan Menteri</td></tr>
  <tr><td>Tanggal Penetapan / Pengundangan</td><td>03 June 2026 / 04 June 2026</td></tr>
  <tr><td>Subjek</td><td>KEBIJAKAN - PENGATURAN - IMPOR</td></tr>
  <tr><td>Status Peraturan</td><td>Berlaku</td></tr>
  <tr><td>Keterangan Status</td><td>MENGUBAH</td></tr>
  <tr><td><a href="/peraturan/permendag-16-tahun-2025">Peraturan Menteri Perdagangan Republik Indonesia Nomor 16 Tahun 2025 tentang Kebijakan dan Pengaturan Impor</a></td></tr>
  <tr><td>Keterangan Putusan</td><td>-</td></tr>
</table>
<a href="https://jdih.kemendag.go.id/peraturan/download/b3094f6b/file_peraturan">Peraturan [ID]</a>
<a href="https://jdih.kemendag.go.id/peraturan/download/b3094f6b/file_abstract">Abstrak</a>
</body></html>
"""


def test_parse_kemenkeu_search_and_detail():
    hits = scrapers.parse_kemenkeu_search_html(KEMENKEU_SEARCH_HTML)
    assert [hit.slug for hit in hits] == ["pmk-35-tahun-2023", "pmk-81-pmk-04-2022"]
    assert hits[0].url == "https://jdih.kemenkeu.go.id/dok/pmk-35-tahun-2023/summary"

    reg = scrapers.parse_kemenkeu_detail_html(KEMENKEU_DETAIL_HTML, hits[0].url)
    assert reg.portal == "jdih-kemenkeu"
    assert reg.number == "PMK 35 TAHUN 2023"
    assert reg.authority == "Kementerian Keuangan Republik Indonesia"
    assert reg.year == 2023
    assert reg.enacted_date == "2023-03-28"
    assert reg.promulgated_date == "2023-03-29"
    assert reg.effective_date == "2023-04-28"
    assert reg.fulltext_pdf_url() == "https://jdih.kemenkeu.go.id/api/download/full-id/2023pmkeuangan035.pdf"
    assert [f.role for f in reg.files] == ["ABSTRACT_PDF", "FULLTEXT_PDF"]
    assert len(reg.relations) == 1
    assert reg.relations[0].relation_kind == "PARTIALLY_REVOKES"
    assert reg.relations[0].target_number == "131/PMK.04/2020"
    assert reg.relations[0].effective_date == "2023-04-28"


def test_parse_kemendag_search_and_detail():
    hits = scrapers.parse_kemendag_search_html(KEMENDAG_SEARCH_HTML)
    assert len(hits) == 1
    assert hits[0].label == "18 Tahun 2026"

    reg = scrapers.parse_kemendag_detail_html(KEMENDAG_DETAIL_HTML, hits[0].url)
    assert reg.portal == "jdih-kemendag"
    assert reg.number == "Permendag 18 Tahun 2026"
    assert reg.authority == "Kementerian Perdagangan Republik Indonesia"
    assert reg.enacted_date == "2026-06-03"
    assert reg.promulgated_date == "2026-06-04"
    assert reg.status == "Berlaku"
    assert reg.subjects == ("KEBIJAKAN", "PENGATURAN", "IMPOR")
    assert reg.fulltext_pdf_url() == "https://jdih.kemendag.go.id/peraturan/download/b3094f6b/file_peraturan"
    assert len(reg.relations) == 1
    assert reg.relations[0].relation_kind == "AMENDS"
    assert reg.relations[0].target_number == "16 Tahun 2025"


def test_scraper_bounded_execution_and_archiving(tmp_path, monkeypatch):
    pages = {
        "https://jdih.kemenkeu.go.id/search?q=ska&bentuk=Peraturan+Menteri&order=desc": KEMENKEU_SEARCH_HTML,
        "https://jdih.kemenkeu.go.id/dok/pmk-35-tahun-2023/summary": KEMENKEU_DETAIL_HTML,
    }
    scraper = scrapers.JdihKemenkeuScraper(fetcher=lambda url: pages[url], delay_seconds=0)
    results = scraper.scrape("ska", limit=1)
    assert len(results) == 1
    assert results[0].slug == "pmk-35-tahun-2023"

    class FakePdfResponse(BytesIO):
        def __init__(self, body: bytes):
            super().__init__(body)
            self.headers = {"Content-Length": str(len(body))}

    monkeypatch.setattr(acquisition, "urlopen", lambda *a, **kw: FakePdfResponse(b"%PDF-1.4 sample"))
    archived = scrapers.archive_regulation(results[0], tmp_path)
    assert archived is not None
    assert archived.authority == "Kementerian Keuangan Republik Indonesia"
    assert archived.local_ref.endswith(".pdf")
    assert (tmp_path / f"{archived.content_hash}.pdf").read_bytes() == b"%PDF-1.4 sample"


def test_scraper_rejects_non_positive_limit():
    with pytest.raises(ValueError):
        scrapers.JdihKemenkeuScraper(fetcher=lambda _: "", delay_seconds=0).search("impor", limit=0)
    with pytest.raises(ValueError):
        scrapers.JdihKemendagScraper(fetcher=lambda _: "", delay_seconds=0).search("impor", limit=0)


def test_kemenkeu_passive_relation_label_overrides_active_code():
    payload = {
        "data": {
            "Slug": "209-pmk-04-2022",
            "Nomor": "209/PMK.04/2022",
            "Judul": "Tata Cara Pengenaan Tarif Bea Masuk RCEP",
            "TEU": "Kementerian Keuangan",
            "Status": "Approved",
            "PUU": {"Tahun": 2022, "TglMulaiBerlaku": "$D2023-01-02T00:00:00.000Z"},
            "File": [],
            "Relasi": [
                {
                    "KodeRelasi": "MSB",
                    "Nama": "Dicabut sebagian dengan",
                    "TglMulaiBerlaku": "$D2023-04-28T00:00:00.000Z",
                    "Nomor": "PMK 35 TAHUN 2023",
                    "Judul": "Tata Cara Penyerahan SKA dan DAB",
                    "Slug": "pmk-35-tahun-2023",
                },
                {
                    "KodeRelasi": "MUB",
                    "Nama": "Diubah dengan",
                    "TglMulaiBerlaku": "$D2026-10-02T00:00:00.000Z",
                    "Nomor": "PMK 69 TAHUN 2026",
                    "Judul": "Perubahan Tarif",
                    "Slug": "pmk-69-tahun-2026",
                },
            ],
        }
    }
    html = (
        "<html><body><script>"
        f'self.__next_f.push([1,{json.dumps("21:[" + json.dumps(payload) + "]")}])'
        "</script></body></html>"
    )
    reg = scrapers.parse_kemenkeu_detail_html(html, "https://jdih.kemenkeu.go.id/dok/209-pmk-04-2022/summary")
    assert [r.relation_kind for r in reg.relations] == ["PARTIALLY_REVOKED_BY", "AMENDED_BY"]
