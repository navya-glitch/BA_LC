"""
ba_integrations.py - Confluence (read-only) and Jira (create issues) REST clients.
No Streamlit. All settings are passed in from the UI; nothing is hardcoded.
"""
import re
import requests


class IntegrationError(Exception):
    pass


def _session(auth_type, email, token):
    s = requests.Session()
    if auth_type.startswith("Cloud"):
        s.auth = (email, token)
    else:
        s.headers["Authorization"] = f"Bearer {token}"
    s.headers["Accept"] = "application/json"
    return s


def _check(r, what):
    if r.status_code in (401, 403):
        raise IntegrationError(f"{what}: credentials rejected or no permission (HTTP {r.status_code}). "
                               "Check the email, the API token and the Auth type.")
    if r.status_code >= 400:
        body = (r.text or "").strip()
        if body.startswith("<") or "<html" in body[:500].lower():
            raise IntegrationError(
                f"{what}: HTTP {r.status_code} - the server returned a web page instead of API data. "
                f"URL called: {r.url} . The Base URL or Context path is almost certainly wrong. For Confluence "
                "Cloud use Base URL https://YOURSITE.atlassian.net (no page path) and Context path /wiki.")
        raise IntegrationError(f"{what}: HTTP {r.status_code}: {body[:400]}")


def _json(r, what):
    """Parse a JSON response, or explain clearly why the server did not send JSON."""
    try:
        return r.json()
    except ValueError:
        ctype = r.headers.get("Content-Type", "")
        snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text or ""))[:160].strip()
        hint = ("The server answered with a web page, not API data. Usual causes: wrong Base URL or Context path "
                "(Confluence Cloud needs context path /wiki and a Base URL like https://yourco.atlassian.net with no "
                "extra path), an SSO / login page instead of API access, or an API token used with the wrong "
                "Auth type.")
        if not (r.text or "").strip():
            hint = ("The server returned an empty response. Check the Base URL, the Context path and that the "
                    "token has not expired.")
        raise IntegrationError(f"{what}: expected JSON but got '{ctype or 'unknown type'}' "
                               f"(HTTP {r.status_code}) from {r.url}. {hint} Response started with: '{snippet}'")


def _clean_base(base, ctx):
    """Accepts a pasted page URL or a base that already contains the context path, and returns host + ctx."""
    base = (base or "").strip().rstrip("/")
    m = re.match(r"(https?://[^/]+)(/.*)?$", base)
    if m and m.group(1).lower().endswith(".atlassian.net"):
        return m.group(1) + "/wiki"          # Confluence Cloud always lives under /wiki; drop any pasted page path
    ctx = (ctx or "").strip()
    ctx = ("/" + ctx.strip("/")) if ctx else ""
    if ctx:
        if base.endswith(ctx):
            base = base[: -len(ctx)]
        elif ctx + "/" in base:
            base = base[: base.index(ctx + "/")]
    return base + ctx


