"""The runbook assistant's corpus as tables: one row per page in `pages`, and
the chunks the search index embeds in `chunks_<hash8>`.

    chunker.py <corpus folder> <catalog> <schema> <chunk table>

Runs as a one-time serverless job on Databricks, which
scripts/demos/rag/index.py submits for a chunk hash that has no table yet.
The pages are Markdown with front matter (id, title, kind, status, updated,
replaced_by); the past changes in changes.json become pages of kind change.
Each page splits at its headings, at most max_tokens a chunk, with the page's
id and title before each chunk's text, so the embedding and the model see
which page a passage comes from. A retired page stays in both tables,
marked retired: keeping it out of answers is the retriever's job. Only the
standard library here; Spark comes with Databricks' runtime.
"""

import json
import re
import sys
from pathlib import Path

FIELDS = ["id", "title", "kind", "status", "updated", "replaced_by"]


def settings(path):
    """chunking.yaml: flat "key: value" lines, read without a YAML library. The job
    passes the volume's copy: Databricks runs this file without __file__."""
    found = {}
    for line in Path(path).read_text().splitlines():
        line = line.split(" #", 1)[0].strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        value = value.strip('"')
        found[key] = int(value) if value.isdigit() else value
    return found


def front_matter(text):
    """(fields, body) of a page with front matter between two --- lines."""
    match = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
    if not match:
        raise ValueError("a page starts with front matter between --- lines")
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    missing = [f for f in FIELDS if f not in fields]
    if missing:
        raise ValueError(f"front matter without {', '.join(missing)}")
    return fields, match.group(2).strip()


def pages(corpus):
    """Every page: the runbooks, then the past changes as pages of kind change."""
    found = []
    for path in sorted(Path(corpus, "pages").glob("*.md")):
        fields, body = front_matter(path.read_text())
        found.append({**fields, "body": body})
    for change in json.loads(Path(corpus, "changes.json").read_text()):
        body = (f"{change['short_description']}. Closed {change['close_code'].lower()} on {change['closed']}.\n\n"
                f"{change['close_notes']}")
        found.append({"id": change["number"], "title": change["short_description"], "kind": "change",
                      "status": "current", "updated": change["closed"][:10], "replaced_by": "", "body": body})
    return found


def tokens(text):
    """About four tokens for every three words: close enough to cut chunks."""
    return len(text.split()) * 4 // 3 + 1


def sections(body):
    """(heading, text) at each "## " heading; the text before the first is the page's lead."""
    found, heading, lines = [], "", []
    for line in body.splitlines():
        if line.startswith("## "):
            if "\n".join(lines).strip():
                found.append((heading, "\n".join(lines).strip()))
            heading, lines = line[3:].strip(), []
        else:
            lines.append(line)
    if "\n".join(lines).strip():
        found.append((heading, "\n".join(lines).strip()))
    return found


def chunks(page, config):
    """The page's chunks, each at most max_tokens, each starting with the page's id and title."""
    limit = config["max_tokens"]
    found = []
    for heading, text in sections(page["body"]):
        head = f"{page['id']} {page['title']}" + (f"\n{heading}" if heading else "")
        parts, current = [], []
        for paragraph in [p for p in text.split("\n\n") if p.strip()]:
            if current and tokens(head + "\n" + "\n\n".join(current + [paragraph])) > limit:
                parts.append(current)
                current = []
            current.append(paragraph)
        if current:
            parts.append(current)
        for part in parts:
            found.append({"chunk_id": f"{page['id']}#{len(found) + 1}", "page_id": page["id"], "title": page["title"],
                          "kind": page["kind"], "status": page["status"], "replaced_by": page["replaced_by"],
                          "heading": heading, "text": head + "\n" + "\n\n".join(part)})
    return found


def main():
    corpus, catalog, schema, table = sys.argv[1:5]
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()
    config = settings(Path(corpus).parent / "chunking.yaml")
    found = pages(corpus)
    rows = [{key: page[key] for key in FIELDS} for page in found]
    spark.createDataFrame(rows).withColumnRenamed("id", "page_id").createOrReplaceTempView("new_pages")
    spark.sql(f"CREATE TABLE IF NOT EXISTS `{catalog}`.`{schema}`.pages (page_id STRING, title STRING, kind STRING, "
              "status STRING, updated STRING, replaced_by STRING)")
    spark.sql(f"MERGE INTO `{catalog}`.`{schema}`.pages AS p USING new_pages AS n ON p.page_id = n.page_id "
              "WHEN MATCHED THEN UPDATE SET * WHEN NOT MATCHED THEN INSERT *")
    pieces = [chunk for page in found for chunk in chunks(page, config)]
    (spark.createDataFrame(pieces).write.format("delta").mode("overwrite")
     .option("delta.enableChangeDataFeed", "true").saveAsTable(f"`{catalog}`.`{schema}`.`{table}`"))
    spark.sql(f"ALTER TABLE `{catalog}`.`{schema}`.`{table}` SET TBLPROPERTIES (delta.enableChangeDataFeed = true)")
    print(f"{len(rows)} pages, {len(pieces)} chunks in {catalog}.{schema}.{table}")


if __name__ == "__main__":
    main()
