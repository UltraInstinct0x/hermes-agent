"""A notice pinned to a lease that died must still reach the chat through the current live owner."""


def _owners(tmp_path):
    home = str(tmp_path.resolve())
    old = dict(profile_home=home, session_id="chat", lease_id="old-lease", live_session_id="old-live")
    new = dict(profile_home=home, session_id="chat", lease_id="new-lease", live_session_id="new-live")
    return old, new


def test_notice_pinned_to_a_dead_lease_is_adopted_by_the_new_live_owner(tmp_path):
    from tools import bot_live_delivery as mailbox

    old, new = _owners(tmp_path)
    notice_id, turn_id = "4" * 32, "5" * 32
    mailbox.deliver_to_live_owner(tmp_path, old, "good morning", delivery_id=notice_id, notice={"delivery_id": notice_id})
    mailbox.deliver_to_live_owner(tmp_path, old, "a turn input", delivery_id=turn_id)

    assert mailbox.claim_pending_delivery(tmp_path, new) is None  # a new lease still cannot steal by claiming
    assert mailbox.adopt_orphaned_notices(tmp_path, new) == 1  # only the notice moves
    assert mailbox.adopt_orphaned_notices(tmp_path, new) == 0  # and only once

    claimed = mailbox.claim_pending_delivery(tmp_path, new)
    assert claimed is not None and claimed["message"] == "good morning"
    assert claimed["adopted_from"] == {"lease_id": "old-lease", "live_session_id": "old-live"}
    assert mailbox.read_delivery_result(tmp_path, turn_id)["status"] == "queued"  # a turn input stays with its lease
    assert mailbox.claim_pending_delivery(tmp_path, new) is None


def test_a_claimed_notice_is_never_adopted_again(tmp_path):
    """Its row may already be posted: moving it would post it twice."""
    from tools import bot_live_delivery as mailbox

    old, new = _owners(tmp_path)
    delivery_id = "6" * 32
    mailbox.deliver_to_live_owner(tmp_path, old, "posted already", delivery_id=delivery_id, notice={"delivery_id": delivery_id})
    assert mailbox.claim_pending_delivery(tmp_path, old)["status"] == "claimed"

    assert mailbox.adopt_orphaned_notices(tmp_path, new) == 0
    assert mailbox.read_delivery_result(tmp_path, delivery_id)["status"] == "claimed"


def test_the_owner_that_already_holds_a_notice_keeps_it(tmp_path):
    from tools import bot_live_delivery as mailbox

    old, _ = _owners(tmp_path)
    delivery_id = "7" * 32
    mailbox.deliver_to_live_owner(tmp_path, old, "mine", delivery_id=delivery_id, notice={"delivery_id": delivery_id})

    assert mailbox.adopt_orphaned_notices(tmp_path, old) == 0
    assert mailbox.claim_pending_delivery(tmp_path, old)["message"] == "mine"
