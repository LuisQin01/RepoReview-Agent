"""Diagnose GitHub token permissions for PR comment publishing."""
import os
import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError

token = os.getenv("GITHUB_TOKEN", "")
login = os.getenv("GITHUB_SUMMARY_COMMENT_AUTHOR_LOGIN", "")

print("=== Environment Variable Check ===")
print("GITHUB_TOKEN length:", len(token))
if token:
    print("GITHUB_TOKEN starts with:", token[:15] + "...")
else:
    print("GITHUB_TOKEN: (not set)")
print("GITHUB_SUMMARY_COMMENT_AUTHOR_LOGIN:", login or "(not set)")
print()

headers = {
    "Accept": "application/vnd.github+json",
    "Authorization": "Bearer " + token,
    "User-Agent": "RepoReview-Agent",
}

# Test 1: Can we read the PR?
print("=== Test 1: GET /pulls/1 (read PR) ===")
url = "https://api.github.com/repos/LuisQin01/reporeview-test/pulls/1"
try:
    resp = urlopen(Request(url, headers=headers), timeout=10)
    print("Status:", resp.status, "OK")
except HTTPError as e:
    print("Status:", e.code, e.reason)
    print("Body:", e.read().decode("utf-8")[:500])
print()

# Test 2: Can we read issue comments?
print("=== Test 2: GET /issues/1/comments (read comments) ===")
url2 = "https://api.github.com/repos/LuisQin01/reporeview-test/issues/1/comments?per_page=100&page=1"
try:
    resp2 = urlopen(Request(url2, headers=headers), timeout=10)
    print("Status:", resp2.status, "OK")
except HTTPError as e:
    print("Status:", e.code, e.reason)
    print("Body:", e.read().decode("utf-8")[:500])
print()

# Test 3: Can we POST a comment?
print("=== Test 3: POST /issues/1/comments (write comment) ===")
url3 = "https://api.github.com/repos/LuisQin01/reporeview-test/issues/1/comments"
body = json.dumps({"body": "<!-- reporeview-summary -->\n## RepoReview test"}).encode("utf-8")
post_headers = dict(headers)
post_headers["Content-Type"] = "application/json; charset=utf-8"
try:
    resp3 = urlopen(Request(url3, data=body, method="POST", headers=post_headers), timeout=10)
    result = json.loads(resp3.read().decode("utf-8"))
    print("Status:", resp3.status, "OK")
    print("Comment ID:", result.get("id"))
except HTTPError as e:
    print("Status:", e.code, e.reason)
    body = e.read().decode("utf-8")
    print("Body:", body[:800])
    print()
    print("=== Key Response Headers ===")
    for k, v in e.headers.items():
        if any(x in k.lower() for x in ["limit", "retry", "scope", "permission", "x-oauth"]):
            print("  ", k, ":", v)