def html_to_text(h):
    t = re.sub(r"<(script|style).*?</\1>", " ", h or "", flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')):
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------- Confluence
class Confluence:
    def __init__(self, base, ctx, auth_type, email, token, session=None):
        self.base = _clean_base(base, ctx)
        self.http = session or _session(auth_type, email, token)

    @staticmethod
    def _name(u):
        return (u or {}).get("displayName") or (u or {}).get("publicName") or ""

    def search(self, keywords, spaces, limit=10):
        kw = " OR ".join(f'text ~ "{k.replace(chr(34), "")}"' for k in keywords if k.strip())
        if not kw:
            raise IntegrationError("Enter at least one search keyword.")
        cql = f"type = page AND ({kw})"
        if spaces:
            cql = "space in (%s) AND %s" % (",".join(f'"{x}"' for x in spaces), cql)
        r = self.http.get(f"{self.base}/rest/api/content/search", timeout=60, params={
            "cql": cql, "limit": limit,
            "expand": "body.storage,version,history,history.contributors.publishers,space"})
        _check(r, "Confluence search")
        pages = []
        for i, res in enumerate(_json(r, "Confluence search").get("results", []), 1):
            contributors = []
            try:
                contributors = [self._name(u) for u in res["history"]["contributors"]["publishers"].get("users", [])]
            except (KeyError, TypeError, AttributeError):
                pass
            commenters = []
            try:  # best effort: comments reveal informal stakeholders
                c = self.http.get(f"{self.base}/rest/api/content/{res['id']}/child/comment", timeout=30,
                                  params={"expand": "version", "limit": 50})
                if c.status_code < 400:
                    commenters = sorted({self._name(x["version"]["by"]) for x in _json(c, "Confluence comments").get("results", [])
                                         if x.get("version")})
            except (requests.RequestException, IntegrationError):
                pass
            pages.append({
                "id": f"C{i}", "real_id": res["id"], "title": res.get("title", ""),
                "space": (res.get("space") or {}).get("key", ""),
                "owner": self._name(res.get("history", {}).get("createdBy")),
                "last_editor": self._name(res.get("version", {}).get("by")),
                "contributors": sorted(set(filter(None, contributors))),
                "commenters": [c for c in commenters if c],
                "updated": (res.get("version", {}).get("when") or "")[:10],
                "url": self.base + (res.get("_links", {}).get("webui", "")),
                "body": html_to_text(res.get("body", {}).get("storage", {}).get("value", "")),
            })
        return pages


# ---------------------------------------------------------------- Jira
class Jira:
    def __init__(self, base, auth_type, email, token, session=None):
        self.base = base.rstrip("/")
        self.cloud = auth_type.startswith("Cloud")
        self.http = session or _session(auth_type, email, token)

    def myself(self):
        r = self.http.get(f"{self.base}/rest/api/2/myself", timeout=30)
        _check(r, "Jira login")
        return _json(r, "Jira connection test").get("displayName", "")

    def project_meta(self, project_key):
        r = self.http.get(f"{self.base}/rest/api/2/project/{project_key}", timeout=30)
        _check(r, f"Jira project {project_key}")
        types = [t["name"] for t in _json(r, "Jira project lookup").get("issueTypes", []) if not t.get("subtask")]
        pr = self.http.get(f"{self.base}/rest/api/2/priority", timeout=30)
        prios = [p["name"] for p in pr.json()] if pr.status_code < 400 else []
        return {"issue_types": types, "priorities": prios}

    def find_duplicates(self, project_key, summary):
        clean = re.sub(r'["\\\\]', " ", summary)[:200]
        jql = f'project = "{project_key}" AND summary ~ "\\"{clean}\\""'
        urls = [f"{self.base}/rest/api/3/search/jql", f"{self.base}/rest/api/2/search"] if self.cloud \
            else [f"{self.base}/rest/api/2/search"]
        for u in urls:
            try:
                r = self.http.get(u, timeout=30, params={"jql": jql, "fields": "summary", "maxResults": 3})
            except requests.RequestException:
                continue
            if r.status_code < 400:
                return [i["key"] for i in r.json().get("issues", [])]
        return []

    def create_issue(self, project_key, issue_type, summary, description, labels=None, priority=None,
                     parent_key=None, epic_link_field=None, epic_name_field=None, epic_name=None,
                     story_points_field=None, story_points=None):
        fields = {"project": {"key": project_key}, "issuetype": {"name": issue_type},
                  "summary": summary[:250], "description": description}
        if labels:
            fields["labels"] = labels
        if priority:
            fields["priority"] = {"name": priority}
        if epic_name_field and epic_name:
            fields[epic_name_field] = epic_name
        if story_points_field and story_points not in (None, ""):
            fields[story_points_field] = float(story_points)
        warnings = []
        attempts = [dict(fields)]
        if parent_key:
            attempts = [{**fields, "parent": {"key": parent_key}}]
            if epic_link_field:
                attempts.append({**fields, epic_link_field: parent_key})
            attempts.append(dict(fields))
        last = None
        for n, f in enumerate(attempts):
            r = self.http.post(f"{self.base}/rest/api/2/issue", json={"fields": f}, timeout=60)
            if r.status_code < 300:
                if parent_key and n == len(attempts) - 1:
                    warnings.append("created WITHOUT epic link (parent not accepted by this project)")
                return {"key": r.json()["key"], "warnings": warnings}
            last = r
            if r.status_code in (401, 403):
                break
        _check(last, "Jira create issue")
        raise IntegrationError(f"Jira create issue failed: {last.text[:300]}")
