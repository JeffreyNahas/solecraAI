from app.config import Settings
from app.whatsapp.buttons import approval_buttons, parse_button_id
from app.whatsapp.parse import parse_webhook
from app.whatsapp.signature import verify_signature
from tests.helpers import APP_SECRET, load_fixture, sign


def test_signature_valid_and_invalid():
    body = b'{"a":1}'
    assert verify_signature(APP_SECRET, body, sign(body))
    assert not verify_signature(APP_SECRET, body, sign(body, "other-secret"))
    assert not verify_signature(APP_SECRET, body + b" ", sign(body))
    assert not verify_signature(APP_SECRET, body, None)
    assert not verify_signature("", body, sign(body, ""))  # unset secret never passes


def test_parse_text():
    [msg] = parse_webhook(load_fixture("text_message.json"))
    assert (msg.message_id, msg.sender, msg.msg_type, msg.text) == (
        "wamid.TEXT0001",
        "15145550001",
        "text",
        "hello bot",
    )


def test_parse_button():
    [msg] = parse_webhook(load_fixture("button_reply.json"))
    assert msg.msg_type == "button" and msg.button_id == "approve:1:1"


def test_parse_status_only_payload_has_no_messages():
    assert parse_webhook(load_fixture("status_failed.json")) == []


def test_button_ids_roundtrip():
    ids = [bid for bid, _ in approval_buttons(42, 3)]
    assert ids == ["approve:42:3", "edit:42:3", "reject:42:3"]
    parsed = parse_button_id("reject:42:3")
    assert parsed and parsed.action == "reject" and parsed.approval_id == 42 and parsed.version == 3
    for bad in [None, "", "approve:1", "delete:1:1", "approve:x:1", "approve:1:2:3"]:
        assert parse_button_id(bad) is None


def test_owner_phones_parsing():
    s = Settings(_env_file=None, owner_phones=" +1 (514) 555-0001:Alice, 15145550002 ,")
    assert s.owners() == {"15145550001": "Alice", "15145550002": "15145550002"}
