def test_create_checkout_returns_receipt_id():
    processor = FakeProcessor()
    processor.charge_card.return_value.id = "receipt-1"

    assert create_checkout(processor, 100)["receipt_id"] == "receipt-1"


def test_healthcheck():
    assert healthcheck()["status"] == "ok"
