"""OData filter helpers for Azure AI Search.

Session and document ids reach these filters from client input, so every string
literal must be escaped: an unescaped quote would let a caller rewrite the filter
and read another session's chunks.
"""


def odata_str(value: str) -> str:
    """Quote a value as an OData string literal (single quotes are doubled)."""
    return "'" + str(value).replace("'", "''") + "'"
