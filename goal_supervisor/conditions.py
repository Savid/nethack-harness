"""Small, three-valued predicates over caller-supplied observations."""
import math


OPS = {"eq", "ne", "lt", "lte", "gt", "gte", "contains"}


def validate(conditions):
    if not isinstance(conditions, list):
        raise ValueError("conditions must be a list")
    for condition in conditions:
        if not isinstance(condition, dict) or set(condition) != {"path", "op", "value"}:
            raise ValueError("each condition needs path, op and value")
        path = condition["path"]
        if not isinstance(path, list) or not path or not all(
                isinstance(key, str) or type(key) is int and key >= 0 for key in path):
            raise ValueError("condition path must contain object keys or nonnegative array indexes")
        if not isinstance(condition["op"], str) or condition["op"] not in OPS:
            raise ValueError("unsupported condition operator")


def equal(left, right):
    # JSON booleans must not satisfy numeric targets such as an attempt count of 1.
    if type(left) is bool or type(right) is bool:
        return type(left) is type(right) and left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(equal(a, b) for a, b in zip(left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(equal(left[key], right[key]) for key in left)
    return left == right


def evaluate(condition, observation):
    value = observation
    found = True
    for key in condition["path"]:
        if isinstance(value, dict) and isinstance(key, str) and key in value:
            value = value[key]
        elif isinstance(value, list) and type(key) is int and key < len(value):
            value = value[key]
        else:
            found, value = False, None
            break
    evidence = {"condition": condition, "observed": value, "found": found, "matches": None}
    target, op = condition["value"], condition["op"]
    if not found or value is None:
        return evidence
    if op in ("eq", "ne"):
        evidence["matches"] = equal(value, target) if op == "eq" else not equal(value, target)
    elif op == "contains":
        if isinstance(value, list):
            evidence["matches"] = any(equal(item, target) for item in value)
        elif isinstance(value, (str, dict)) and isinstance(target, str):
            evidence["matches"] = target in value
    elif all(type(item) is int or type(item) is float and math.isfinite(item) for item in (value, target)):
        evidence["matches"] = {"lt": value < target, "lte": value <= target,
                               "gt": value > target, "gte": value >= target}[op]
    return evidence


def check(conditions, observation, any_match=False):
    evidence = [evaluate(condition, observation) for condition in conditions]
    values = [item["matches"] for item in evidence]
    if any_match:
        result = True if True in values else None if None in values else False
    else:
        result = False if False in values else None if None in values else True
    return {"matches": result, "evidence": evidence}
