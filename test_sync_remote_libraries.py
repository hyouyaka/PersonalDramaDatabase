import unittest
from unittest.mock import Mock, patch

import sync_remote_libraries


class SyncRemoteLibrariesTests(unittest.TestCase):
    def test_sync_fetches_all_remote_payloads_before_writing_any_file(self) -> None:
        calls = []

        def fetch_info_payloads() -> list[tuple[object, object]]:
            calls.append("fetch_info_payloads")
            return [("missevan.json", {"93605": {"dramaId": 93605}}), ("manbo.json", {"records": []})]

        def fetch_cvid_map_payload() -> tuple[object, object]:
            calls.append("fetch_cvid_map_payload")
            return ("cvmap.json", {"CV A": {"displayName": "CV A"}})

        def write_payloads(payloads: list[tuple[object, object]]) -> None:
            calls.append(("write_payloads", payloads))

        sync_remote_libraries.sync_remote_libraries(
            fetch_info_payloads_func=fetch_info_payloads,
            fetch_cvid_map_payload_func=fetch_cvid_map_payload,
            write_payloads_func=write_payloads,
        )

        self.assertEqual(
            calls,
            [
                "fetch_info_payloads",
                "fetch_cvid_map_payload",
                (
                    "write_payloads",
                    [
                        ("missevan.json", {"93605": {"dramaId": 93605}}),
                        ("manbo.json", {"records": []}),
                        ("cvmap.json", {"CV A": {"displayName": "CV A"}}),
                    ],
                ),
            ],
        )

    def test_sync_does_not_write_when_cvid_map_fetch_fails(self) -> None:
        write_payloads = Mock()

        with self.assertRaisesRegex(RuntimeError, "bad cvmap"):
            sync_remote_libraries.sync_remote_libraries(
                fetch_info_payloads_func=Mock(return_value=[("missevan.json", {"93605": {"dramaId": 93605}})]),
                fetch_cvid_map_payload_func=Mock(side_effect=RuntimeError("bad cvmap")),
                write_payloads_func=write_payloads,
            )

        write_payloads.assert_not_called()

    def test_download_cvid_map_file_rejects_non_object_remote_payload(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "expected a JSON object"):
            sync_remote_libraries.download_cvid_map_file(
                path=Mock(),
                upstash=Mock(return_value="[]"),
            )

    def test_main_loads_env_then_syncs_remote_libraries(self) -> None:
        with (
            patch.object(sync_remote_libraries, "load_env_file") as load_env_file,
            patch.object(sync_remote_libraries, "sync_remote_libraries") as sync_remote,
        ):
            result = sync_remote_libraries.main([])

        self.assertEqual(result, 0)
        load_env_file.assert_called_once_with(sync_remote_libraries.ROOT / ".env")
        sync_remote.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
