"""Tests for cross-provider playlist migration (archive → webdav re-point)."""

from __future__ import annotations

import pytest

from file_provider.service import Service, _date_key


def _row(provider: str, source_ref: str, title: str, sort_order: float) -> dict:
    from file_provider.providers.base import ProviderTrack

    t = ProviderTrack(title=title, source_ref=source_ref, size_bytes=1)
    return {
        "track_id": t.track_id(provider),
        "title": title,
        "duration_seconds": 0,
        "size_bytes": 1,
        "provider": provider,
        "source_ref": source_ref,
        "sort_order": float(sort_order),
        "has_video": False,
    }


ARCHIVE_ROWS = [
    _row("archive", "item::BTO Radio Interviews/#03 - 04_25_02 - #9B58.mp3",
         "BTO Radio Interviews — #03 - 04_25_02 - #9B58", 0),
    _row("archive", "item::BTO Radio Interviews/#01 - 11_08_01 - #4193.mp3",
         "BTO Radio Interviews — #01 - 11_08_01 - #4193", 1),
    _row("archive", "item::Extras/only-on-archive.mp3", "only on archive", 2),
]

WEBDAV_ROWS = [
    _row("webdav", "Lectures2002-2011/2002/apr-2002.mp4", "apr-2002", 0),
    _row("webdav", "Lectures2002-2011/2001/nov-2001.mp4", "nov-2001", 1),
    _row("webdav", "VolumeSeries/volume-i-power-vs-force.mp4", "volume-i-power-vs-force", 2),
]


@pytest.fixture
def migrated(db, cache) -> Service:
    s = Service(db, cache, [])
    s.db.upsert_tracks(ARCHIVE_ROWS)
    s.db.upsert_tracks(WEBDAV_ROWS)
    s.db.set_cursor(1)
    return s


# ================================================================== date keys
class TestDateKey:
    def test_bto_mmddyy(self) -> None:
        assert _date_key("BTO Radio Interviews — #03 - 04_25_02 - #9B58") == "2002-04"
        assert _date_key("#01 - 11_08_01 - #4193") == "2001-11"

    def test_pure_month_name(self) -> None:
        assert _date_key("apr-2002") == "2002-04"
        assert _date_key("nov 2001") == "2001-11"
        assert _date_key("apr2002") == "2002-04"

    def test_embedded_months_get_no_key(self) -> None:
        # Audiobook/compilation titles must NOT claim a lecture's month slot.
        assert _date_key("Satsangs/satsang-qa-jan-mar-jul-2011") is None
        assert _date_key("TheWaytoGod:RadicalSubjectivity:The'I'ofSelf-February2002[B006W1639I]") is None
        assert _date_key("HomoSpiritusDevotionalNondualitySeries(SpiritualCommunity-June2003)") is None

    def test_none_when_no_date(self) -> None:
        assert _date_key("volume-i-power-vs-force") is None
        assert _date_key("only on archive") is None

    def test_invalid_ranges(self) -> None:
        assert _date_key("#00 - 13_25_02 - #aa") is None
        assert _date_key("#00 - 04_00_02 - #aa") is None
        assert _date_key("xyz-1999") is None


# ================================================================= migration
class TestMigrateProvider:
    def test_repoints_by_date_and_keeps_order(self, migrated: Service) -> None:
        moved = migrated.migrate_provider(primary="webdav", fallback="archive")
        assert moved == 2

        rows = migrated.db.fetchall(
            "SELECT provider, title, sort_order FROM tracks ORDER BY sort_order, track_id"
        )
        titles = [r["title"] for r in rows]
        # Matched gdrive tracks take the archive positions they replaced;
        # unmatched archive stays; unmatched gdrive appends at the end.
        assert titles == ["apr-2002", "nov-2001", "only on archive", "volume-i-power-vs-force"]
        assert [r["provider"] for r in rows] == ["webdav", "webdav", "archive", "webdav"]
        assert [r["sort_order"] for r in rows] == [0.0, 1.0, 2.0, 3.0]

    def test_cursor_still_points_at_same_content(self, migrated: Service) -> None:
        migrated.migrate_provider(primary="webdav", fallback="archive")
        row = migrated.db.track_at(migrated.db.get_cursor())
        # Cursor was 1: BTO #01 (11_08_01 = Nov 2001) → nov-2001.mp4.
        assert row["provider"] == "webdav"
        assert row["title"] == "nov-2001"

    def test_idempotent(self, migrated: Service) -> None:
        assert migrated.migrate_provider(primary="webdav", fallback="archive") == 2
        assert migrated.migrate_provider(primary="webdav", fallback="archive") == 0

    def test_no_match_leaves_everything(self, db, cache) -> None:
        s = Service(db, cache, [])
        s.db.upsert_tracks(ARCHIVE_ROWS)
        s.db.upsert_tracks(
            [_row("webdav", "a/volume-i-power-vs-force.mp4", "volume-i-power-vs-force", 0)]
        )
        assert s.migrate_provider(primary="webdav", fallback="archive") == 0
        assert s.db.playlist_length() == 4

    def test_missing_provider_returns_zero(self, migrated: Service) -> None:
        assert migrated.migrate_provider(primary="webdav", fallback="telegram") == 0
        assert migrated.migrate_provider(primary="telegram", fallback="archive") == 0

    def test_refresh_prunes_inactive_provider_rows(self, db, cache, fake_provider) -> None:
        db.upsert_tracks(
            [_row("telegram", "chan/1.mp3", "stale telegram track", 0)]
        )
        s = Service(db, cache, [fake_provider])
        assert db.playlist_length() == 1
        s.refresh_playlist()
        rows = db.fetchall("SELECT DISTINCT provider FROM tracks")
        assert [r["provider"] for r in rows] == ["fake"]
        assert db.playlist_length() == 3

    def test_refresh_triggers_migration(self, db, cache, fake_provider) -> None:
        class FakeWebDav:
            name = "webdav"

            def is_configured(self):
                return True

            def list_tracks(self):
                from file_provider.providers.base import ProviderTrack

                return [ProviderTrack(title="apr-2002", source_ref="2002/apr-2002.mp4")]

            def ensure_cached(self, source_ref, target_path):
                raise AssertionError("not called during migration")

        class FakeArchive:
            name = "archive"

            def is_configured(self):
                return True

            def list_tracks(self):
                from file_provider.providers.base import ProviderTrack

                return [
                    ProviderTrack(
                        title="BTO Radio Interviews — #03 - 04_25_02 - #9B58",
                        source_ref="item::x.mp3",
                    )
                ]

            def ensure_cached(self, source_ref, target_path):
                raise AssertionError("not called during migration")

        s = Service(db, cache, [FakeWebDav(), FakeArchive()])
        s.refresh_playlist()
        rows = s.db.fetchall("SELECT provider, title FROM tracks ORDER BY sort_order")
        assert [(r["provider"], r["title"]) for r in rows] == [("webdav", "apr-2002")]
