"""RU billing — broadapps/YooKassa in CloudPayments format (ADR-068).

POST /v1/billing/cloudpayments/webhook — server-to-server payment callback from the broadapps
aggregator (CloudPayments wire format). Public trigger; crediting happens only after verifying
the payment via the broadapps API. Isolated from Adapty / StoreKit / BYOK: own parser, dedup
table and ledger namespace (``cp-txn:*``). Card data is never read into business logic, logged,
or persisted.

POST /v1/billing/cloudpayments/checkout — JWT/client-auth payment-link creation. ``userId`` is
taken from the authenticated subject, never the request body.
"""
