"""Read-only GitHub Actions history inventory (August 2026 onward)."""
import csv,json,os,urllib.request,urllib.error,time,random
from datetime import datetime,timezone
from pathlib import Path
ROOT="https://api.github.com/repos/"+os.environ["GITHUB_REPOSITORY"]
TOKEN=os.environ["GH_TOKEN"]
def get(path):
    req=urllib.request.Request(ROOT+path,headers={"Authorization":"Bearer "+TOKEN,"Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req,timeout=25) as res:return json.load(res)
        except urllib.error.HTTPError as err:
            if err.code not in (429,500,502,503,504) or attempt==5:raise
            wait=min(30,2**attempt)+random.random()
            print("GitHub temporary HTTP",err.code,"retrying",path,"attempt",attempt+1,flush=True)
            time.sleep(wait)
        except (TimeoutError,urllib.error.URLError):
            if attempt==5:raise
            time.sleep(min(30,2**attempt)+random.random())
def main():
    base=Path("results/quality_swing/main_ingesta_history")
    base.mkdir(parents=True,exist_ok=True)
    workflows=get("/actions/workflows?per_page=100").get("workflows",[])
    runs=[]
    for page in range(1,21):
        page_runs=get("/actions/runs?per_page=100&page="+str(page)).get("workflow_runs",[])
        if not page_runs:break
        for r in page_runs:
            if r.get("created_at","")[:10]<"2026-08-01":continue
            runs.append(r)
        if page_runs[-1].get("created_at","")[:10]<"2026-08-01":break
    output=[]
    for r in runs:
        run_id=r["id"]
        try:artifacts=get("/actions/runs/"+str(run_id)+"/artifacts?per_page=100").get("artifacts",[])
        except Exception as err:
            print("Could not inspect artifacts for run",run_id,type(err).__name__,flush=True)
            artifacts=[]
        workflow_name=r.get("name","")
        match=("ingest" in workflow_name.lower() or "ingesta" in workflow_name.lower())
        if not match and not artifacts:continue
        if not artifacts:
            output.append({"run_id":run_id,"date":r.get("created_at"),"workflow":workflow_name,"status":r.get("conclusion"),
                           "artifact":"","artifact_id":"","expired":"","run_url":r.get("html_url")})
        for a in artifacts:
            output.append({"run_id":run_id,"date":r.get("created_at"),"workflow":workflow_name,"status":r.get("conclusion"),
                           "artifact":a.get("name"),"artifact_id":a.get("id"),"expired":a.get("expired"),"run_url":r.get("html_url")})
    keys=["run_id","date","workflow","status","artifact","artifact_id","expired","run_url"]
    with (base/"runs_and_artifacts.csv").open("w",newline="",encoding="utf8") as f:
        writer=csv.DictWriter(f,fieldnames=keys);writer.writeheader();writer.writerows(output)
    summary={"runs_checked":len(runs),"entries":len(output),"workflows":[{"name":w.get("name"),"path":w.get("path"),"id":w.get("id")} for w in workflows]}
    (base/"workflows.json").write_text(json.dumps(summary,indent=2),encoding="utf8")
    print("History inventory complete; scanned",len(runs),"runs, indexed",len(output),"entries.")
if __name__=="__main__":main()
