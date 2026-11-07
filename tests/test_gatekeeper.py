"""The gatekeeper: a bot without a model that only guards who is in each group listed in groups.json.
It never posts into a group; it acts (ban, approve, decline) and writes one audit line per decision.
  K1 a listed person joining a listed group -> no action
  K2 an unlisted person joining -> ban in that group (and the decision is audited)
  K3 a join request from a listed person -> approve; from an unlisted one -> decline
  K4 a listed bot is allowed; an unlisted bot is banned like a person
  K5 an update from a chat that is not in groups.json -> no action on the chat's members, one audit line
  K6 leaving, being kicked, or a status change of someone already allowed -> no action
  K7 the gatekeeper never bans an owner, even if the owner is missing from a group's member list
  K8 groups.json: a chat_id or a member id that is not an integer, or an unknown key -> load refuses with a clear error
  K9 the poller asks Telegram only for chat_member and chat_join_request updates
  K11 one groups.json with herald: her_id is never banned in any listed group, a peer_bot is allowed, the top level
      has no "owners" (refused as unknown), and a group without owners is refused
"""
import json, os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from herald import gatekeeper as G  # noqa: E402

HALL, TRAIN, STRANGER_CHAT = -1001000000001, -1001000000002, -1009999999999
OWNER, HER, ANNA, BORIS, EVE, TEAM_BOT, ODD_BOT, PEER = 11, 12, 21, 22, 66, 701, 799, 702

GROUPS = {"her_id": HER,
          "groups": [{"chat_id": HALL, "name": "hall", "kind": "hall", "owners": [OWNER], "members": [ANNA, BORIS], "bots": [TEAM_BOT]},
                     {"chat_id": TRAIN, "name": "train-1", "kind": "train", "owners": [OWNER], "members": [ANNA], "bots": [],
                      "peer_bots": [PEER], "persona": "p", "notes": "n"}]}


def member_update(chat, user, old="left", new="member", is_bot=False):
    return {"update_id": 1, "chat_member": {
        "chat": {"id": chat, "type": "supergroup"},
        "from": {"id": user, "is_bot": is_bot},
        "old_chat_member": {"status": old, "user": {"id": user, "is_bot": is_bot}},
        "new_chat_member": {"status": new, "user": {"id": user, "is_bot": is_bot}}}}


def join_request(chat, user):
    return {"update_id": 2, "chat_join_request": {"chat": {"id": chat, "type": "supergroup"},
                                                   "from": {"id": user, "is_bot": False}, "user_chat_id": user}}


