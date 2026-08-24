from ebook_pipeline.core.ids import is_uuid4, new_id, utc_now


def test_uuid4_is_valid_and_unique() -> None:
    first = new_id()
    second = new_id()
    assert first != second
    assert is_uuid4(first)
    assert is_uuid4(second)


def test_utc_timestamp_is_timezone_aware() -> None:
    assert utc_now().endswith("+00:00")
