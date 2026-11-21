"""Pure returned-job policy shared by live routing and replay."""


def has_return_history(item):
    return any(
        getattr(item, f"{kind}_return_proof", None) is not None or (getattr(item, f"{kind}_return_count", 0) or 0) > 0
        for kind in ("failed", "unsent")
    )


def last_failed_printers(item) -> frozenset[str]:
    identities = set()
    for proof, key in (
        (getattr(item, "failed_return_proof", None), "last_failed_printer_identity"),
        (getattr(item, "unsent_return_proof", None), "printer_identity"),
    ):
        if isinstance(proof, dict) and type(proof.get("version")) is int and proof["version"] == 1:
            identity = proof.get(key)
            if isinstance(identity, str) and identity:
                identities.add(identity)
    return frozenset(identities)


def returned_printer_rank(last_failed_identities, printer_identity, ordinary_rank):
    """Only rank existing candidates. Busy filtering and dispatch gates stay upstream."""
    return (printer_identity not in last_failed_identities, *ordinary_rank)
