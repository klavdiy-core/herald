# Пустое имя темы -> ValueError до любого вызова.
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import test_forum_lock as L  # noqa: E402


class EmptyNames(L.Base):
    def test_empty_or_blank_name_refused_before_any_call(self):
        for bad in ("", "   ", "\n\t"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.bot.create_topic(bad)
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.bot.edit_topic(1, bad)
        self.assertEqual(self.api.of("createForumTopic"), [])
        self.assertEqual(self.api.of("editForumTopic"), [])


if __name__ == "__main__":
    unittest.main()
