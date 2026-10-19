from sec_split_corroboration import sec_candidates
facts={"facts":{"us-gaap":{"StockholdersEquityNoteStockSplitConversionRatio1":{"units":{"pure":[
{"val":4,"end":"2024-05-01","filed":"2024-05-15","accn":"first"},
{"val":10,"end":"2024-05-01","filed":"2024-10-01","accn":"future"},
{"val":2,"end":"2025-01-01","filed":"2025-02-01","accn":"futureperiod"}
]}}}}}
values=sec_candidates(facts,"2024-06-28")
assert len(values)==1 and values[0]["ratio"]==4,values
print("PASS SEC disclosed split candidates filing cutoff")
