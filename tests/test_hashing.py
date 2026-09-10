"""Hashing must be stable across key order, encoding, and process runs."""

from __future__ import annotations

import pytest

from src.hashing import canonical_json, hash_file, hash_obj, short


def test_key_order_does_not_change_the_hash():
    assert hash_obj({"a": 1, "b": 2}) == hash_obj({"b": 2, "a": 1})


def test_nested_key_order_does_not_change_the_hash():
    left = {"outer": {"z": [1, 2], "a": {"q": True}}}
    right = {"outer": {"a": {"q": True}, "z": [1, 2]}}
    assert hash_obj(left) == hash_obj(right)


def test_list_order_does_change_the_hash():
    # Sequence matters for messages and tool lists; it must not be normalised away.
    assert hash_obj([1, 2]) != hash_obj([2, 1])


def test_unicode_is_literal_not_escaped():
    # ensure_ascii=False: the same string must hash identically whether it came
    # from YAML or a JSON round-trip.
    assert canonical_json({"x": "café"}) == '{"x":"café"}'


def test_type_changes_are_visible():
    """A numeric 1 and the string "1" are different arguments, so different hashes."""
    assert hash_obj({"n": 1}) != hash_obj({"n": "1"})


def test_unhashable_type_is_rejected_loudly():
    with pytest.raises(TypeError, match="not canonically hashable"):
        hash_obj({"bad": object()})


def test_hash_file_matches_content(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text('{"x":1}')
    b.write_text('{"x":1}')
    assert hash_file(a) == hash_file(b)
    b.write_text('{"x":2}')
    assert hash_file(a) != hash_file(b)


def test_short_is_truncation_only():
    digest = hash_obj({"a": 1})
    assert digest.startswith(short(digest))
