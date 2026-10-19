from split_attestation import verify_attestation
y={"events":[{"date":"2024-08-01","ratio":2}],"history_end":"2026-01-01"}
a={"review_status":"confirmed","reviewer":"Analyst","independent_source_url":"https://example.org/filings",
"coverage_start":"2020-01-01","coverage_end":"2026-01-01",
"events":[{"date":"2024-08-01","ratio":2}]}
assert verify_attestation(y,a,"2024-06-28","2023-12-31")[0]
bad={**a,"events":[{"date":"2024-08-02","ratio":2}]}
assert not verify_attestation(y,bad,"2024-06-28","2023-12-31")[0]
bad={**a,"coverage_end":"2024-12-31"}
assert not verify_attestation(y,bad,"2024-06-28","2023-12-31")[0]
bad={**a,"review_status":"draft"}
assert not verify_attestation(y,bad,"2024-06-28","2023-12-31")[0]
print("PASS independent split attestation gates")
