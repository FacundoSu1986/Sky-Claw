import json

data = json.load(open("bandit_output.json", encoding="utf-8"))
for e in data.get("results", []):
    if e["issue_severity"] in ("HIGH", "MEDIUM"):
        print(
            f"{e['filename']}:{e['line_number']} - {e['test_id']} ({e['issue_severity']}) - {e['issue_text']}"
        )
