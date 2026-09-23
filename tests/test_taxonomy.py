"""One case per bucket, built through the real scorer so the two cannot drift."""

from __future__ import annotations

import csv

import pytest

from src.metrics import score_step
from src.prompts import parse_completion
from src.taxonomy import BUCKETS, bucket_table, check_labels, classify, neighbours
from src.trajectories import Action, ToolCall

CATALOG = ["cancel_pending_order", "cancel_order", "get_order_details", "calculate"]
REF = Action(
    kind="tool_call",
    calls=(ToolCall("cancel_pending_order", {"order_id": "#W1", "reason": "ordered by mistake"}),),
)


def _row(completion, reference=REF):
    return score_step(reference, parse_completion(completion), CATALOG).as_dict()


def _call(name, **args):
    import json

    return f'<tool_call>\n{{"name": "{name}", "arguments": {json.dumps(args)}}}\n</tool_call>'


@pytest.mark.parametrize(
    ("completion", "bucket"),
    [
        (_call("cancel_pending_order", order_id="#W1", reason="ordered by mistake"), None),
        (_call("made_up_tool"), "hallucinated_tool"),
        (_call("cancel_order", order_id="#W1"), "wrong_tool_neighbor"),
        (_call("calculate", expression="1+1"), "wrong_tool_unrelated"),
        (
            _call("cancel_pending_order", order_id="#W9", reason="ordered by mistake"),
            "right_tool_wrong_args",
        ),
        ("I've cancelled it for you.", "premature_termination"),
        ("<tool_call>\nnot json\n</tool_call>", "malformed_output"),
    ],
)
def test_each_tool_call_failure_lands_in_one_bucket(completion, bucket):
    assert classify(_row(completion)) == bucket


def test_reply_steps_are_outside_the_five_buckets():
    ref = Action(kind="reply", text="What is your email?")
    assert classify(_row("Could I have your email?", ref)) is None
    assert classify(_row(_call("get_order_details"), ref)) == "call_instead_of_reply"


def test_neighbours_share_a_content_word_not_a_connector():
    assert neighbours("get_order_details", "get_product_details")
    assert neighbours("find_user_id_by_email", "users_lookupByEmail")
    assert not neighbours("get_order_details", "calculate")
    assert not neighbours("find_user_id_by_zip", "transfer_to_human_agents")  # only "to"/"id"


def test_bucket_shares_are_over_wrong_tool_call_steps():
    rows = [
        _row(_call("made_up_tool")),
        _row(_call("calculate")),
        _row("done"),
        _row(_call("cancel_pending_order", order_id="#W1", reason="ordered by mistake")),
    ]
    table = bucket_table(rows)
    assert table["n_wrong"] == 3
    assert table["share_of_wrong_tool_call_steps"]["hallucinated_tool"] == pytest.approx(
        1 / 3, abs=1e-4
    )
    assert set(table["share_of_wrong_tool_call_steps"]) == set(BUCKETS)


def test_label_check_scores_agreement(tmp_path):
    sheet = tmp_path / "s.csv"
    with sheet.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            ["step_id", "auto_bucket", "human_bucket", "reference_tools", "completion", "context"]
        )
        w.writerow(["1", "hallucinated_tool", "hallucinated_tool", "", "", ""])
        w.writerow(["2", "wrong_tool_neighbor", "wrong_tool_unrelated", "", "", ""])
        w.writerow(["3", "right_tool_wrong_args", "", "", "", ""])  # unlabelled: ignored
    result = check_labels(sheet)
    assert result["labelled"] == 2 and result["agreement"] == 0.5 and not result["passes_90pct"]
    assert result["disagreements"] == [("2", "wrong_tool_neighbor", "wrong_tool_unrelated")]
