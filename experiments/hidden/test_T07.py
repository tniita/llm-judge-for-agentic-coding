from decimal import Decimal

import pytest

from shoplib.importer import import_products


def test_valid_rows():
    result = import_products("sku,name,price,category\nPEN, Pen ,120,stationery\nCUP,Cup,80.5,\n")
    assert [(p.sku, p.name, p.price, p.category) for p in result.products] == [
        ("PEN", "Pen", Decimal("120"), "stationery"),
        ("CUP", "Cup", Decimal("80.5"), "general"),
    ]
    assert result.errors == []


def test_category_column_optional():
    result = import_products("sku,name,price\nPEN,Pen,120\n")
    assert result.products[0].category == "general"


def test_missing_required_column():
    with pytest.raises(ValueError):
        import_products("sku,name\nPEN,Pen\n")


def test_errors_with_line_numbers():
    csv_text = "sku,name,price\nA,Alpha,abc\nB,Beta,-1\n\nC,,10\nD,Delta,5\nD,Delta2,6\n"
    result = import_products(csv_text)
    assert [p.sku for p in result.products] == ["D"]
    assert [line for line, _ in result.errors] == [2, 3, 5, 7]
    assert all(isinstance(msg, str) and msg for _, msg in result.errors)
