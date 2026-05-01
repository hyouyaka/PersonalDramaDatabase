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

    def test_main_downloads_remote_info_before_append_and_upload(self) -> None:
        remote_missevan = {str(idx): {"dramaId": idx, "title": f"猫耳{idx}"} for idx in range(101)}
        remote_manbo = {
            "version": 1,
            "updatedAt": "remote",
            "records": [{"dramaId": str(idx), "name": f"漫播{idx}"} for idx in range(51)],
        }

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            missevan_path = root / "missevan-drama-info.json"
            manbo_path = root / "manbo-drama-info.json"
            missevan_path.write_text('{"local": {"dramaId": "local"}}', encoding="utf-8")
            manbo_path.write_text('{"records": [{"dramaId": "local"}]}', encoding="utf-8")
            commands = []

            def fake_upstash(command):
                if command == ["GET", sync.QUEUE_KEY]:
                    return json.dumps({"manbo": ["201"], "missevan": ["101"]})
                if command == ["GET", sync.MANBO_INFO_KEY]:
                    return json.dumps(remote_manbo, ensure_ascii=False)
                if command == ["GET", sync.MISSEVAN_INFO_KEY]:
                    return json.dumps(remote_missevan, ensure_ascii=False)
                if command[0] == "SET":
                    commands.append(command)
                    return "OK"
                raise AssertionError(command)

            def fake_run_script(script_name, drama_ids):
                self.assertEqual(json.loads(missevan_path.read_text(encoding="utf-8")), remote_missevan)
                self.assertEqual(json.loads(manbo_path.read_text(encoding="utf-8")), remote_manbo)
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
        self.assertTrue(any(command[:2] == ["SET", sync.MANBO_INFO_KEY] for command in commands))
        self.assertTrue(any(command[:2] == ["SET", sync.MISSEVAN_INFO_KEY] for command in commands))

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


if __name__ == "__main__":
    unittest.main()
