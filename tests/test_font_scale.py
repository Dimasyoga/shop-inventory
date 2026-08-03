"""The text scale: stored as a percentage, rendered onto <html> on every page.

The point of the setting is that the shop owner can read the screen, so the failure
that matters most is not a rejected save -- it is a stored value the renderer chokes
on, which would leave her with a broken page and no way back to Settings to fix it.
Hence the fallback tests below carry as much weight as the round trip.
"""
import database

import app as app_module


def get_setting(key):
    conn = database.get_db()
    val = database.get_setting(conn, key)
    conn.close()
    return val


def set_setting(key, value):
    conn = database.get_db()
    database.set_setting(conn, key, value)
    conn.commit()
    conn.close()


def test_scale_round_trip(client):
    res = client.post("/api/settings/font-scale", json={"scale": 130})
    assert res.status_code == 200
    assert get_setting("ui_font_scale") == "130"


def test_scale_is_rendered_onto_the_html_element(client):
    client.post("/api/settings/font-scale", json={"scale": 150})
    html = client.get("/settings").get_data(as_text=True)
    assert 'style="font-size: 150%"' in html


def test_every_page_carries_the_scale(client):
    """Not just Settings: a scale applied on one page and not the next would leave
    the shop owner switching between two sizes as she navigates."""
    client.post("/api/settings/font-scale", json={"scale": 115})
    for path in ("/", "/products", "/orders", "/restock", "/self-use", "/sales",
                 "/stock-history", "/settings"):
        html = client.get(path).get_data(as_text=True)
        assert 'style="font-size: 115%"' in html, path


def test_login_page_carries_the_scale(client):
    """It renders before a session exists, and it is the first thing to read."""
    client.post("/api/settings/font-scale", json={"scale": 130})
    with app_module.app.test_client() as anon:
        html = anon.get("/login").get_data(as_text=True)
    assert 'style="font-size: 130%"' in html


def test_scale_outside_the_offered_set_is_rejected(client):
    res = client.post("/api/settings/font-scale", json={"scale": 400})
    assert res.status_code == 400
    assert get_setting("ui_font_scale") is None


def test_non_numeric_scale_is_rejected(client):
    for bad in ("huge", None, {}, 1.5):
        res = client.post("/api/settings/font-scale", json={"scale": bad})
        assert res.status_code == 400, bad


def test_unset_scale_renders_the_default(client):
    assert get_setting("ui_font_scale") is None
    html = client.get("/settings").get_data(as_text=True)
    assert f'style="font-size: {app_module.DEFAULT_FONT_SCALE}%"' in html


def test_unreadable_stored_scale_falls_back_rather_than_breaking_the_page(client):
    """A value written by hand, by a half-finished migration, or by a version that
    offered a size this one no longer does. Rendering `font-size: banana%` would be
    ignored by the browser but `font-size: 20%` would not, and a page nobody can read
    is a page she cannot reach Settings from."""
    for bogus in ("banana", "", "20", "0", "-100", "115.5"):
        set_setting("ui_font_scale", bogus)
        html = client.get("/settings").get_data(as_text=True)
        assert f'style="font-size: {app_module.DEFAULT_FONT_SCALE}%"' in html, bogus


def test_offered_sizes_all_survive_a_round_trip(client):
    """Whatever FONT_SCALES holds is what the Settings page renders buttons for, so
    every member must be acceptable to the endpoint that those buttons call."""
    for scale in app_module.FONT_SCALES:
        assert client.post("/api/settings/font-scale", json={"scale": scale}).status_code == 200
        assert get_setting("ui_font_scale") == str(scale)
