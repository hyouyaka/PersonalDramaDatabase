import unittest

import commen_tasks_gui


class CommandBuilderTests(unittest.TestCase):
    def test_build_sync_remote_libraries_command_downloads_only(self) -> None:
        self.assertEqual(
            commen_tasks_gui.build_sync_remote_libraries_command(),
            [commen_tasks_gui.PYTHON_EXE, "sync_remote_libraries.py"],
        )


if __name__ == "__main__":
    unittest.main()
