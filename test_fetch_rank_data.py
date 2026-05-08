import json
import unittest
from unittest.mock import patch

import fetch_rank_data


class RankFullStoreKeyTests(unittest.TestCase):
    def test_upload_full_ranks_writes_latest_key_only(self) -> None:
        store = {"_meta": {"updated_at": "2026-05-08T00:33:19+00:00"}}

        with (
            patch.object(fetch_rank_data, "upstash_request", return_value="OK") as request,
            patch("builtins.print"),
        ):
            fetch_rank_data.upload_full_ranks(store)

        request.assert_called_once()
        command = request.call_args.args[0]
        self.assertEqual(command[:2], ["SET", "ranks:latest"])
        self.assertEqual(json.loads(command[2]), store)

    def test_load_remote_full_ranks_reads_latest_key_only(self) -> None:
        payload = {"_meta": {"updated_at": "2026-05-08T00:33:19+00:00"}}

        with patch.object(fetch_rank_data, "upstash_request", return_value=json.dumps(payload)) as request:
            self.assertEqual(fetch_rank_data.load_remote_full_ranks(), payload)

        request.assert_called_once_with(["GET", "ranks:latest"])


if __name__ == "__main__":
    unittest.main()
