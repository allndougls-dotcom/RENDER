from turso_stock_radar_scores import export,endpoint
assert endpoint("libsql://example.turso.io")=="https://example.turso.io/v2/pipeline"
def mock(sql):
    if "sqlite_master" in sql:return [{"name":"stock_snapshots"},{"name":"users"}]
    if "table_info" in sql:
        if "stock_snapshots" in sql:
            return [{"name":x} for x in ("ticker","created_at","combined_score","fund_score","tech_score")]
        return [{"name":"email"},{"name":"password"}]
    if "stock_snapshots" in sql:
        return [{"ticker":"AAA","created_at":"2026-09-01","combined_score":"8.75","fund_score":9,"tech_score":8.4},
                {"ticker":"BBB","created_at":"2026-09-02","combined_score":"8.10","fund_score":8.3,"tech_score":7.8}]
    raise AssertionError(sql)
rows,coverage=export(mock,"2026-08-01","2026-10-19")
assert len(rows)==2 and len(coverage)==1
assert rows[0]["combined_score"]==8.1 or rows[0]["combined_score"]==8.75
assert set(rows[0])=={"source_table","ticker","date","combined_score","fund_score","tech_score"}
print("PASS read-only score extraction and table guardrails")
