"""Audit original SIDI scoring inputs and locate available SEC/PIT reconstruction code.

Read-only historical Git CSV inventory. This does NOT claim historical PIT
reconstruction is complete. Outputs reproducible coverage and score validation.
"""
import csv,io,json,re,subprocess,statistics
from collections import Counter,defaultdict
from pathlib import Path
from recover_original_ingesta_history import git,col

OUT=Path("results/sidi_growth/pit_audit")
FIELDS=("revenue_growth","eps_growth","roe","debt_equity","pe","pb",
        "current_ratio","fcf_ni_ratio","fcf","market_cap",
        "fund_score","tech_score","combined_score","fund_growth",
        "fund_solidity","fund_valuation","sector")
def available(v):
    v=str(v or "").strip().lower()
    return bool(v and v not in {"nan","none","null","n/a","na","--","inf","-inf"})
def main():
    OUT.mkdir(parents=True,exist_ok=True)
    commits=git("log","--all","--format=%H","--","data/master").decode().splitlines()
    seen=set(); records=[];snapshot_rows=[];discrepancies=[]
    for sha in commits:
        paths=git("ls-tree","-r","--name-only",sha,"--","data/master").decode().splitlines()
        for path in paths:
            m=re.search(r"sp500_full_export_(\d{8})\.csv$",path)
            if not m:continue
            dt=m.group(1);day=f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}"
            blob=git("rev-parse",f"{sha}:{path}").decode().strip()
            if blob in seen:continue
            seen.add(blob)
            try:content=git("show",f"{sha}:{path}").decode("utf-8-sig")
            except UnicodeDecodeError:continue
            reader=csv.DictReader(io.StringIO(content))
            names=reader.fieldnames or []
            cols={name:col(names,(name,)) for name in FIELDS}
            ticker_col=col(names,("ticker","symbol","stock_symbol"))
            if ticker_col is None:continue
            counts=Counter();valid=0;differences=[];neutral=Counter()
            for item in reader:
                valid+=1
                for name,c in cols.items():
                    if c and available(item.get(c)):counts[name]+=1
                    elif name in FIELDS:neutral[name]+=1
                try:
                    f=float(item[cols["fund_score"]]);t=float(item[cols["tech_score"]])
                    combined=float(item[cols["combined_score"]])
                    delta=round(combined-round(.6*f+.4*t,2),4)
                    differences.append(abs(delta))
                    if abs(delta)>0.011:
                        discrepancies.append({"date":day,"ticker":item[ticker_col],"actual_score":combined,
                        "computed_from_parts":round(.6*f+.4*t,2),"delta":delta,"blob":blob[:12]})
                except (KeyError,ValueError,TypeError):pass
                if all(available(item.get(cols[x])) if cols[x] else False for x in FIELDS[:10]):
                    counts["all_10_raw_inputs"]+=1
            snapshot_rows.append({"date":day,"blob":blob[:12],"path":path,"rows":valid,
                "max_score_formula_abs_diff":max(differences) if differences else None,
                **{f"coverage_{k}_pct":round(100*counts[k]/valid,2) if valid else None for k in (*FIELDS,"all_10_raw_inputs")}})
    snapshot_rows.sort(key=lambda r:(r["date"],r["blob"]))
    if snapshot_rows:
        cols=list(snapshot_rows[0])
        with (OUT/"snapshot_input_coverage.csv").open("w",newline="") as f:
            w=csv.DictWriter(f,cols);w.writeheader();w.writerows(snapshot_rows)
    if discrepancies:
        with (OUT/"score_formula_discrepancies.csv").open("w",newline="") as f:
            w=csv.DictWriter(f,discrepancies[0]);w.writeheader();w.writerows(discrepancies)
    tracked=git("ls-files","research","modules/ingesta").decode().splitlines()
    pit=[p for p in tracked if any(k in p.lower() for k in ("pit","sec","fundament","reconstruct","history","scor"))]
    (OUT/"pit_research_file_inventory.txt").write_text("\n".join(pit)+"\n")
    summary={"unique_archived_csv_blobs":len(snapshot_rows),
     "archive_dates":sorted({x["date"] for x in snapshot_rows}),
     "score_formula_discrepancies":len(discrepancies),
     "suspected_pit_related_code":pit,
     "latest_snapshot":snapshot_rows[-1] if snapshot_rows else None,
     "method":"Coverage of original archived CSV inputs; score check 0.6*fund +0.4*tech.",
     "not_yet_validated":["Historical SEC inputs available as of each signal timestamp",
      "Historical market caps and sector medians","Full price technical indicator parity",
      "Constituent membership and delisted firms","Reconstructed historical scores versus archived score components"]}
    (OUT/"summary.json").write_text(json.dumps(summary,indent=2))
    print(json.dumps({"snapshots":len(snapshot_rows),"days":len(summary["archive_dates"]),
                      "formula_discrepancies":len(discrepancies),"pit_files":pit},indent=2))
if __name__=="__main__":main()
