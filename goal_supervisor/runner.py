"""One persisted execution window at a time, leaving goal choice to the caller."""
from .goals import GoalBoard


def step(document, client, save, recover=False):
    board = GoalBoard(document["board"])

    def persist():
        document["board"] = board.snapshot()
        save(document)

    if recover:
        execution = board.state["inflight"]
        if not execution:
            raise ValueError("there is no unresolved execution to recover")
    else:
        board.editable()
        status = client.paused()
        observation = client.observe()
        preparation = board.begin(observation, (status.get("last") or {}).get("decision", 0))
        persist()
        if preparation["reason"] != "execute":
            return preparation
        execution = preparation["context"]["execution"]
    # The reservation remains on disk if I/O fails; another action requires explicit recovery.
    observation, outcome = client.recover(execution) if recover else client.execute(preparation["context"])
    result = board.finish(execution["id"], observation, outcome, recovered=recover)
    persist()
    return result
