"""Unit tests for the i18n helper."""
import i18n


def test_normalize_lang():
    assert i18n.normalize_lang("id") == "id"
    assert i18n.normalize_lang("en") == "en"
    assert i18n.normalize_lang("fr") == i18n.DEFAULT_LANG   # unsupported -> default
    assert i18n.normalize_lang(None) == i18n.DEFAULT_LANG
    assert i18n.normalize_lang("") == i18n.DEFAULT_LANG


def test_english_is_identity():
    t = i18n.make_t("en")
    assert t("Dashboard") == "Dashboard"
    assert t("some string never translated") == "some string never translated"


def test_indonesian_translates_known_keys():
    t = i18n.make_t("id")
    assert t("Dashboard") == "Dasbor"
    assert t("Settings") == "Pengaturan"


def test_missing_key_falls_back_to_source():
    t = i18n.make_t("id")
    assert t("brand new untranslated phrase") == "brand new untranslated phrase"


def test_placeholder_formatting():
    t_en = i18n.make_t("en")
    t_id = i18n.make_t("id")
    assert t_en("Order #{n}", n=5) == "Order #5"
    assert t_id("Order #{n}", n=5) == "Pesanan #5"


def test_js_table_english_is_empty():
    assert i18n.js_table("en") == {}
    assert i18n.js_table("id")["Dashboard"] == "Dasbor"


def test_every_id_value_is_a_string():
    # Guards against typos that would break str.format at render time.
    for src, dst in i18n.TRANSLATIONS["id"].items():
        assert isinstance(dst, str), src


def test_translator_exposes_resolved_language():
    assert i18n.make_t("id").lang == "id"
    assert i18n.make_t("fr").lang == "en"  # unsupported -> default


def test_month_name():
    assert i18n.month_name(7, "en") == "July"
    assert i18n.month_name(7, "id") == "Juli"
    assert i18n.month_name(7, "en", abbr=True) == "Jul"
    assert i18n.month_name(8, "id", abbr=True) == "Agu"
    assert i18n.month_name(1, "id") == "Januari"
    assert i18n.month_name(12, "id") == "Desember"


def test_weekday_abbr():
    assert i18n.weekday_abbr(0, "en") == "Mon"   # Monday = 0
    assert i18n.weekday_abbr(0, "id") == "Sen"
    assert i18n.weekday_abbr(6, "id") == "Min"   # Sunday


def test_calendar_tables_are_complete():
    for lang in i18n.LANGUAGES:
        assert len(i18n.MONTHS[lang]) == 12
        assert len(i18n.MONTHS_ABBR[lang]) == 12
        assert len(i18n.WEEKDAYS_ABBR[lang]) == 7
