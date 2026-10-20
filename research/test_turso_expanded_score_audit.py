from turso_expanded_score_audit import extract
def run(sql):
 if "sqlite_master" in sql:return [{"name":"stock_snapshot"},{"name":"radar_events"}]
 if "table_info" in sql:
  if "stock_snapshot" in sql:return [{"name":k} for k in ("ticker","created_at","combined_score","fund_score","tech_score")]
  return [{"name":k} for k in ("payload","created_at")]
 if "stock_snapshot" in sql:return [{"ticker":"CF","dt":"2026-09-30","score":8.42,"fund":8.94,"tech":None}]
 if "radar_events" in sql:return [{"ticker":"ABC","dt":"2026-08-21","score":8.7,"fund":8.9,"tech":8.4}]
 raise AssertionError(sql)
rows,audit=extract(run,"2026-08-01","2026-10-20")
assert len(rows)==2 and sum(x["combined_score"]>8.5 for x in rows)==1
assert all(x["status"]=="ok" for x in audit)
assert any("json_valid" in " " for _ in []) is False
print("PASS scalar+JSON Turso audit mock")
