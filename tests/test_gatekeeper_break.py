"""Second review of herald/gatekeeper.py (a bot without a model that keeps a group's membership equal to groups.json): break tests, red first.
Everything runs against a fake Bot and a temp folder: no network, no token.
  D1 two entries with the same chat_id in groups.json must be refused (today the second silently replaces the first: a typo widens or narrows a group)
  D2 a groups.json without any owner must be refused (an owner who rejoins would be banned from their own group: 'owners are never banned' needs an owner)
  J1 a join request without a `from` user must not become a decline/approve with user_id None (the API call would carry null); a log action is fine
  J2 a chat_member update with a joined status and no user id gives no ban
  B1 a ban that FAILS (Telegram error) is not silently final: the same user is banned again on a later poll, or the process stops with a non-zero exit
  L1 the list is read again: a person removed from groups.json is banned when they join afterwards, without restarting the gatekeeper
  C1..C3 controls: an owner is never banned; an unlisted joiner is banned; a listed joiner is left alone; a join request of a listed user is approved
Run from the repo root: python -X utf8 tests/test_gatekeeper_break.py [repo-root]"""
import json, os, sys, tempfile, unittest
from unittest import mock

ROOT = sys.argv[1] if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]) else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]):
    sys.argv.pop(1)
sys.path.insert(0, ROOT)
from herald import gatekeeper as gk  # noqa: E402
from herald import telegram  # noqa: E402

CHAT, OWNER, MEMBER, STRANGER = -1001, 11, 22, 33


def groups(members=(MEMBER,), owners=(OWNER,)):
    return gk.Groups.from_dict({"groups": [{"chat_id": CHAT, "name": "g", "owners": list(owners), "members": list(members), "bots": []}]})


def joined(user, chat=CHAT):
    return {"update_id": 1, "chat_member": {"chat": {"id": chat}, "new_chat_member": {"status": "member", "user": {"id": user}}}}


class Decide(unittest.TestCase):
    def test_C1_owner_is_never_banned(self):
        self.assertEqual(gk.decide(joined(OWNER), groups(members=())), [])

    def test_C2_unlisted_joiner_is_banned_and_listed_is_left(self):
        self.assertEqual([a.kind for a in gk.decide(joined(STRANGER), groups())], ["ban"])
        self.assertEqual(gk.decide(joined(MEMBER), groups()), [])

    def test_C3_listed_join_request_is_approved(self):
        up = {"update_id": 2, "chat_join_request": {"chat": {"id": CHAT}, "from": {"id": MEMBER}}}
        self.assertEqual([a.kind for a in gk.decide(up, groups())], ["approve"])

    def test_J1_join_request_without_a_user_is_not_acted_on(self):
        up = {"update_id": 3, "chat_join_request": {"chat": {"id": CHAT}}}
        acts = gk.decide(up, groups())
        self.assertFalse([a for a in acts if a.kind in ("approve", "decline", "ban") and a.user_id is None],
                         "J1: an action with user_id None: %s" % [(a.kind, a.user_id) for a in acts])

    def test_J2_joined_status_without_a_user_gives_no_ban(self):
        up = {"update_id": 4, "chat_member": {"chat": {"id": CHAT}, "new_chat_member": {"status": "member"}}}
        self.assertFalse([a for a in gk.decide(up, groups()) if a.kind == "ban"])


class Config(unittest.TestCase):
    def test_D1_duplicate_chat_id_is_refused(self):
        with self.assertRaises(ValueError, msg="D1: two entries with the same chat_id were accepted"):
            gk.Groups.from_dict({"groups": [
                {"chat_id": CHAT, "name": "a", "owners": [OWNER], "members": [MEMBER]}, {"chat_id": CHAT, "name": "b", "owners": [OWNER], "members": []}]})

    def test_D2_no_owner_is_refused(self):
        for data in ({"groups": [{"chat_id": CHAT, "members": [MEMBER]}]}, {"groups": [{"chat_id": CHAT, "owners": [], "members": [MEMBER]}]}):
            with self.assertRaises(ValueError, msg="D2: a groups.json without an owner was accepted"):
                gk.Groups.from_dict(data)


