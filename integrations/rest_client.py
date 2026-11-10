"""Minimal read-only API client; print only explicitly requested mail results."""
import argparse
import json
import os
from pathlib import Path
import sys
import urllib.parse
import urllib.request

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mailhub.config import client_bearer_token, normalize_origin


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # Do not forward a bearer token to a redirect destination.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="HTTPS service origin")
    parser.add_argument("--query", default="", help="Plain-text cached-mail search")
    args = parser.parse_args(argv)
    try:
        origin = normalize_origin(args.url, allow_loopback_http=False)
    except ValueError:
        raise SystemExit("Use an HTTPS origin without credentials, paths or query parameters.") from None
    try:
        token = client_bearer_token(os.environ)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    query = urllib.parse.urlencode({"q": args.query, "limit": 20})
    request = urllib.request.Request(origin + "/v1/messages?" + query,
                                     headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
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
