"""Pack complete evidence units by actual role input limits, never by list tail."""

from ..errors import Stopped


def packets(items, payload, llm, role, schema, correction=None):
    batch = []
    for item in items:
        try:
            if correction is None:
                llm.build(role, payload(batch + [item]), schema)
            else:
                llm.build(role, payload(batch + [item]), schema, correction=correction)
        except Stopped as error:
            if error.code != "input_budget_exceeded":
                raise
            if batch:
                yield batch
                batch = []
            # Keep even an oversized singleton: the caller records the failure
            # for this item and can continue with subsequent packets.
        batch.append(item)
    if batch:
        yield batch
