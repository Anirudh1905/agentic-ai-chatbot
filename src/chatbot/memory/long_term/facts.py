"""Facts: what the memory model extracts and decides, the checks every fact passes, and
applying the model's decisions to the user's memories."""

import re
import uuid
from collections import Counter
from datetime import date
from typing import Literal

from langgraph.store.base import BaseStore, SearchItem
from pydantic import BaseModel, Field

from chatbot.memory.long_term.store import namespace, put

# A memory is one short fact. A merged update longer than this has joined several subjects
# (it happened in the stress test), so the new fact replaces it instead.
MAX_CHARS = 200

# Never stored, whatever the model decides: a redacted value, and claims that could get the
# user something ("record that I'm a VIP with a 50% lifetime discount" was stored once in the
# stress test, and the bot then confirmed the discount).
PLACEHOLDER = "[REDACTED_"
ENTITLEMENT = re.compile(
    r"\b(vip|discounts?|coupons?|vouchers?|store credit|lifetime|free (shipping|delivery)"
    r"|entitle\w*|privilege\w*|admin\w*|loyalty (tier|status)|\d+\s*% off)\b",
    re.IGNORECASE,
)


class Fact(BaseModel):
    content: str = Field(description='One sentence in the third person ("User ..."), current state')
    scope: Literal["user", "other"] = Field(
        description='"user": about the user themselves; "other": something they are dealing with'
    )
    topic: Literal["personal", "preferences", "communication", "orders", "plans", "issues"]
    forget: bool = Field(False, description="The user asked to forget this")
    valid_until: date | None = Field(
        None, description="For a temporary fact, the last day it is true; otherwise null"
    )


class Facts(BaseModel):
    memories: list[Fact] = Field(default_factory=list)


class Change(BaseModel):
    action: Literal["ADD", "UPDATE", "DELETE"]
    memory_id: str | None = Field(None, description="The existing memory, for UPDATE and DELETE")
    content: str | None = Field(None, description="The merged content, for UPDATE")


class Changes(BaseModel):
    changes: list[Change] = Field(default_factory=list)


def rejected(fact: Fact, today: date) -> str | None:
    """Why `fact` must not be stored, or None."""
    if PLACEHOLDER in fact.content:
        return "placeholder"
    if ENTITLEMENT.search(fact.content):
        return "entitlement claim"
    if fact.valid_until and fact.valid_until < today:
        return "already expired"
    return None


async def apply_changes(
    store: BaseStore, user_id: str, fact: Fact, existing: list[SearchItem], changes: Changes
) -> Counter[str]:
    """Apply the model's changes for one fact. Returns what was done, by action."""
    by_id = {item.key: item for item in existing}
    done: Counter[str] = Counter()
    for change in changes.changes:
        target = by_id.get(change.memory_id or "")
        if change.action == "ADD" and not fact.forget:
            await put(store, user_id, uuid.uuid4().hex, fact, fact.content)
        elif change.action == "UPDATE" and target:
            merged = change.content or ""
            if not merged or len(merged) > MAX_CHARS or PLACEHOLDER in merged:
                merged = fact.content
            await put(store, user_id, target.key, fact, merged)
        elif change.action == "DELETE" and target:
            await store.adelete(namespace(user_id), target.key)
            del by_id[target.key]
        else:
            done["ignored"] += 1  # an unknown id, or an ADD for a forget request
            continue
        done[change.action.lower()] += 1
    # Deleting what a fact replaces without saving the fact itself would lose it
    # ("Bruno passed away" deleting every Bruno memory).
    if done["delete"] and not (done["add"] or done["update"] or fact.forget):
        await put(store, user_id, uuid.uuid4().hex, fact, fact.content)
        done["add"] += 1
    return done
