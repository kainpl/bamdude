"""Pure returned-job policy shared by live routing and replay."""


def has_return_history(item):
    return getattr(item, "failed_return_proof", None) is not None or (getattr(item, "failed_return_count", 0) or 0) > 0


def last_failed_printer(item):
    proof = getattr(item, "failed_return_proof", None)
    if not isinstance(proof, dict) or type(proof.get("version")) is not int or proof["version"] != 1:
        return None
    identity = proof.get("last_failed_printer_identity")
    return identity if isinstance(identity, str) and identity else None


def returned_printer_rank(last_failed_identity, printer_identity, ordinary_rank):
    """Only rank existing candidates. Busy filtering and dispatch gates stay upstream."""
    failed = last_failed_identity is not None and last_failed_identity == printer_identity
    return (not failed, *ordinary_rank)
