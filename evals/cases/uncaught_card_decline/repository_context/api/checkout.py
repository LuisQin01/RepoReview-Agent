def create_checkout(processor, amount):
    receipt = processor.charge_card(amount)
    return {"receipt_id": receipt.id}


def healthcheck():
    return {"status": "ok"}
