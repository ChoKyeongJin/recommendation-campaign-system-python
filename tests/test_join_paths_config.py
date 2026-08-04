from __future__ import annotations

import copy
import json

import pytest

import member_filters_config
from join_paths import JoinPathError, build_join_paths, load_join_paths


def test_join_paths_match_the_validated_member_target_registry() -> None:
    config = member_filters_config.load_config()

    paths = build_join_paths(config)

    cart = config["cart_targets"]
    cart_product = cart["product_join"]
    cart_path = paths["cart_to_product"]
    assert cart_path.source_table == cart["table"]
    assert cart_path.source_alias == cart["alias"]
    assert cart_path.target_table == cart_product["table"]
    assert cart_path.target_alias == cart_product["alias"]
    assert (
        cart_path.conditions[0].left,
        cart_path.conditions[0].right,
    ) == (cart_product["left"], cart_product["right"])

    purchase = config["purchase_product_target"]
    purchase_path = paths["purchase_to_product"]
    assert purchase_path.source_table == purchase["order_detail"]["table"]
    assert purchase_path.source_alias == purchase["order_detail"]["alias"]
    assert purchase_path.target_table == purchase["product"]["table"]
    assert purchase_path.target_alias == purchase["product"]["alias"]
    assert (
        purchase_path.conditions[0].left,
        purchase_path.conditions[0].right,
    ) == ("OD.PRODUCT_ID", "P.PRODUCT_ID")


def test_load_join_paths_uses_renamed_physical_bindings(tmp_path) -> None:
    config = copy.deepcopy(member_filters_config.load_config())
    cart = config["cart_targets"]
    cart.update({"table": "CART_SOURCE_V2", "alias": "SRC"})
    cart["product_join"].update(
        {
            "table": "PRODUCT_DIM_V2",
            "alias": "DIM",
            "left": "SRC.PRODUCT_FK",
            "right": "DIM.PRODUCT_PK",
        }
    )
    purchase = config["purchase_product_target"]
    purchase["order_detail"].update(
        {"table": "ORDER_LINE_V2", "alias": "LINE"}
    )
    purchase["product"].update(
        {
            "table": "PRODUCT_DIM_V3",
            "alias": "PRD",
            "join": "PRD.PRODUCT_PK = LINE.PRODUCT_FK",
        }
    )
    registry_path = tmp_path / "member_target_filters.json"
    registry_path.write_text(
        json.dumps(config, ensure_ascii=False), encoding="utf-8"
    )

    paths = load_join_paths(registry_path)

    cart_path = paths["cart_to_product"]
    assert (
        cart_path.source_table,
        cart_path.source_alias,
        cart_path.target_table,
        cart_path.target_alias,
    ) == ("CART_SOURCE_V2", "SRC", "PRODUCT_DIM_V2", "DIM")
    assert cart_path.render_join_line() == (
        "     INNER JOIN PRODUCT_DIM_V2 DIM "
        "ON SRC.PRODUCT_FK = DIM.PRODUCT_PK"
    )

    purchase_path = paths["purchase_to_product"]
    assert (
        purchase_path.source_table,
        purchase_path.source_alias,
        purchase_path.target_table,
        purchase_path.target_alias,
    ) == ("ORDER_LINE_V2", "LINE", "PRODUCT_DIM_V3", "PRD")
    assert (
        purchase_path.conditions[0].left,
        purchase_path.conditions[0].right,
    ) == ("LINE.PRODUCT_FK", "PRD.PRODUCT_PK")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing_left", "cart_targets.product_join.left"),
        ("cart_alias_mismatch", "does not match cart_targets.alias"),
        ("invalid_purchase_join", "must contain one equality"),
    ],
)
def test_join_paths_fail_closed_for_invalid_bindings(
    mutation: str, message: str
) -> None:
    config = copy.deepcopy(member_filters_config.load_config())
    if mutation == "missing_left":
        del config["cart_targets"]["product_join"]["left"]
    elif mutation == "cart_alias_mismatch":
        config["cart_targets"]["product_join"]["left"] = "OTHER.PRODUCT_ID"
    else:
        config["purchase_product_target"]["product"]["join"] = (
            "P.PRODUCT_ID = OD.PRODUCT_ID = EXTRA.PRODUCT_ID"
        )

    with pytest.raises(JoinPathError, match=message):
        build_join_paths(config)
