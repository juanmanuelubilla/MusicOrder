"""Decision Engine.

Given a group of tracks that the Matching Engine judged to represent the SAME
recording, this engine decides which one should be kept. It only runs *after*
matching and never influences song identity: quality, metadata, artwork,
library priority and user preferences are independent of "are these the same
content?".

The Matching Engine stays responsible for ``same_content``; this package owns
``winner = B``. The physical action (copy / skip / move) belongs to the Fusion
Engine.
"""

from __future__ import annotations

from musicfusion.decision.engine import Candidate, DecisionEngine, DecisionResult
from musicfusion.decision.storage import DecisionStore, group_key

__all__ = ["Candidate", "DecisionEngine", "DecisionResult", "DecisionStore", "group_key"]
