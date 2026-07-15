# Payment processor contract

`CardProcessor.charge_card(amount)` raises `CardDeclined` when the issuing bank
declines the card. Checkout endpoints must catch `CardDeclined` and return HTTP
422 with `{"code": "card_declined"}`. Propagating it is an internal-server-error
bug because clients cannot distinguish a declined card from a failed checkout.
