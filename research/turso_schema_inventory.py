"""Read-only database schema inventory for SIDI. Exports metadata, never data rows."""
import json,os,re
from pathlib import Path
from turso_stock_radar_scores import endpoint,query,ident
def main():
    token=os.getenv("TURSO_AUTH_TOKEN")
    if not token:raise SystemExit("TURSO_AUTH_TOKEN missing")
    api=endpoint(os.getenv("TURSO_DATABASE_URL",""))
    tables=query(api,token,"SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    output=[]
    for item in tables:
        name=item.get("name","")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",name):continue
        columns=query(api,token,"PRAGMA table_info("+ident(name)+")")
        output.append({"table":name,"columns":[{"name":c["name"],"type":c.get("type","")} for c in columns]})
    path=Path("results/quality_swing/turso_schema_inventory")
    path.mkdir(parents=True,exist_ok=True)
    (path/"schema_inventory.json").write_text(json.dumps({"tables":output},indent=2),encoding="utf-8")
    print("Completed metadata-only inventory of",len(output),"tables")
if __name__=="__main__":main()
