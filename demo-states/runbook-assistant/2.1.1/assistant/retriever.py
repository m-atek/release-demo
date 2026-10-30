"""What the assistant asks the search index for one question.

The passages it gets back are all the model sees. They carry each page's id
and title but not its status, so the model can't tell a retired page from a
current one: keeping retired pages out is this file's job.
"""

COLUMNS = ["chunk_id", "page_id", "title", "status", "text"]


def search_arguments(question, settings):
    """The index query for one question: the passages the model may answer from."""
    return search(
        query_text=question,
        query_type="hybrid",
        filters={"status": "current"},
        num_results=settings["num_results"],
    )


def search(**arguments):
    """similarity_search's keyword arguments, with the columns every answer needs."""
    return {"columns": COLUMNS, **arguments}
