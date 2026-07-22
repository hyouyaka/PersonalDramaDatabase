import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import commen_tasks_gui


class CommandBuilderTests(unittest.TestCase):
    def test_build_sync_remote_libraries_command_downloads_only(self) -> None:
        self.assertEqual(
            commen_tasks_gui.build_sync_remote_libraries_command(),
            [commen_tasks_gui.PYTHON_EXE, "sync_remote_libraries.py"],
        )


class UpstashEditorStateTests(unittest.TestCase):
    def test_collection_switch_clears_previous_edit_target(self) -> None:
        page = SimpleNamespace(
            current_identity="old-id",
            current_collection_index=0,
            detail_view=Mock(),
            apply_button=Mock(),
            refresh_items=Mock(),
        )

        commen_tasks_gui.UpstashEditorPage.on_collection_changed(page, 1)

        self.assertIsNone(page.current_identity)
        self.assertIsNone(page.current_collection_index)
        page.detail_view.clear.assert_called_once_with()
        page.apply_button.setText.assert_called_once_with("应用条目修改")
        page.refresh_items.assert_called_once_with()

    def test_failed_resource_switch_restores_loaded_key(self) -> None:
        key_box = Mock()
        key_box.findData.return_value = 2
        page = SimpleNamespace(
            worker=SimpleNamespace(operation="load"),
            loaded=SimpleNamespace(spec=SimpleNamespace(key="ranks:latest")),
            key_box=key_box,
            resource_status=Mock(),
        )

        with patch.object(commen_tasks_gui.QMessageBox, "critical"):
            commen_tasks_gui.UpstashEditorPage.on_worker_error(page, "network failed")

        key_box.setCurrentIndex.assert_called_once_with(2)
        status = page.resource_status.setText.call_args.args[0]
        self.assertIn("继续显示原资源 ranks:latest", status)


if __name__ == "__main__":
    unittest.main()