class UnionSchema(unittest.TestCase):
    """one groups.json for the gatekeeper and herald: top {her_id, groups}, a group {chat_id, name, kind, owners, peer_bots, persona, notes, members, bots}
    allowed in a group = her_id + the group's owners + members + bots + peer_bots; her_id is never banned in a listed group"""
    HER, BOT, PEER, OTHER_OWNER, CHAT2 = 5005, 777, 778, 12, -1002

    def g(self):
        return gk.Groups.from_dict({"her_id": self.HER, "groups": [
            {"chat_id": CHAT, "name": "a", "kind": "train", "owners": [OWNER], "members": [MEMBER], "bots": [self.BOT], "peer_bots": [self.PEER], "persona": "", "notes": ""},
            {"chat_id": self.CHAT2, "name": "b", "kind": "train", "owners": [self.OTHER_OWNER], "members": [], "bots": [], "peer_bots": []}]})

    def banned(self, user, chat=CHAT):
        return [a for a in gk.decide(joined(user, chat), self.g()) if a.kind == "ban"]

    def test_W0_control_a_stranger_is_banned_and_listed_are_not(self):
        self.assertTrue(self.banned(STRANGER), "W0: a stranger was not banned under the union schema")
        for u in (OWNER, MEMBER):
            self.assertFalse(self.banned(u), "W0: listed user %s was banned" % u)

    def test_W1_her_id_is_never_banned_in_a_listed_group(self):
        for chat in (CHAT, self.CHAT2):
            self.assertFalse(self.banned(self.HER, chat), "W1: her_id was banned in %s" % chat)

    def test_W2_bots_and_peer_bots_are_allowed(self):
        for u in (self.BOT, self.PEER):
            self.assertFalse(self.banned(u), "W2: a listed bot %s was banned at the door" % u)
        self.assertTrue(self.banned(self.PEER, self.CHAT2), "W2: a bot of group A is allowed into group B")

    def test_W3_an_owner_is_the_owner_of_their_group_only(self):
        self.assertFalse(self.banned(self.OTHER_OWNER, self.CHAT2), "W3 control: the owner of B was banned in B")
        self.assertTrue(self.banned(self.OTHER_OWNER, CHAT), "W3: the owner of B is let into A")
        self.assertTrue(self.banned(OWNER, self.CHAT2), "W3: the owner of A is let into B")

    def test_W4_a_top_level_owners_key_is_refused(self):
        with self.assertRaises(ValueError, msg="W4: the old top-level 'owners' was accepted (a leftover that nobody reads)"):
            gk.Groups.from_dict({"owners": [OWNER], "groups": [{"chat_id": CHAT, "name": "g", "owners": [OWNER], "members": []}]})


class Loop(unittest.TestCase):
    """main() with a fake Bot: scripted getUpdates answers, the call log is checked"""

    def run_main(self, script, groups_after=None, fail_first_ban=False, polls=4):
        d = tempfile.mkdtemp(prefix="gk_")
        gp, lp = os.path.join(d, "groups.json"), os.path.join(d, "gk.log")
        json.dump({"groups": [{"chat_id": CHAT, "name": "g", "owners": [OWNER], "members": [MEMBER], "bots": []}]}, open(gp, "w"))
        calls, state = [], {"n": 0, "failed": False}

        class FakeBot:
            def __init__(self, *a, **k):
                pass

            def _call(self, method, params=None, files=None, timeout=None):
                if method == "getUpdates":
                    state["n"] += 1
                    if state["n"] == 2 and groups_after is not None:
                        json.dump(groups_after, open(gp, "w"))
                    if state["n"] > polls:
                        raise SystemExit(0)
                    return script.get(state["n"], [])
                calls.append((method, dict(params or {})))
                if method == "banChatMember" and fail_first_ban and not state["failed"]:
                    state["failed"] = True
                    raise telegram.TelegramError("banChatMember: 400 not enough rights")
                return True

        env = {"HERALD_GATEKEEPER_TOKEN": "x"}
        rc = None
        with mock.patch.dict(os.environ, env), mock.patch.object(gk.telegram, "Bot", FakeBot), mock.patch.object(gk.time, "sleep", lambda s: None):
            try:
                rc = gk.main(["--groups", gp, "--log", lp])
            except SystemExit as e:
                rc = e.code
        return calls, rc

    def test_B1_a_failed_ban_is_retried_or_stops_loudly(self):
        calls, rc = self.run_main({1: [joined(STRANGER)]}, fail_first_ban=True)
        bans = [c for c in calls if c[0] == "banChatMember" and c[1].get("user_id") == STRANGER]
        self.assertTrue(len(bans) >= 2 or rc not in (0, None), "B1: a failed ban was tried %d time(s) and the process went on (exit %r): the intruder stays" % (len(bans), rc))

    def test_L1_a_person_removed_from_the_list_is_banned_on_their_next_join(self):
        after = {"groups": [{"chat_id": CHAT, "name": "g", "owners": [OWNER], "members": [], "bots": []}]}
        calls, _ = self.run_main({3: [joined(MEMBER)]}, groups_after=after)
        self.assertTrue([c for c in calls if c[0] == "banChatMember" and c[1].get("user_id") == MEMBER],
                        "L1: the list was not re-read: a person removed from groups.json joined and was not banned")

    def test_C4_control_the_loop_bans_a_stranger_on_its_first_poll(self):
        calls, _ = self.run_main({1: [joined(STRANGER)]})
        self.assertTrue([c for c in calls if c[0] == "banChatMember" and c[1].get("user_id") == STRANGER], "C4: the loop did not ban an unlisted joiner")


if __name__ == "__main__":
    unittest.main(verbosity=1)
