import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cvid_map_tools


class RemoteCombinedMapTests(unittest.TestCase):
    def test_missing_remote_and_missing_local_backup_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_backup = Path(tmp) / "missevan&manbo-cvid-map.json"
            upstash = Mock(return_value=None)

            with (
                patch.object(cvid_map_tools, "COMBINED_CVID_MAP_PATH", missing_backup),
                self.assertRaises(RuntimeError),
            ):
                cvid_map_tools.load_remote_combined_map(upstash=upstash)

        upstash.assert_called_once_with(["GET", "cvid-map:v1"])


if __name__ == "__main__":
    unittest.main()
