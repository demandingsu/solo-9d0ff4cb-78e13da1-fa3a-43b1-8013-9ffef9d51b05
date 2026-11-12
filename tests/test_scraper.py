import hashlib

import httpx
import pytest

from app.scraper import (
    inspect_issue,
    normalize_edition,
    normalize_issue,
    parse_page_numbers,
    split_ownership,
)
from sample_site.main import app as sample_app


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=sample_app),
        base_url="http://sample.test",
        follow_redirects=True,
    )


@pytest.mark.asyncio
async def test_clean_issue_is_qualified_and_crawls_pagination():
    async with make_client() as client:
        report = await inspect_issue(
            "http://sample.test/issues/clean/page/1.html", client=client
        )

    assert report.status == "合格"
    assert report.issue_no == "2024-05-01"
    assert report.edition == "第3版"
    assert report.total_pages == 6
    assert len(report.directory_pages) == 2
    assert [page.status for page in report.pages] == ["covered"] * 6
    assert len(report.accepted_scans) == 5
    assert report.failures == []
    assert report.ignored_scans == []
    # page 2 and 3 come from the same spread scan
    assert report.pages[1].sources[0].code == report.pages[2].sources[0].code
    assert report.pages[1].sources[0].pages_text == "2-3"


@pytest.mark.asyncio
async def test_conflict_and_missing_pages_make_issue_unqualified():
    async with make_client() as client:
        report = await inspect_issue(
            "http://sample.test/issues/conflict/page/1.html", client=client
        )

    assert report.status == "不合格"
    assert report.conflict_pages == [3]
    assert report.missing_pages == [4, 5]
    assert len(report.pages[2].sources) == 2
    assert len(report.ignored_scans) == 2
    reasons = " ".join(scan.reason for scan in report.ignored_scans)
    assert "期版不一致" in reasons
    assert "缺少图片链接" in reasons
    assert len(report.directory_pages) == 3
    assert report.failures == []


@pytest.mark.asyncio
async def test_failed_detail_forces_verification_status():
    async with make_client() as client:
        report = await inspect_issue(
            "http://sample.test/issues/pending/page/1.html", client=client
        )

    assert report.status == "待核实"
    assert len(report.failures) == 1
    assert report.failures[0].stage == "详情"
    assert "HTTP 404" in report.failures[0].message
    assert report.conflict_pages == [4]
    assert report.missing_pages == [5, 9, 11, 12]
    assert len(report.directory_pages) == 3
    assert len(report.ignored_scans) == 1
    assert "期版不一致" in report.ignored_scans[0].reason


@pytest.mark.asyncio
async def test_detail_failure_is_not_hidden_even_if_pages_are_covered():
    async with make_client() as client:
        report = await inspect_issue(
            "http://sample.test/issues/pending/page/1.html", client=client
        )

    # The visible page grid still reports findings, but the overall verdict is
    # never "合格" while a detail fetch failed.
    assert report.status != "合格"
    assert report.status_key == "pending"


def test_page_number_parsing_handles_spreads_and_lists():
    assert parse_page_numbers("5") == [5]
    assert parse_page_numbers("第2—3版") == [2, 3]
    assert parse_page_numbers("2,3,5-7") == [2, 3, 5, 6, 7]
    assert parse_page_numbers("12、13") == [12, 13]
    assert parse_page_numbers("13-12") == [12, 13]
    assert parse_page_numbers("") == []


def test_ownership_and_tokens_are_normalised():
    assert split_ownership("2024-05-01 第3版") == ("2024-05-01", "第3版")
    assert split_ownership("期号：2024-05-01　版次：第3版") == ("2024-05-01", "第3版")
    assert normalize_issue("2024年5月1日") == "2024-05-01"
    assert normalize_issue("2024-05-01") == "2024-05-01"
    assert normalize_edition("第三版") == "3"
    assert normalize_edition("第3版") == "3"
    assert normalize_edition("3版") == "3"
    assert normalize_edition("第4版") != normalize_edition("第3版")


