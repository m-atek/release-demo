"""Demo 2's GitHub side, through the GitHub App: pull requests, their comment,
the merge, and commits made through the API, so nothing needs a clone or a
token in a URL. The app needs Contents, Pull requests and Checks: write."""

import base64
import urllib.error
import urllib.parse

import stack
from common import fail, github_api, github_app_token, ssm_client

MARKER = "<!-- network-stack-plan -->"


class GitHub:
    def __init__(self, ssm=None):
        self.token, self.repository, _ = github_app_token(ssm or ssm_client())

    def call(self, method, path, body=None, missing_ok=False):
        """The repository's API; path starts after /repos/<owner>/<repo>."""
        try:
            return github_api(method, f"/repos/{self.repository}{path}", self.token, body)
        except urllib.error.HTTPError as error:
            text = error.read().decode(errors="replace")
            if missing_ok and error.code == 404:
                return None
            if error.code == 403 and "not accessible by integration" in text:
                fail(f"GitHub refused {method} {path}: the GitHub App lacks a permission. It needs Contents, "
                     "Pull requests and Checks: write. See the README, Demo 2.")
            fail(f"GitHub {method} {path}: {error.code} {text[:300]}")

    # Pull requests

    def pull(self, number):
        return self.call("GET", f"/pulls/{number}")

    def open_pulls(self, branch=None):
        query = "?state=open&per_page=100" + (f"&head={self.owner}:{urllib.parse.quote(branch)}" if branch else "")
        return self.call("GET", f"/pulls{query}")

    @property
    def owner(self):
        return self.repository.split("/")[0]

    def create_pull(self, title, branch, body, base="main"):
        return self.call("POST", "/pulls", {"title": title, "head": branch, "base": base, "body": body})

    def close_pull(self, number, comment=None):
        if comment:
            self.call("POST", f"/issues/{number}/comments", {"body": comment})
        return self.call("PATCH", f"/pulls/{number}", {"state": "closed"})

    def merge(self, number, sha, title, message):
        """Squash-merges the pull request, only if its head is still the commit Jenkins planned."""
        return self.call("PUT", f"/pulls/{number}/merge", {"merge_method": "squash", "sha": sha,
                                                          "commit_title": title, "commit_message": message})

    def commit(self, sha):
        return self.call("GET", f"/commits/{sha}")

    def upsert_comment(self, number, body):
        """One comment per pull request, rewritten as the run goes on."""
        body = f"{body}\n\n{MARKER}"
        comments = self.call("GET", f"/issues/{number}/comments?per_page=100")
        for comment in comments:
            if MARKER in comment["body"] and comment["user"]["type"] == "Bot":
                return self.call("PATCH", f"/issues/comments/{comment['id']}", {"body": body})
        return self.call("POST", f"/issues/{number}/comments", {"body": body})

    # Branches and commits through the API

    def branch_sha(self, branch):
        ref = self.call("GET", f"/git/ref/heads/{branch}", missing_ok=True)
        return ref["object"]["sha"] if ref else None

    def file_at(self, path, ref):
        """A file's text on a branch or commit, or None."""
        found = self.call("GET", f"/contents/{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref)}", missing_ok=True)
        if not found or isinstance(found, list):
            return None
        return base64.b64decode(found["content"]).decode()

    def commit_files(self, parent, files, message, author=None):
        """A commit on top of parent that writes or, for None, deletes each path. Returns its sha."""
        base_tree = self.call("GET", f"/git/commits/{parent}")["tree"]["sha"]
        tree = []
        for path, content in files.items():
            if content is None:
                tree.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            else:
                blob = self.call("POST", "/git/blobs", {"content": content, "encoding": "utf-8"})
                tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        new_tree = self.call("POST", "/git/trees", {"base_tree": base_tree, "tree": tree})
        body = {"message": message, "tree": new_tree["sha"], "parents": [parent]}
        if author:
            body["author"] = author
        return self.call("POST", "/git/commits", body)["sha"]

    def set_branch(self, branch, sha, force=False):
        if self.branch_sha(branch):
            return self.call("PATCH", f"/git/refs/heads/{branch}", {"sha": sha, "force": force})
        return self.call("POST", "/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})

    def owner_identity(self):
        """The repository owner as a commit author: their GitHub noreply address, which
        links the commit to their account. people.yaml maps that account to Sam."""
        user = github_api("GET", f"/users/{self.owner}", self.token)
        return {"name": user.get("name") or user["login"],
                "email": f"{user['id']}+{user['login']}@users.noreply.github.com"}


def requester(pull, head_commit):
    """Who asked for the change, from people.yaml: the pull request's author, or for a
    pull request the GitHub App opened, the author of its newest commit."""
    import yaml

    everyone = yaml.safe_load((stack.ROOT / "people.yaml").read_text())
    logins = [pull["user"]["login"]]
    if (head_commit or {}).get("author"):
        logins.append(head_commit["author"]["login"])
    for login in logins:
        for key, person in everyone.items():
            if person.get("github_login") == login and person.get("servicenow_user"):
                return key, person
    return None, {"display_name": pull["user"]["login"]}