class Decide(unittest.TestCase):
    def setUp(self):
        self.g = G.Groups.from_dict(GROUPS)

    def act(self, update):
        return [(a.kind, a.chat_id, a.user_id) for a in G.decide(update, self.g)]

    def test_K1_listed_person_joins(self):
        self.assertEqual(self.act(member_update(HALL, ANNA)), [])

    def test_K2_unlisted_person_is_banned(self):
        self.assertEqual(self.act(member_update(HALL, EVE)), [("ban", HALL, EVE)])
        self.assertEqual(self.act(member_update(TRAIN, BORIS)), [("ban", TRAIN, BORIS)], "listed in the hall, not in this train")

    def test_K3_join_requests(self):
        self.assertEqual(self.act(join_request(TRAIN, ANNA)), [("approve", TRAIN, ANNA)])
        self.assertEqual(self.act(join_request(TRAIN, EVE)), [("decline", TRAIN, EVE)])

    def test_K4_bots(self):
        self.assertEqual(self.act(member_update(HALL, TEAM_BOT, is_bot=True)), [])
        self.assertEqual(self.act(member_update(HALL, ODD_BOT, is_bot=True)), [("ban", HALL, ODD_BOT)])
        self.assertEqual(self.act(member_update(TRAIN, TEAM_BOT, is_bot=True)), [("ban", TRAIN, TEAM_BOT)], "a bot listed only in the hall")

    def test_K5_unknown_chat(self):
        acts = G.decide(member_update(STRANGER_CHAT, EVE), self.g)
        self.assertEqual([a.kind for a in acts], ["log"], "an unknown chat gets one audit line and nothing else")

    def test_K6_no_action_on_leave_or_kick_or_promotion(self):
        self.assertEqual(self.act(member_update(HALL, EVE, old="member", new="left")), [])
        self.assertEqual(self.act(member_update(HALL, EVE, old="member", new="kicked")), [])
        self.assertEqual(self.act(member_update(HALL, ANNA, old="member", new="administrator")), [])

    def test_K11_her_and_peer_bots_and_the_one_schema(self):
        self.assertEqual(self.act(member_update(TRAIN, HER)), [], "her_id is listed nowhere as a member and still never banned")
        self.assertEqual(self.act(join_request(HALL, HER)), [("approve", HALL, HER)])
        self.assertEqual(self.act(member_update(TRAIN, PEER, is_bot=True)), [], "a peer_bot of the group is allowed")
        self.assertEqual(self.act(member_update(HALL, PEER, is_bot=True)), [("ban", HALL, PEER)], "a peer_bot of another group is not")
        with self.assertRaises(ValueError, msg="top-level owners is the old schema"):
            G.Groups.from_dict({"owners": [OWNER], "groups": []})
        with self.assertRaises(ValueError, msg="a group without owners"):
            G.Groups.from_dict({"her_id": HER, "groups": [{"chat_id": HALL, "name": "h", "owners": [], "members": [ANNA]}]})
        with self.assertRaises(ValueError, msg="her_id must be a number"):
            G.Groups.from_dict({"her_id": "@her", "groups": []})

    def test_K7_owner_is_never_banned(self):
        self.assertEqual(self.act(member_update(TRAIN, OWNER)), [])
        self.assertEqual(self.act(join_request(TRAIN, OWNER)), [("approve", TRAIN, OWNER)])


class Load(unittest.TestCase):
    def _load(self, data):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "groups.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f)
            return G.Groups.load(p)

    def test_K8_bad_files_refused(self):
        for bad in ({"groups": [{"chat_id": "-100123", "name": "x", "owners": [1], "members": []}]},
                    {"groups": [{"chat_id": -100123, "name": "x", "owners": [1], "members": ["@anna"]}]},
                    {"groups": [{"chat_id": -100123, "name": "x", "owners": [1], "members": [True]}]},
                    {"groups": [{"chat_id": -100123, "name": "x", "owners": [1], "members": [], "color": "red"}]},
                    {"groups": [], "extra": 1}):
            with self.assertRaises(ValueError, msg=f"accepted: {bad}"):
                self._load(bad)
        self.assertEqual(len(self._load(GROUPS).groups), 2, "control: a good file loads")


class Apply(unittest.TestCase):
    def test_K10_each_action_calls_the_right_method_and_is_logged(self):
        calls, lines = [], []

        class FakeBot:
            def _call(self, method, params=None, **kw):
                calls.append((method, params))

        bot = FakeBot()
        for kind, method in (("ban", "banChatMember"), ("approve", "approveChatJoinRequest"),
                             ("decline", "declineChatJoinRequest")):
            G.apply(G.Action(kind, TRAIN, EVE, "t"), bot, lines.append)
            self.assertEqual(calls[-1][0], method)
            self.assertEqual((calls[-1][1]["chat_id"], calls[-1][1]["user_id"]), (TRAIN, EVE))
        self.assertIs(calls[0][1].get("revoke_messages"), False, "a ban must not wipe the person's messages")
        n = len(calls)
        G.apply(G.Action("log", STRANGER_CHAT, EVE, "t"), bot, lines.append)
        self.assertEqual(len(calls), n, "a log action calls Telegram")
        self.assertEqual(len(lines), 4, "every decision is one audit line")


class Poll(unittest.TestCase):
    def test_K9_allowed_updates_are_narrow(self):
        self.assertEqual(sorted(G.ALLOWED_UPDATES), ["chat_join_request", "chat_member"])


if __name__ == "__main__":
    unittest.main()
