"""The answer path, shared by the smoke check, the evaluation and /ask.

    python scripts/demos/rag/answer.py production "How do I roll back orders-service?"

1. Reads the environment's pointer from SSM: version, package SHA-256, index.
2. Fetches that package from JFrog into a cache on the tools host, checked by
   its SHA-256, and loads its retriever, prompt and settings.
3. Queries the index with the package's search arguments, as the service
   principal, at the index's own query URL.
4. Asks Claude through llm.py with the package's model and effort, and
   checks the sources: an answer citing a page it wasn't given is refused,
   "No answer: it cited a page it wasn't given."
5. Returns the answer, its sources (id, title, status), the retrieved chunks
   (page id, chunk id, status, score), the version, the model and the tokens.

The passages carry each page's id and title but not its status, so the model
can't tell a retired page from a current one; keeping retired pages out is
the retriever's job. The evaluation passes spans, so the search shows in
MLflow as a RETRIEVER span with each page's status, and the call as an LLM span.
"""

import argparse
import contextlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parents[1]))
import rag  # noqa: E402

CITED_UNKNOWN = "No answer: it cited a page it wasn't given."


class NoSpans:
    """Without the evaluation: the answer path runs the same, untraced."""

    @contextlib.contextmanager
    def retriever(self, inputs):
        yield lambda outputs: None

    @contextlib.contextmanager
    def llm(self, inputs):
        yield lambda outputs: None


def rest_query(arguments):
    """similarity_search's keyword arguments as the REST query's body."""
    body = {"query_text": arguments["query_text"], "columns": list(arguments["columns"]),
            "num_results": int(arguments.get("num_results", 5))}
    if arguments.get("filters"):
        body["filters_json"] = json.dumps(arguments["filters"], sort_keys=True)
    if arguments.get("query_type"):
        body["query_type"] = str(arguments["query_type"]).upper()
    return body


def passages_text(question, chunks):
    """The user turn: the question, then each passage under its page's id and title. No status."""
    blocks = [f"[{c['page_id']}] {c['title']}\n{c['text']}" for c in chunks]
    return f"Question: {question}\n\nPassages:\n\n" + "\n\n".join(blocks)


def search(db, pointer, assistant, question, spans):
    from databricks_api import rows

    arguments = assistant["retriever"].search_arguments(question, assistant["settings"])
    body = rest_query(arguments)
    with spans.retriever({"query": question, "arguments": arguments}) as done:
        index = db.index(pointer["index"])
        if not index:
            raise SystemExit(rag.ENDPOINT_DOWN)  # the message is the exit's, for /ask and Slack
        found = rows(db.query(pointer["index"], body, (index.get("status") or {}).get("index_url")))
        chunks = [{"page_id": r.get("page_id"), "chunk_id": r.get("chunk_id"), "title": r.get("title"),
                   "status": r.get("status"), "text": r.get("text", ""), "score": r.get("score")} for r in found]
        done(chunks)
    return arguments, chunks


def generate(assistant, question, chunks, spans):
    import llm

    settings = assistant["settings"]
    text = passages_text(question, chunks)
    with spans.llm({"model": settings["model"], "effort": settings.get("effort"), "question": question}) as done:
        result = llm.complete("answer", system=assistant["prompt"], user=text, schema=assistant["schema"],
                              service=rag.SERVICE, model=settings["model"], effort=settings.get("effort"))
        done({"answer": result.data.get("answer"), "sources": result.data.get("sources"), "model": result.model})
    return result


def answer(question, environment, spans=None, db=None, pointer=None):
    """The answer of the version the environment runs."""
    from databricks_api import Databricks

    spans = spans or NoSpans()
    pointer = pointer or rag.read_pointer(environment)
    if not pointer:
        raise SystemExit(f"{environment} runs no version yet: release runbook-assistant first.")
    assistant = rag.load(rag.fetch(pointer["package"], pointer["sha256"]))
    db = db or Databricks.as_pipeline()
    arguments, chunks = search(db, pointer, assistant, question, spans)
    result = generate(assistant, question, chunks, spans)
    given = {c["page_id"]: c for c in chunks}
    cited = [str(s) for s in result.data.get("sources") or []]
    unknown = [s for s in cited if s not in given]
    return {
        "question": question, "environment": environment, "version": pointer["version"],
        "answer": CITED_UNKNOWN if unknown else result.data.get("answer", ""),
        "refused": bool(unknown), "unknown_sources": unknown,
        "sources": [{"id": s, "title": given[s]["title"], "status": given[s]["status"]} for s in cited if s in given],
        "chunks": [{k: c[k] for k in ("page_id", "chunk_id", "status", "score")} for c in chunks],
        # What the model read, for the judge: each passage's page, title and text, as it was given.
        "passages": [{"page": c["page_id"], "title": c["title"], "text": c["text"]} for c in chunks],
        "arguments": arguments, "model": result.model, "tokens": result.tokens, "cost": result.cost,
        "removed": result.removed,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("environment", choices=["staging", "production"])
    parser.add_argument("question")
    args = parser.parse_args()
    print(json.dumps(answer(args.question, args.environment), indent=2))


if __name__ == "__main__":
    main()
