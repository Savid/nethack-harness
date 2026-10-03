"""The decision-endpoint client, answer normalisation and the questions the inner loop asks."""
import json
import time
import urllib.error
import urllib.request

ACT_INSTRUCTIONS = ("Choose the hero's best next action. Survive first: do not melee when HP is low if a safer "
                    "option exists. Otherwise make progress: descend when on the down stairs, travel to known down "
                    "stairs, kill weak monsters in the way, explore.")
DANGER_INSTRUCTIONS = ("Could the hero plausibly die within the next 5 turns (very low HP, dangerous monster "
                       "adjacent, or a deadly condition)?")


class Unhealthy(RuntimeError):
    """The endpoint failed or was too slow; the caller should play on rules for a while."""


def decider(url, model=None, key=None, timeout=4.0):
    """POST {state, questions} to a SystemOne-compatible endpoint. One short attempt plus at most one retry on
    429/503/529 (honouring a small Retry-After); anything else raises Unhealthy."""
    if not url or url == "none":
        return None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key

    def decide(body, timeout=timeout):
        if model:
            body = dict(body, model=model)
        data = json.dumps(body).encode()
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, data=data, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code in (429, 503, 529) and attempt == 0:
                    try:
                        wait = min(2.0, float(e.headers.get("Retry-After") or 0.5))
                    except ValueError:
                        wait = 0.5
                    time.sleep(wait)
                    continue
                raise Unhealthy("decision endpoint HTTP %d: %s" % (e.code, e.read()[:200]))
            except (urllib.error.URLError, OSError, ValueError) as e:
                raise Unhealthy("decision endpoint unreachable or slow: %s" % e)
        raise Unhealthy("decision endpoint busy")
    return decide


def normalize(answers):
    """Engines differ in optional fields: make choice answers carry choice, probabilities and confidence."""
    out = {}
    for key, ans in (answers or {}).items():
        if not isinstance(ans, dict):
            continue
        ans = dict(ans)
        probs = ans.get("probabilities")
        if "choice" in ans or (isinstance(probs, dict) and ans.get("type") == "choice"):
            if not isinstance(probs, dict) or not probs:
                probs = {ans.get("choice"): float(ans.get("confidence", 1.0))}
            probs = {str(k): float(v) for k, v in probs.items()}
            ans["probabilities"] = probs
            ans.setdefault("choice", max(probs, key=probs.get))
            ans.setdefault("confidence", max(probs.values()))
        if "noul" not in ans and isinstance(probs, dict) and "true" in probs:
            ans["noul"] = probs["true"]
        if "noul" in ans and "confidence" not in ans:
            ans["confidence"] = max(float(ans["noul"]), 1 - float(ans["noul"]))
        for k in ("noul", "score", "confidence"):
            if k in ans:
                ans[k] = float(ans[k])
        out[key] = ans
    return out


def confidence(probs):
    """Top probability normalised by the number of options: 0 = uniform, 1 = certain."""
    n = len(probs)
    if n < 2:
        return 1.0
    p1 = max(probs.values())
    return max(0.0, (n * p1 - 1) / (n - 1))


def builtin_questions(acts, rng):
    order = list(acts)
    rng.shuffle(order)   # cheap insurance against position bias
    return {"act": {"type": "choice", "instructions": ACT_INSTRUCTIONS,
                    "criteria": {a.key: a.desc for a in order}},
            "danger": {"type": "noul", "instructions": DANGER_INSTRUCTIONS}}
