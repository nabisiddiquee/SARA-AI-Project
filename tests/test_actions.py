from my_python_project.actions import _extract_name, _has_kw, is_hindi


def test_has_kw_requires_word_boundary_for_ascii():
    # The reported false-positive: short keywords must not match inside words.
    assert not _has_kw("this is history", ["hi"])
    assert not _has_kw("the machine works", ["hi"])
    assert _has_kw("hi there", ["hi"])
    assert _has_kw("say hi", ["hi"])


def test_has_kw_matches_multiword_keyword():
    assert _has_kw("please open youtube now", ["open youtube"])
    assert not _has_kw("open the door", ["open youtube"])


def test_has_kw_non_ascii_substring():
    # Devanagari keyword matches even when inflected/attached.
    assert _has_kw("कैमरा खोलो", ["खोल"])


def test_extract_name_preserves_case():
    name = _extract_name("create file Report.txt", ["create file", "make file"])
    assert name == "Report.txt"


def test_extract_name_no_keyword():
    assert _extract_name("Report.txt", ["create file"]) == ""


def test_is_hindi_devanagari():
    assert is_hindi("नमस्ते")
    assert is_hindi("camera kholo")
    assert not is_hindi("open the camera")
