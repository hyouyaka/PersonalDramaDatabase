from __future__ import annotations

import io
import json
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import compare_weekly_info as compare


class ArchiveComparisonTests(unittest.TestCase):
    def test_removed_archive_is_confirmed_before_backup_and_after_info_update(self):
        # Neither the backup naming time nor current info time bounds membership.
        for archived_at in ("2026-11-06T05:17:43+00:00", "2026-11-14T00:00:00+00:00", None):
            with self.subTest(archived_at=archived_at):
                archive = {"100": {"record": {"dramaId": 100, "title": "已归档"}}}
                if archived_at is not None:
                    archive["100"]["archivedAt"] = archived_at
                original = deepcopy(archive)
                self.assertEqual(compare.confirm_archived(["100"], archive),
                                 {"100": archive["100"]["record"]})
                self.assertEqual(archive, original)

    def test_only_removed_ids_are_archived_and_other_removals_remain_unconfirmed(self):
        before = {"100": {"dramaId": 100}, "200": {"dramaId": 200}, "300": {"dramaId": 300}}
        after = {"200": before["200"], "400": {"dramaId": 400}}
        archive = {drama_id: {"record": {"dramaId": int(drama_id)}}
                   for drama_id in ("100", "200", "400", "500")}
        result = compare.compare(before, after)
        self.assertEqual(result["removed"], ["100", "300"])
        self.assertEqual(set(compare.confirm_archived(result["removed"], archive)), {"100"})

    def test_invalid_matched_archive_record_fails_instead_of_listing_wrong_drama(self):
        for entry in (None, {}, {"record": {"dramaId": 200}}):
            with self.subTest(entry=entry), self.assertRaisesRegex(ValueError, "Invalid archived record"):
                compare.confirm_archived(["100"], {"100": entry})

    def test_cli_classifies_removed_records_by_archive_on_both_platforms(self):
        sources = {"cvid-map:v1": {}}
        baseline = {}
        for platform in compare.PLATFORMS:
            label = "旧库作品"
            row = {"dramaId": "100", "title" if platform == "missevan" else "name": label}
            baseline[platform] = {"100": row} if platform == "missevan" else {"records": [row]}
            sources[f"{platform}:info:v2"] = {} if platform == "missevan" else {"records": []}
            sources[f"{platform}:info:meta:v2"] = {"updatedAt": "2026-11-13T05:14:30+00:00", "recordCount": 0}
            sources[f"{platform}:info:archive:v1"] = {"records": {
                "100": {"archivedAt": "2026-11-06T05:17:43+00:00", "record": row},
                "900": {"archivedAt": "2026-11-10T00:00:00+00:00", "record": {"dramaId": 900}},
            }}
        original = deepcopy(sources)
        log = {p: {"published": 0, "detected": 0} for p in compare.PLATFORMS}
        log["success"] = True
        args = ["compare_weekly_info.py", "--missevan-before", "20261106T051830205814Z_missevan.json",
                "--manbo-before", "20261106T051829815189Z_manbo.json", "--log", "weekly.log", "--details"]

        def read_backup(path, *unused, **kwargs):
            platform = "missevan" if "missevan" in path.name else "manbo"
            return json.dumps(baseline[platform])

        output, details = io.StringIO(), io.StringIO()
        with (
            patch.object(compare.sys, "argv", args),
            patch.object(compare, "read_sources", return_value=(sources, {})),
            patch.object(compare, "log_summary", return_value=log),
            patch.object(Path, "read_text", read_backup),
            patch.object(compare.sys, "stdout", output),
            patch.object(compare.sys, "stderr", details),
        ):
            compare.main()
        for label in ("猫耳", "漫播"):
            self.assertIn(f"# {label}归档下架（1部）", output.getvalue())
        self.assertEqual(output.getvalue().count("- 旧库作品（主役暂缺）"), 2)
        self.assertNotIn("移出原因待核对", output.getvalue())
        self.assertIn("本次发布归档：猫耳0部、漫播0部", output.getvalue())
        rows = [json.loads(line) for line in details.getvalue().splitlines()]
        self.assertEqual([row["archived_removed"] for row in rows[:2]], [["100"], ["100"]])
        self.assertEqual(sources, original)

    def test_cover_sound_ids_filter_does_not_hide_substantive_changes(self):
        before = {"100": {"dramaId": 100, "cover": "old", "soundIds": [1]},
                  "200": {"dramaId": 200, "cover": "old", "needpay": False}}
        after = {"100": {"dramaId": 100, "cover": "new", "soundIds": [2]},
                 "200": {"dramaId": 200, "cover": "new", "needpay": True}}
        result = compare.compare(before, after)
        self.assertEqual(result["cosmetic"], ["100"])
        self.assertEqual(result["changed"], {"200": {"needpay": [False, True]}})


if __name__ == "__main__":
    unittest.main()
