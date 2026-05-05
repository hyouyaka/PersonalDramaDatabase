import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import sync_new_drama_ids as sync


class SyncNewDramaIdsReadinessTests(unittest.TestCase):
    def test_missevan_ready_requires_is_member_field(self) -> None:
        base = {
            "title": "猫耳剧",
            "type": 4,
            "catalog": 89,
            "createTime": "2026.04",
            "maincvs": [1, 2],
        }

        self.assertFalse(sync.is_missevan_ready(base))
        self.assertTrue(sync.is_missevan_ready({**base, "is_member": False}))

    def test_manbo_ready_requires_vip_free_field(self) -> None:
        base = {
            "name": "漫播剧",
            "catalog": 1,
            "createTime": "2026.04",
            "genre": "纯爱",
            "mainCvNicknames": ["CV A", "CV B"],
        }

        self.assertFalse(sync.is_manbo_ready(base))
        self.assertTrue(sync.is_manbo_ready({**base, "vipFree": 0}))

    def test_upload_json_file_rejects_suspiciously_small_info_store(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "missevan-drama-info.json"
            path.write_text('{"101": {"dramaId": 101}}', encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "Refusing to upload"):
                sync.upload_json_file(sync.MISSEVAN_INFO_KEY, path)

    def test_merge_and_upload_preserves_remote_changes_and_updates_only_queue_ids(self) -> None:
        remote_missevan = {str(idx): {"dramaId": idx, "title": f"远端猫耳{idx}"} for idx in range(101)}
        remote_missevan["101"] = {"dramaId": 101, "title": "远端运行中新增"}
        remote_missevan["102"] = {"dramaId": 102, "title": "远端保留"}
        remote_manbo = {
            "version": 1,
            "updatedAt": "remote",
            "records": [{"dramaId": str(idx), "name": f"远端漫播{idx}"} for idx in range(51)]
            + [
                {"dramaId": "201", "name": "远端运行中新增"},
                {"dramaId": "202", "name": "远端保留"},
            ],
        }

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            missevan_path = root / "missevan-drama-info.json"
            manbo_path = root / "manbo-drama-info.json"
            local_missevan = {
                "101": {"dramaId": 101, "title": "本次更新猫耳"},
                "102": {"dramaId": 102, "title": "本地旧猫耳不得上传"},
            }
            local_manbo = {
                "version": 1,
                "updatedAt": "local",
                "records": [
                    {"dramaId": "201", "name": "本次更新漫播"},
                    {"dramaId": "202", "name": "本地旧漫播不得上传"},
                ],
            }
            missevan_path.write_text(json.dumps(local_missevan, ensure_ascii=False), encoding="utf-8")
            manbo_path.write_text(json.dumps(local_manbo, ensure_ascii=False), encoding="utf-8")
            uploads = {}

            def fake_upstash(command):
                if command == ["GET", sync.MISSEVAN_INFO_KEY]:
                    return json.dumps(remote_missevan, ensure_ascii=False)
                if command == ["GET", sync.MANBO_INFO_KEY]:
                    return json.dumps(remote_manbo, ensure_ascii=False)
                if command[0] == "SET":
                    uploads[command[1]] = json.loads(command[2])
                    return "OK"
                raise AssertionError(command)

            with patch.object(sync, "upstash_request", side_effect=fake_upstash):
                sync.merge_and_upload_info_file(sync.MISSEVAN_INFO_KEY, missevan_path, ["101"])
                sync.merge_and_upload_info_file(sync.MANBO_INFO_KEY, manbo_path, ["201"])
            saved_missevan = json.loads(missevan_path.read_text(encoding="utf-8"))
            saved_manbo = {item["dramaId"]: item for item in json.loads(manbo_path.read_text(encoding="utf-8"))["records"]}

        self.assertEqual(uploads[sync.MISSEVAN_INFO_KEY]["101"]["title"], "本次更新猫耳")
        self.assertEqual(uploads[sync.MISSEVAN_INFO_KEY]["102"]["title"], "远端保留")
        self.assertEqual(saved_missevan["102"]["title"], "远端保留")
        uploaded_manbo = {item["dramaId"]: item for item in uploads[sync.MANBO_INFO_KEY]["records"]}
        self.assertEqual(uploaded_manbo["201"]["name"], "本次更新漫播")
        self.assertEqual(uploaded_manbo["202"]["name"], "远端保留")
        self.assertEqual(uploads[sync.MANBO_INFO_KEY]["updatedAt"], "remote")
        self.assertEqual(saved_manbo["202"]["name"], "远端保留")

    def test_main_downloads_remote_info_before_append_and_merges_latest_remote_on_upload(self) -> None:
        remote_missevan = {str(idx): {"dramaId": idx, "title": f"猫耳{idx}"} for idx in range(101)}
        remote_manbo = {
            "version": 1,
            "updatedAt": "remote",
            "records": [{"dramaId": str(idx), "name": f"漫播{idx}"} for idx in range(51)],
        }
        latest_missevan = {**remote_missevan, "999": {"dramaId": 999, "title": "运行中远端新增猫耳"}}
        latest_manbo = {
            **remote_manbo,
            "records": remote_manbo["records"] + [{"dramaId": "999", "name": "运行中远端新增漫播"}],
        }

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            missevan_path = root / "missevan-drama-info.json"
            manbo_path = root / "manbo-drama-info.json"
            missevan_path.write_text('{"local": {"dramaId": "local"}}', encoding="utf-8")
            manbo_path.write_text('{"records": [{"dramaId": "local"}]}', encoding="utf-8")
            commands = []
            get_counts = {sync.MANBO_INFO_KEY: 0, sync.MISSEVAN_INFO_KEY: 0}

            def fake_upstash(command):
                if command == ["GET", sync.QUEUE_KEY]:
                    return json.dumps({"manbo": ["201"], "missevan": ["101"]})
                if command == ["GET", sync.MANBO_INFO_KEY]:
                    get_counts[sync.MANBO_INFO_KEY] += 1
                    payload = remote_manbo if get_counts[sync.MANBO_INFO_KEY] == 1 else latest_manbo
                    return json.dumps(payload, ensure_ascii=False)
                if command == ["GET", sync.MISSEVAN_INFO_KEY]:
                    get_counts[sync.MISSEVAN_INFO_KEY] += 1
                    payload = remote_missevan if get_counts[sync.MISSEVAN_INFO_KEY] == 1 else latest_missevan
                    return json.dumps(payload, ensure_ascii=False)
                if command[0] == "SET":
                    commands.append(command)
                    return "OK"
                raise AssertionError(command)

            def fake_run_script(script_name, drama_ids):
                if script_name == "append_missevan_ids.py":
                    self.assertEqual(json.loads(missevan_path.read_text(encoding="utf-8")), remote_missevan)
                    data = json.loads(missevan_path.read_text(encoding="utf-8"))
                    data["101"] = {"dramaId": 101, "title": "本次补齐猫耳", "is_member": True, "type": 4, "catalog": 89, "maincvs": [1, 2]}
                    data["local-only"] = {"dramaId": "local-only", "title": "本地不应上传"}
                    missevan_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                if script_name == "append_manbo_ids.py":
                    self.assertEqual(json.loads(missevan_path.read_text(encoding="utf-8")), remote_missevan)
                    self.assertEqual(json.loads(manbo_path.read_text(encoding="utf-8")), remote_manbo)
                    data = json.loads(manbo_path.read_text(encoding="utf-8"))
                    data["records"].append(
                        {"dramaId": "201", "name": "本次补齐漫播", "vipFree": 1, "catalog": 1, "createTime": "2026.04", "genre": "纯爱", "mainCvNicknames": ["A", "B"]}
                    )
                    data["records"].append({"dramaId": "local-only", "name": "本地不应上传"})
                    manbo_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                commands.append(["RUN", script_name, list(drama_ids)])

            with (
                patch.object(sync, "ROOT", root),
                patch.object(sync, "MISSEVAN_INFO_PATH", missevan_path),
                patch.object(sync, "MANBO_INFO_PATH", manbo_path),
                patch.object(sync, "load_env_file"),
                patch.object(sync, "upstash_request", side_effect=fake_upstash),
                patch.object(sync, "run_script", side_effect=fake_run_script),
            ):
                self.assertEqual(sync.main(), 0)

        self.assertIn(["RUN", "append_manbo_ids.py", ["201"]], commands)
        self.assertIn(["RUN", "append_missevan_ids.py", ["101"]], commands)
        manbo_upload = next(json.loads(command[2]) for command in commands if command[:2] == ["SET", sync.MANBO_INFO_KEY])
        missevan_upload = next(json.loads(command[2]) for command in commands if command[:2] == ["SET", sync.MISSEVAN_INFO_KEY])
        self.assertEqual(missevan_upload["101"]["title"], "本次补齐猫耳")
        self.assertEqual(missevan_upload["999"]["title"], "运行中远端新增猫耳")
        self.assertNotIn("local-only", missevan_upload)
        uploaded_manbo = {item["dramaId"]: item for item in manbo_upload["records"]}
        self.assertEqual(uploaded_manbo["201"]["name"], "本次补齐漫播")
        self.assertEqual(uploaded_manbo["999"]["name"], "运行中远端新增漫播")
        self.assertNotIn("local-only", uploaded_manbo)

    def test_main_stops_before_append_when_remote_info_is_invalid(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            missevan_path = root / "missevan-drama-info.json"
            manbo_path = root / "manbo-drama-info.json"

            def fake_upstash(command):
                if command == ["GET", sync.QUEUE_KEY]:
                    return json.dumps({"manbo": ["201"], "missevan": []})
                if command == ["GET", sync.MANBO_INFO_KEY]:
                    return json.dumps({"records": []})
                raise AssertionError(command)

            with (
                patch.object(sync, "ROOT", root),
                patch.object(sync, "MISSEVAN_INFO_PATH", missevan_path),
                patch.object(sync, "MANBO_INFO_PATH", manbo_path),
                patch.object(sync, "load_env_file"),
                patch.object(sync, "upstash_request", side_effect=fake_upstash),
                patch.object(sync, "run_script", side_effect=AssertionError("should not append")),
            ):
                with self.assertRaisesRegex(RuntimeError, "Refusing to download"):
                    sync.main()

    def test_main_does_not_download_remote_info_when_queue_is_empty(self) -> None:
        commands = []

        def fake_upstash(command):
            commands.append(command)
            if command == ["GET", sync.QUEUE_KEY]:
                return json.dumps({"manbo": [], "missevan": []})
            raise AssertionError(command)

        with (
            patch.object(sync, "load_env_file"),
            patch.object(sync, "upstash_request", side_effect=fake_upstash),
            patch.object(sync, "run_script", side_effect=AssertionError("should not append")),
        ):
            self.assertEqual(sync.main(), 0)

        self.assertEqual(commands, [["GET", sync.QUEUE_KEY]])

    def test_main_does_not_backfill_ranks_by_default(self) -> None:
        def fake_upstash(command):
            if command == ["GET", sync.QUEUE_KEY]:
                return json.dumps({"manbo": ["201"], "missevan": []})
            if command == ["GET", sync.MANBO_INFO_KEY]:
                return json.dumps(
                    {
                        "version": 1,
                        "updatedAt": "remote",
                        "records": [{"dramaId": str(idx), "name": f"漫播{idx}"} for idx in range(51)],
                    },
                    ensure_ascii=False,
                )
            if command == ["GET", sync.MISSEVAN_INFO_KEY]:
                return json.dumps({str(idx): {"dramaId": idx, "title": f"猫耳{idx}"} for idx in range(101)}, ensure_ascii=False)
            if command[0] == "SET":
                return "OK"
            raise AssertionError(command)

        with (
            TemporaryDirectory() as tmp,
            patch.object(sync, "ROOT", Path(tmp)),
            patch.object(sync, "MISSEVAN_INFO_PATH", Path(tmp) / "missevan-drama-info.json"),
            patch.object(sync, "MANBO_INFO_PATH", Path(tmp) / "manbo-drama-info.json"),
            patch.object(sync, "load_env_file"),
            patch.object(sync, "upstash_request", side_effect=fake_upstash),
            patch.object(sync, "run_script"),
            patch.object(sync, "backfill_rank_metadata", side_effect=AssertionError("should not backfill")),
        ):
            self.assertEqual(sync.main([]), 0)

    def test_main_backfills_only_missevan_platform_when_requested(self) -> None:
        backfills = []

        def fake_upstash(command):
            if command == ["GET", sync.QUEUE_KEY]:
                return json.dumps({"manbo": [], "missevan": ["101"]})
            if command == ["GET", sync.MANBO_INFO_KEY]:
                return json.dumps(
                    {
                        "version": 1,
                        "updatedAt": "remote",
                        "records": [{"dramaId": str(idx), "name": f"漫播{idx}"} for idx in range(51)],
                    },
                    ensure_ascii=False,
                )
            if command == ["GET", sync.MISSEVAN_INFO_KEY]:
                return json.dumps({str(idx): {"dramaId": idx, "title": f"猫耳{idx}"} for idx in range(101)}, ensure_ascii=False)
            if command[0] == "SET":
                return "OK"
            raise AssertionError(command)

        with (
            TemporaryDirectory() as tmp,
            patch.object(sync, "ROOT", Path(tmp)),
            patch.object(sync, "MISSEVAN_INFO_PATH", Path(tmp) / "missevan-drama-info.json"),
            patch.object(sync, "MANBO_INFO_PATH", Path(tmp) / "manbo-drama-info.json"),
            patch.object(sync, "load_env_file"),
            patch.object(sync, "upstash_request", side_effect=fake_upstash),
            patch.object(sync, "run_script"),
            patch.object(sync, "backfill_rank_metadata", side_effect=lambda platforms: backfills.append(platforms)),
        ):
            self.assertEqual(sync.main(["--backfill-ranks"]), 0)

        self.assertEqual(backfills, [("missevan",)])

    def test_main_backfills_only_manbo_platform_when_requested(self) -> None:
        backfills = []

        def fake_upstash(command):
            if command == ["GET", sync.QUEUE_KEY]:
                return json.dumps({"manbo": ["201"], "missevan": []})
            if command == ["GET", sync.MANBO_INFO_KEY]:
                return json.dumps(
                    {
                        "version": 1,
                        "updatedAt": "remote",
                        "records": [{"dramaId": str(idx), "name": f"漫播{idx}"} for idx in range(51)],
                    },
                    ensure_ascii=False,
                )
            if command == ["GET", sync.MISSEVAN_INFO_KEY]:
                return json.dumps({str(idx): {"dramaId": idx, "title": f"猫耳{idx}"} for idx in range(101)}, ensure_ascii=False)
            if command[0] == "SET":
                return "OK"
            raise AssertionError(command)

        with (
            TemporaryDirectory() as tmp,
            patch.object(sync, "ROOT", Path(tmp)),
            patch.object(sync, "MISSEVAN_INFO_PATH", Path(tmp) / "missevan-drama-info.json"),
            patch.object(sync, "MANBO_INFO_PATH", Path(tmp) / "manbo-drama-info.json"),
            patch.object(sync, "load_env_file"),
            patch.object(sync, "upstash_request", side_effect=fake_upstash),
            patch.object(sync, "run_script"),
            patch.object(sync, "backfill_rank_metadata", side_effect=lambda platforms: backfills.append(platforms)),
        ):
            self.assertEqual(sync.main(["--backfill-ranks"]), 0)

        self.assertEqual(backfills, [("manbo",)])

    def test_main_backfills_both_platforms_when_requested(self) -> None:
        backfills = []

        def fake_upstash(command):
            if command == ["GET", sync.QUEUE_KEY]:
                return json.dumps({"manbo": ["201"], "missevan": ["101"]})
            if command == ["GET", sync.MANBO_INFO_KEY]:
                return json.dumps(
                    {
                        "version": 1,
                        "updatedAt": "remote",
                        "records": [{"dramaId": str(idx), "name": f"漫播{idx}"} for idx in range(51)],
                    },
                    ensure_ascii=False,
                )
            if command == ["GET", sync.MISSEVAN_INFO_KEY]:
                return json.dumps({str(idx): {"dramaId": idx, "title": f"猫耳{idx}"} for idx in range(101)}, ensure_ascii=False)
            if command[0] == "SET":
                return "OK"
            raise AssertionError(command)

        with (
            TemporaryDirectory() as tmp,
            patch.object(sync, "ROOT", Path(tmp)),
            patch.object(sync, "MISSEVAN_INFO_PATH", Path(tmp) / "missevan-drama-info.json"),
            patch.object(sync, "MANBO_INFO_PATH", Path(tmp) / "manbo-drama-info.json"),
            patch.object(sync, "load_env_file"),
            patch.object(sync, "upstash_request", side_effect=fake_upstash),
            patch.object(sync, "run_script"),
            patch.object(sync, "backfill_rank_metadata", side_effect=lambda platforms: backfills.append(platforms)),
        ):
            self.assertEqual(sync.main(["--backfill-ranks"]), 0)

        self.assertEqual(backfills, [("missevan", "manbo")])


if __name__ == "__main__":
    unittest.main()
