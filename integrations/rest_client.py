"""Minimal read-only API client; print only explicitly requested mail results."""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="HTTPS service origin")
    parser.add_argument("--query", default="", help="Plain-text cached-mail search")
    args = parser.parse_args()
    origin = urllib.parse.urlsplit(args.url)
    if (origin.scheme != "https" or not origin.hostname or origin.username or origin.password
            or origin.path not in ("", "/") or origin.query or origin.fragment):
        raise SystemExit("Use an HTTPS origin without credentials, paths or query parameters.")
    token = os.environ.get("MAILHUB_MCP_TOKEN", "")
    if not token or any(char.isspace() for char in token):
        raise SystemExit("Set MAILHUB_MCP_TOKEN in the process environment.")
    query = urllib.parse.urlencode({"q": args.query, "limit": 20})
    request = urllib.request.Request(args.url.rstrip("/") + "/v1/messages?" + query,
                                     headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    # No automatic redirect: avoid sending authenticated requests elsewhere.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=30) as response:
            result = json.load(response)
    except Exception:
        print("MailHarbor API request failed. Check the service and token.", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
