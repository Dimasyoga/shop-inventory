"""BotPoller._cycle tests: config gating, token hot-swap, poison updates."""
import database
from telegram_bot import BotPoller


class RecordingAPI:
    def __init__(self, token):
        self.token = token
        self.updates_batches = []
        self.get_updates_calls = []

    def get_updates(self, offset=None, timeout=25):
        self.get_updates_calls.append(offset)
        return self.updates_batches.pop(0) if self.updates_batches else []

    def send_message(self, *a, **k):
        pass

    def answer_callback_query(self, *a, **k):
        pass

    def edit_message_text(self, *a, **k):
        pass


def save_settings(**kv):
    db = database.get_db()
    for k, v in kv.items():
        database.set_setting(db, k, v)
    db.commit()
    db.close()


def make_poller():
    apis = []

    def api_factory(token):
        api = RecordingAPI(token)
        apis.append(api)
        return api

    poller = BotPoller(api_factory=api_factory, sleep=lambda s: None)
    return poller, apis


def test_disabled_bot_makes_no_api_calls(db_path):
    save_settings(telegram_enabled="0", telegram_bot_token="123:ABC")
    poller, apis = make_poller()
    poller._cycle()
    assert apis == []


def test_missing_token_makes_no_api_calls(db_path):
    save_settings(telegram_enabled="1", telegram_bot_token="")
    poller, apis = make_poller()
    poller._cycle()
    assert apis == []


def test_token_change_resets_offset_and_rebuilds_api(db_path):
    save_settings(telegram_enabled="1", telegram_bot_token="TOKEN-A",
                  telegram_whitelist="111")
    poller, apis = make_poller()
    apis_batch = [{"update_id": 7, "message": {"from": {"id": 111}, "chat": {"id": 1}, "text": "hi"}}]

    poller._cycle()
    assert len(apis) == 1 and apis[0].token == "TOKEN-A"
    apis[0].updates_batches.append(apis_batch)
    poller._cycle()
    assert poller._offset == 8  # advanced past update 7

    save_settings(telegram_bot_token="TOKEN-B")
    poller._cycle()
    assert len(apis) == 2 and apis[1].token == "TOKEN-B"
    assert apis[1].get_updates_calls[0] is None  # offset reset for the new bot


def test_offset_advances_past_poison_update(db_path, monkeypatch):
    save_settings(telegram_enabled="1", telegram_bot_token="123:ABC", telegram_whitelist="111")
    poller, apis = make_poller()
    poller._cycle()  # creates api
    apis[0].updates_batches.append([
        {"update_id": 41, "message": {"from": {"id": 111}, "chat": {"id": 1}, "text": "boom"}},
        {"update_id": 42, "message": {"from": {"id": 111}, "chat": {"id": 1}, "text": "ok"}},
    ])

    import telegram_bot as tb
    calls = []
    real_handle = tb.handle_update

    def exploding_handle(api, db, update, whitelist, tz, states):
        calls.append(update["update_id"])
        if update["update_id"] == 41:
            raise RuntimeError("poison")
        return real_handle(api, db, update, whitelist, tz, states)

    monkeypatch.setattr(tb, "handle_update", exploding_handle)
    poller._cycle()  # must not raise, must process 42
    assert calls == [41, 42]
    assert poller._offset == 43
