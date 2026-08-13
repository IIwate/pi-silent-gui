from __future__ import annotations

import unittest
from unittest.mock import patch

from tests.support import backend as _backend  # noqa: F401

import common as common_module
from common import create_session_tmp, remove_session_tmp, session_state_path, session_tmp


class SessionDirectoryAllocationTests(unittest.TestCase):
    def test_collision_retries_without_reusing_existing_state(self) -> None:
        existing_id = "a" * 12
        fresh_id = "b" * 12
        existing = session_tmp(existing_id)
        marker = existing / "owner.txt"
        marker.write_text("existing", encoding="utf-8")
        self.addCleanup(remove_session_tmp, existing_id)
        self.addCleanup(remove_session_tmp, fresh_id)

        with patch.object(
            common_module,
            "new_session_id",
            side_effect=[existing_id, fresh_id],
        ):
            session_id, created = create_session_tmp()

        self.assertEqual(session_id, fresh_id)
        self.assertEqual(created, session_tmp(fresh_id, create=False))
        self.assertEqual(marker.read_text(encoding="utf-8"), "existing")

    def test_repeated_collisions_fail_without_reusing_a_directory(self) -> None:
        existing_id = "c" * 12
        existing = session_tmp(existing_id)
        marker = existing / "owner.txt"
        marker.write_text("existing", encoding="utf-8")
        self.addCleanup(remove_session_tmp, existing_id)

        with patch.object(common_module, "new_session_id", return_value=existing_id):
            with self.assertRaisesRegex(RuntimeError, "unique session directory"):
                create_session_tmp()

        self.assertEqual(marker.read_text(encoding="utf-8"), "existing")

    def test_failed_entry_deletion_preserves_session_identity_for_retry(self) -> None:
        session_id = "d" * 12
        directory = session_tmp(session_id)
        state_path = session_state_path(session_id)
        state_path.write_text('{"session_id":"dddddddddddd"}', encoding="utf-8")
        blocked = directory / "blocked.bin"
        blocked.write_bytes(b"blocked")
        self.addCleanup(remove_session_tmp, session_id)
        delete_by_handle = common_module._delete_by_handle

        def fail_blocked(kernel32, handle, path) -> None:
            if path.name == blocked.name:
                raise OSError("injected locked entry")
            delete_by_handle(kernel32, handle, path)

        with patch.object(common_module, "_delete_by_handle", side_effect=fail_blocked):
            with self.assertRaisesRegex(OSError, "injected locked entry"):
                remove_session_tmp(session_id)

        self.assertTrue(state_path.is_file(), "cleanup failure must preserve session identity")


if __name__ == "__main__":
    unittest.main()
