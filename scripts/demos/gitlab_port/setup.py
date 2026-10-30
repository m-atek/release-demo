"""Demo 17's GitLab project, its runner and its trigger token, with the demo
account's token (gitlab/api-token). Idempotent; from the Mac.

    python scripts/demos/gitlab_port/setup.py            make what is missing
    python scripts/demos/gitlab_port/setup.py --check    what the acceptance checks and the video need

Makes the private project <account>/release-demo if it is missing, turns
off GitLab's instance runners for it, and creates the project runner
tools-host (tag tools-host, untagged jobs refused, locked to the project),
whose authentication token goes straight into SSM (gitlab/runner-token), and
the pipeline trigger token the Slack handler starts the pipeline with
(gitlab/trigger-token). It writes the project's id (gitlab/project-id).
Values are never printed. Then: scripts/demo.sh up gitlab-port registers the
runner on the tools host, starts it and syncs main.

--check prints: private, instance runners off, the project's runners and
whether each is online, no CI/CD variables, and a trigger token present.
"""

import argparse
import sys

from gitlab_demo import PROJECT, RUNNER, SSM_PREFIX, TRIGGER, GitLab, put_parameter


def ensure_project(gl):
    project = gl.project(missing_ok=True)
    if project is None:
        project = gl.call("POST", "/projects", {"name": PROJECT, "path": PROJECT, "visibility": "private",
                                                "initialize_with_readme": False, "shared_runners_enabled": False,
                                                "description": "A copy of github.com/m-atek/release-demo's main, "
                                                               "for its GitLab CI demo."})
        print(f"project {project['path_with_namespace']}: created, private")
    if project.get("shared_runners_enabled"):
        gl.call("PUT", f"/projects/{project['id']}", {"shared_runners_enabled": False})
        print("instance runners: turned off")
    put_parameter(gl.ssm, "gitlab/project-id", str(project["id"]), secure=False)
    return project


def ensure_runner(gl, project):
    runners = [r for r in gl.runners(project["id"]) if r.get("description") == RUNNER]
    if runners:
        print(f"runner {RUNNER}: there (id {runners[0]['id']})")
        if not stored(gl, "gitlab/runner-token"):
            token = gl.call("POST", f"/runners/{runners[0]['id']}/reset_authentication_token")["token"]
            put_parameter(gl.ssm, "gitlab/runner-token", token)
            print(f"runner {RUNNER}: a new authentication token, stored in SSM")
        return runners[0]
    runner = gl.call("POST", "/user/runners", {"runner_type": "project_type", "project_id": project["id"],
                                               "description": RUNNER, "tag_list": RUNNER, "run_untagged": False,
                                               "locked": True, "access_level": "not_protected",
                                               "maximum_timeout": 3600})
    put_parameter(gl.ssm, "gitlab/runner-token", runner["token"])
    print(f"runner {RUNNER}: created (id {runner['id']}), its token stored in SSM")
    return runner


def ensure_trigger(gl, project):
    triggers = [t for t in gl.call("GET", f"/projects/{project['id']}/triggers") or [] if t["description"] == TRIGGER]
    if triggers and stored(gl, "gitlab/trigger-token"):
        print(f"trigger token '{TRIGGER}': there")
        return
    token = triggers[0]["token"] if triggers and len(triggers[0].get("token", "")) > 8 else None
    if token is None:
        token = gl.call("POST", f"/projects/{project['id']}/triggers", {"description": TRIGGER})["token"]
        print(f"trigger token '{TRIGGER}': created")
    put_parameter(gl.ssm, "gitlab/trigger-token", token)
    print("trigger token: stored in SSM")


def stored(gl, name):
    value = gl.ssm.get_parameter(Name=SSM_PREFIX + name, WithDecryption=True)["Parameter"]["Value"]
    return value != "unset"


def check(gl):
    project = gl.project(missing_ok=True)
    if project is None:
        print("FAIL no project yet: run setup.py")
        return 1
    problems = []
    print(f"project {project['path_with_namespace']}: {project['visibility']}")
    problems += [] if project["visibility"] == "private" else ["the project isn't private"]
    print(f"instance runners: {'on' if project.get('shared_runners_enabled') else 'off'}")
    problems += ["instance runners are on"] if project.get("shared_runners_enabled") else []
    runners = gl.runners(project["id"])
    for runner in runners:
        print(f"runner {runner['description']}: {runner.get('status')}, tags {', '.join(runner.get('tag_list', []))}, "
              f"untagged jobs {'run' if runner.get('run_untagged') else 'refused'}, "
              f"{'locked to the project' if runner.get('locked') else 'not locked'}")
    if [r["description"] for r in runners] != [RUNNER]:
        problems.append(f"the project's runners are {[r['description'] for r in runners] or 'none'}, not {RUNNER} alone")
    elif runners[0].get("status") != "online":
        problems.append(f"{RUNNER} is {runners[0].get('status')}: scripts/demo.sh up gitlab-port starts it")
    variables = gl.call("GET", f"/projects/{project['id']}/variables") or []
    print(f"CI/CD variables: {len(variables)}")
    problems += [f"the project has {len(variables)} CI/CD variable(s)"] if variables else []
    triggers = [t for t in gl.call("GET", f"/projects/{project['id']}/triggers") or [] if t["description"] == TRIGGER]
    print(f"trigger token '{TRIGGER}': {'present' if triggers else 'missing'}")
    problems += [] if triggers else ["no trigger token"]
    for problem in problems:
        print(f"  FAIL {problem}")
    print("As the demo needs it." if not problems else f"{len(problems)} thing(s) to fix.")
    return 1 if problems else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="show the settings, change nothing")
    args = parser.parse_args()
    gl = GitLab()
    if args.check:
        return check(gl)
    project = ensure_project(gl)
    ensure_runner(gl, project)
    ensure_trigger(gl, project)
    return check(gl)


if __name__ == "__main__":
    sys.exit(main())