@pytest.mark.asyncio
async def test_disjoint_pages_with_identical_images_are_listed_as_reuse():
    async with make_client() as client:
        report = await inspect_issue(
            "http://sample.test/issues/reuse/page/1.html", client=client
        )

    # Page coverage itself is complete and conflict-free.
    assert report.coverage_status == "合格"
    assert [page.status for page in report.pages] == ["covered"] * 6
    assert report.conflict_pages == []
    assert report.missing_pages == []

    # Image audit must fail the issue because of byte-identical reused images.
    assert report.image_status == "不合格"
    assert report.status == "不合格"
    assert len(report.reused_images) == 2

    by_pages = {
        tuple(group.page_numbers): group for group in report.reused_images
    }
    # Same image URL reused across page 1 and page 4.
    group_same_url = by_pages[(1, 4)]
    assert len(group_same_url.scans) == 2
    assert len(set(group_same_url.image_urls)) == 1
    assert all(scan.image_verified for scan in group_same_url.scans)

    # Different URLs serving identical bytes across the 2-3 spread and page 5.
    group_copy = by_pages[(2, 3, 5)]
    assert len(group_copy.image_urls) == 2
    assert len(set(group_copy.image_urls)) == 2

    # Every scan in a reuse group shares the same hash and every group member
    # is a distinct detail (a spread detail still counts only once).
    for group in report.reused_images:
        assert len({scan.detail_url for scan in group.scans}) == len(group.scans)
        assert len({scan.sha256 for scan in group.scans}) == 1
        assert group.scans[0].sha256 == group.sha256

    # The unique address is downloaded once even though two details use it.
    assert report.unique_image_count == 4
    assert report.verified_image_count == 4


@pytest.mark.asyncio
async def test_reuse_hash_matches_actual_streamed_bytes():
    async with make_client() as client:
        report = await inspect_issue(
            "http://sample.test/issues/reuse/page/1.html", client=client
        )
        response = await client.get(report.reused_images[0].image_urls[0])

    expected = hashlib.sha256(response.content).hexdigest()
    assert report.reused_images[0].sha256 == expected
    assert all(scan.sha256 == expected for scan in report.reused_images[0].scans)


@pytest.mark.asyncio
async def test_image_failure_keeps_confirmed_reuse_but_pends_image_audit():
    async with make_client() as client:
        report = await inspect_issue(
            "http://sample.test/issues/badimg/page/1.html", client=client
        )

    # Coverage is complete; the image failure does not change page findings.
    assert report.coverage_status == "合格"
    assert [page.status for page in report.pages] == ["covered"] * 5

    assert report.image_status == "待核实"
    assert report.status == "待核实"
    image_failures = report.image_failures
    assert len(image_failures) == 1
    assert image_failures[0].stage == "图片"
    assert "HTTP 404" in image_failures[0].message
    assert "/img/missing/" in image_failures[0].url

    # The confirmed reuse (page 1 / page 3) is still surfaced.
    assert len(report.reused_images) == 1
    group = report.reused_images[0]
    assert group.page_numbers == [1, 3]

    # The failed scan is neither marked verified nor dropped from coverage.
    failed_scan = next(
        scan for scan in report.accepted_scans if not scan.image_verified
    )
    assert failed_scan.pages_text == "2"
    assert failed_scan.sha256 == ""
    assert report.verified_image_count == report.unique_image_count - 1


@pytest.mark.asyncio
async def test_clean_issue_passes_image_audit_with_distinct_hashes():
    async with make_client() as client:
        report = await inspect_issue(
            "http://sample.test/issues/clean/page/1.html", client=client
        )

    assert report.image_status == "合格"
    assert report.reused_images == []
    assert report.image_failures == []
    assert report.verified_image_count == report.unique_image_count
    assert len({scan.sha256 for scan in report.accepted_scans}) == len(
        report.accepted_scans
    )


@pytest.mark.asyncio
async def test_each_unique_image_url_is_downloaded_only_once(monkeypatch):
    from app import scraper

    calls: list[str] = []
    original = scraper._fingerprint_image

    async def counting_fingerprint(client, url, semaphore):
        calls.append(url)
        return await original(client, url, semaphore)

    monkeypatch.setattr(scraper, "_fingerprint_image", counting_fingerprint)
    async with make_client() as client:
        report = await inspect_issue(
            "http://sample.test/issues/reuse/page/1.html", client=client
        )

    assert len(calls) == report.unique_image_count
    assert len(calls) == len(set(calls))
