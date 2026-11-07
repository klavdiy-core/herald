import unittest

from herald import telegram

TOKEN = "1234567:" + "a" * 35
OWN, HER, OTHER, BOT = 11, 12, 13, 900
GA, GB = -1001000000002, -1001000000003


def upd(chat, sender, text="hi", is_bot=False, uid=1):
    return {"update_id": uid, "message": {"chat": {"id": chat, "type": "supergroup", "is_forum": True},
                                          "from": {"id": sender, "is_bot": is_bot}, "text": text}}


class Groups(unittest.TestCase):
    def setUp(self):
        self.bot = telegram.Bot(TOKEN, owners={1}, her_id=HER, groups=[
            {"chat_id": GA, "owners": [OWN], "peer_bots": [BOT]},
            {"chat_id": GB, "owners": [OTHER], "peer_bots": []}])

    def heard(self, chat, sender, is_bot=False):
        return self.bot._accept(upd(chat, sender, is_bot=is_bot))

    def test_each_group_hears_its_own_people_only(self):
        self.assertIsNotNone(self.heard(GA, OWN))
        self.assertIsNone(self.heard(GB, OWN), "A's owner was heard in B")
        self.assertIsNotNone(self.heard(GB, OTHER))
        self.assertIsNone(self.heard(GA, OTHER), "B's owner was heard in A")

    def test_her_is_heard_in_every_group(self):
        self.assertIsNotNone(self.heard(GA, HER))
        self.assertIsNotNone(self.heard(GB, HER))

    def test_peer_bots_are_per_group(self):
        self.assertIsNotNone(self.heard(GA, BOT, True))
        self.assertIsNone(self.heard(GB, BOT, True), "A's peer bot was heard in B")

    def test_unknown_group_and_stranger_are_ignored(self):
        self.assertIsNone(self.heard(-1009999999999, OWN))
        self.assertIsNone(self.heard(GA, 777))

    def test_topics_only_in_own_groups(self):
        with self.assertRaises(PermissionError):
            self.bot.create_topic("x", chat_id=-1009999999999)
        with self.assertRaises(PermissionError):
            self.bot.send_text(-1009999999999, "x")


if __name__ == "__main__":
    unittest.main()
