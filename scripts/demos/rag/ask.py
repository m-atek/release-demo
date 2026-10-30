"""/ask in Slack: production's runbook assistant answers, in the message the Slack handler posted.

    QUESTION=... SLACK_CHANNEL=... SLACK_TS=... ASKED_BY=... python scripts/demos/rag/ask.py

Run by the Jenkins job runbook-assistant-ask. The question arrives as an
environment variable, never quoted into a script. The message becomes the
question, the answer, "Sources: [id] [title] ([status])" and "Answered by
runbook-assistant [version]"; a failure puts its reason there instead. A
question that looks like it holds a secret is never sent: "Not asked: the
question looked like it held a secret."
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parents[1]))
import rag  # noqa: E402
from common import get_parameter, ssm_client  # noqa: E402

SECRET = "Not asked: the question looked like it held a secret."


def asked(question, user):
    who = f"<@{user}>" if user else "someone"
    return f"Question from {who}: {question}"


def answered(question, user, found):
    sources = ", ".join(f"{s['id']} {s['title']} ({s['status']})" for s in found["sources"]) or "none"
    return "\n\n".join([asked(question, user), found["answer"], f"Sources: {sources}",
                        f"Answered by {rag.SERVICE} {found['version']}"])


def failed(question, user, reason):
    return "\n\n".join([asked(question, user), f":x:  {reason}"])


def update(text):
    import notify

    channel, ts = os.environ.get("SLACK_CHANNEL", ""), os.environ.get("SLACK_TS", "")
    if not channel or not ts:
        print(text)
        return
    notify.slack("chat.update", get_parameter(ssm_client(), "slack/bot-token"), channel=channel, ts=ts, text=text)


def main():
    import answer
    import llm

    question = os.environ.get("QUESTION", "").strip()
    user = os.environ.get("ASKED_BY", "")
    if not 3 <= len(question) <= 300:
        update(failed(question, user, "Ask with 3 to 300 characters."))
        return 0
    try:
        _, removed = llm.redact(question)
    except llm.Refused as refusal:
        update(failed(question, user, f"Not asked: {refusal}"))
        return 1
    if removed:
        update(failed("[a question holding a secret]", user, SECRET))
        return 0
    try:
        found = answer.answer(question, "production")
    except SystemExit as stop:
        update(failed(question, user, stop.code if isinstance(stop.code, str) else "The answer path stopped."))
        return 1
    except llm.Refused as refusal:
        update(failed(question, user, f"No answer: {refusal}"))
        return 1
    except Exception as error:
        update(failed(question, user, f"No answer ({error.__class__.__name__}). The job's log says why."))
        return 1
    update(answered(question, user, found))
    print(f"Answered by {rag.SERVICE} {found['version']} from {', '.join(s['id'] for s in found['sources']) or 'nothing'}"
          f" in {found['tokens']['output']} tokens out")
    return 0


if __name__ == "__main__":
    sys.exit(main())
