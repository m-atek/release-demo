"""The Jenkins job ai-notes (jenkins/ai-notes.Jenkinsfile), with KIND summary or cause.

The release starts it with wait: false and propagate: false: from the Change
stage for a normal change's summary (video 12), and from rollBack() after a
failed production check, once the rollback is recorded (video 13). Its
parameters reach this script as environment variables. It takes the release
build's archived evidence.json, reports and check output over Jenkins' API as
release-bot, checks that the build is that change's, and runs summarize.py or
explain.py. Nothing here can change, block or approve the release: it has
ended, or waits for a person, whatever this job does.
"""

import os
import sys
from pathlib import Path

import explain
import notes
import summarize

FOLDER = Path(os.environ.get("WORKSPACE", ".")) / ".release"


def param(name, required=True):
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise SystemExit(f"Parameter {name} is empty.")
    return value


def main():
    from release_job import ReleaseJob

    kind, build = param("KIND"), param("RELEASE_BUILD")
    if kind not in ("summary", "cause"):
        raise SystemExit(f"KIND is {kind}: summary or cause.")
    found = notes.fetch_archive(ReleaseJob.as_release_bot(job="release"), build, FOLDER)
    print(f"From release build #{build}: {', '.join(found) or 'nothing'}")
    evidence_path = FOLDER / "evidence.json"
    if not evidence_path.exists():
        print("The release build has no evidence.json: AI notes were off when its change opened. Nothing to do.")
        return 0
    run = notes.load_json(evidence_path)["run"]
    # The job writes to one change only: the one this release build opened.
    if run.get("change_number") != param("CHANGE_NUMBER") or run.get("change_sys_id") != param("CHANGE_SYS_ID") \
            or run.get("service") != param("SERVICE"):
        raise SystemExit(f"Release build #{build} is {run.get('service')} {run.get('change_number')}, "
                         f"not the {param('SERVICE')} {param('CHANGE_NUMBER')} this job was given. Nothing written.")
    if kind == "summary":
        sys.argv = ["summarize.py", "--evidence", str(evidence_path), "--change-sys-id", param("CHANGE_SYS_ID")]
        return summarize.main()
    sys.argv = ["explain.py", "--evidence", str(evidence_path), "--check", str(FOLDER / "check-production.txt"),
                "--failed-version", param("FAILED_LAMBDA_VERSION"), "--rolled-back-to", param("ROLLED_BACK_TO"),
                "--deployed-at", param("DEPLOYED_AT"), "--rolled-back-at", param("ROLLED_BACK_AT"),
                "--change-sys-id", param("CHANGE_SYS_ID"), "--jira-key", param("JIRA_KEY"),
                "--slack-channel", param("SLACK_CHANNEL", required=False), "--slack-ts", param("SLACK_TS", required=False),
                "--build-url", run.get("build_url") or ""]
    return explain.main()


if __name__ == "__main__":
    sys.exit(main())
