"""Simple, transparent lexical retrieval over the document pack.

The data pack is six short documents - an embedding index would be
overkill and harder to audit. Instead we score chunks by keyword/phrase
overlap, which is easy to reason about and to unit test, and we always
surface authority metadata (status/effective date/account scope)
alongside the text so the model cannot answer without seeing it.
"""
import re
from .store import DOCUMENTS

STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "of", "to",
    "in", "on", "for", "and", "or", "if", "do", "does", "did", "can", "should",
    "would", "will", "with", "without", "as", "at", "by", "from", "this",
    "that", "it", "its", "their", "there", "what", "when", "how", "i", "we",
    "my", "our", "customer", "please",
}


def _tokenize(text: str):
    return [w for w in re.findall(r"[a-z0-9%]+", text.lower()) if w not in STOPWORDS]


def search(query: str, top_k: int = 4, account_id: str | None = None, doc_types: list[str] | None = None):
    """Return the top_k most relevant chunks across all documents.

    account_id: if given, boosts/does not exclude account-specific agreements
    for that account (agreements for OTHER accounts are still searchable in
    internal mode for investigation purposes - access control for structured
    customer records is enforced separately and much more strictly in tools.py).
    """
    q_tokens = set(_tokenize(query))
    results = []
    for doc in DOCUMENTS:
        if doc_types and doc["doc_type"] not in doc_types:
            continue
        for chunk in doc["chunks"]:
            hay = _tokenize(chunk["heading"] + " " + chunk["text"] + " " + doc["title"])
            overlap = q_tokens.intersection(hay)
            score = len(overlap)
            # phrase bonus
            if query.lower() in chunk["text"].lower():
                score += 3
            if score == 0:
                continue
            if doc["doc_type"] == "customer_agreement":
                if account_id and doc.get("account_id") == account_id:
                    score += 2  # relevant to the customer in scope
            results.append({
                "score": score,
                "doc_id": doc["doc_id"],
                "title": doc["title"],
                "doc_type": doc["doc_type"],
                "status": doc["status"],
                "effective_date": doc.get("effective_date"),
                "superseded_by": doc.get("superseded_by"),
                "supersedes": doc.get("supersedes"),
                "account_id": doc.get("account_id"),
                "chunk_id": chunk["chunk_id"],
                "heading": chunk["heading"],
                "text": chunk["text"],
            })
    results.sort(key=lambda r: r["score"], reverse=True)
    top = results[:top_k]
    for r in top:
        r.pop("score", None)
        if r["status"] == "DEPRECATED":
            r["warning"] = (
                "DEPRECATED: superseded by "
                f"{r.get('superseded_by', 'a newer document')}. Do not use as current policy."
            )
    return top
