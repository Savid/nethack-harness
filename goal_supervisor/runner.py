"""One persisted execution window at a time, leaving goal choice to the caller."""
from .goals import GoalBoard
from .harness import Rejected


def step(document, client, save, recover=False, current=None):
    """Run one window. `current` is the (status, observation) a previous window of this process ended with;
    the window returns its own ending pair for the next one."""
    board = GoalBoard(document["board"])

    def persist():
        document["board"] = board.snapshot()
        save(document)

    if recover:
        execution = board.state["inflight"]
        if not execution:
            raise ValueError("there is no unresolved execution to recover")
        status, observation, outcome = client.recover(execution)
    else:
        board.editable()
        status, observation = current or client.snapshot()
        preparation = board.begin(observation, (status.get("last") or {}).get("decision", 0))
        persist()
        if preparation["reason"] != "execute":
            return preparation, (status, observation)
        execution = preparation["context"]["execution"]
        preparation["after_decision"] = board.state["inflight"]["after_decision"]
        # The reservation remains on disk if I/O fails; another action requires explicit recovery.
        try:
            status, observation, outcome = client.execute(preparation)
        except Rejected as error:
            result = board.reject(execution["id"], str(error))
            persist()
            return result, (status, observation)
    result = board.finish(execution["id"], observation, outcome, recovered=recover)
    persist()
    return result, (status, observation)
