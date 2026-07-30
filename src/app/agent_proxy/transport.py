"""Redis transport of the agent-run event fan-out (ADR-067 §3.1, §3.5, §4).

The consumer WRITES here (one pipeline per upstream event), the broker READS here (replay + live),
and both the broker's close rules (§3.3) and the orphan reaper (§5) ask it whether a lease is
alive. Everything Redis-shaped lives in this module so the two callers never build a key by hand.

Keys, all under ``agent:run:{runId}:`` in the dedicated logical DB ``AGENT_RUN_REDIS_DB``:

===============  =======  =========================================================================
``…:lease``      STRING   worker uuid owning the single upstream subscription (§4)
``…:epoch``      STRING   uuid4 identifying the current GENERATION of the ring (§3.1)
``…:seq``        counter  monotonic sequence WITHIN a generation
``…:events``     LIST     ``{"epoch":…,"seq":N,"data":"<raw SSE block>"}`` — replay buffer
``…:bytes``      STRING   running byte size of the ring; see the byte-ceiling note below
``…:pub``        channel  live delivery
===============  =======  =========================================================================

Three properties are load-bearing and each was paid for by a defect:

* **``epoch`` travels INSIDE every ring element and every published message**, not only in its own
  key (§3.1). A generation change must be visible ON THE EVENT — checking the key only at stream
  open fixes reconnects and leaves the identical defect on the neighbouring path (§3.3.1): Redis
  restarts, the broker's already-open subscription resumes, the live consumer republishes from
  ``seq`` 1, and the broker's "drop ``seq`` <= last delivered = 500" rule silently swallows the
  rest of the run — an SSE stream that stays open and never speaks again.
* **``EXPIRE`` is re-applied to every key in EVERY pipeline**, not just at creation: otherwise the
  ring of a run longer than the TTL evaporates mid-run and a client connecting at that moment gets
  no replay for a live run.
* **``seq`` comes only from Redis ``INCR``**, never from a worker-local counter: on a lease
  takeover an in-memory counter would restart at zero and break subscriber dedup.

Writing is BEST-EFFORT by design: a Redis error is logged and reported as a ``None`` result, never
raised into the consumer. Money, status and ``/state`` live in Postgres, so losing the ring costs
only the live stream — while a raised exception would cost the billing of a whole run.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import redis.asyncio as redis
from redis.exceptions import RedisError

from app.config import Settings

logger = logging.getLogger("app.agent_proxy.transport")

_KEY_PREFIX: Final = "agent:run:"

# One pipeline per event (ADR-067 §3.1: INCR seq → RPUSH → trim → EXPIRE ×3 → PUBLISH), as a Lua
# script so the whole sequence is ATOMIC against a concurrent takeover: a plain client-side pipeline
# would let two workers interleave RPUSH between another's INCR and its RPUSH, producing a ring
# whose element order disagrees with its `seq` order — and the broker's dedup trusts that order.
#
# ⚠️ Head trimming uses LPOP in a loop rather than LTRIM. LTRIM is the natural instruction and the
# ADR names it, but it does NOT report what it removed, so the byte counter could not be kept in
# step with it. The alternative — recomputing the ring size on every event — is O(ring) per event
# (up to 8 MB of string handling per event at the documented ceiling). Semantics are identical
# ("trim from the head"); only the instruction differs.
_PUBLISH_LUA: Final = """
local events_key = KEYS[1]
local seq_key    = KEYS[2]
local epoch_key  = KEYS[3]
local bytes_key  = KEYS[4]
local pub_key    = KEYS[5]

local epoch      = ARGV[1]
local data_json  = ARGV[2]
local max_events = tonumber(ARGV[3])
local max_bytes  = tonumber(ARGV[4])
local ttl        = tonumber(ARGV[5])

local seq = redis.call('INCR', seq_key)
local element = '{"epoch":"' .. epoch .. '","seq":' .. seq .. ',"data":' .. data_json .. '}'
redis.call('RPUSH', events_key, element)
local total = redis.call('INCRBY', bytes_key, #element)

-- Event-count ceiling.
while redis.call('LLEN', events_key) > max_events do
  local dropped = redis.call('LPOP', events_key)
  if not dropped then break end
  total = redis.call('DECRBY', bytes_key, #dropped)
end

-- Byte ceiling. The newest element is never dropped: a single event larger than the whole ceiling
-- would otherwise leave an empty ring and the client would see neither the event nor a reason.
-- The gap is not silent either way — the broker emits run.truncated whenever the first seq it can
-- serve is beyond the client's cursor + 1 (§3.2 step 4).
while total > max_bytes and redis.call('LLEN', events_key) > 1 do
  local dropped = redis.call('LPOP', events_key)
  if not dropped then break end
  total = redis.call('DECRBY', bytes_key, #dropped)
end

-- EXPIRE on EVERY pipeline, for every key of the run: the ring must not evaporate mid-run.
redis.call('EXPIRE', events_key, ttl)
redis.call('EXPIRE', seq_key, ttl)
redis.call('EXPIRE', bytes_key, ttl)
-- The epoch key is only REFRESHED here, never created (see ensure_epoch): a missing epoch key must
-- keep meaning "the generation is gone", and an event silently recreating it would destroy that.
if redis.call('EXISTS', epoch_key) == 1 then
  redis.call('EXPIRE', epoch_key, ttl)
end

redis.call('PUBLISH', pub_key, element)
return seq
"""

# Release the lease only if we still own it (compare-and-delete). A plain DEL would let a worker
# whose lease already expired and was taken over delete the NEW owner's lease.
_RELEASE_LEASE_LUA: Final = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

# Renew only while we still own it — same reason, plus a renewal by a displaced worker would extend
# a lease it no longer holds and delay the rightful takeover.
#
# The ABSENT case is deliberately NOT a loss: see LeaseRenewal / renew_lease.
_RENEW_LEASE_LUA: Final = """
local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
  return 1
end
if current == false then
  redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
  return 2
end
return 0
"""


class LeaseAcquisition(Enum):
    """Outcome of taking a lease (see :meth:`AgentRunEventBus.acquire_lease`), ADR-067 §4.1.

    Three values because a plain ``bool`` conflated the only two answers that call for OPPOSITE
    behaviour, and the conflation was TD-037 all over again: a run started while Redis was down
    took the "someone else drives this run" branch and stood down WITHOUT SUBSCRIBING, so its
    one-shot stream was read by nobody — no billing, no snapshot, no terminal status, and a later
    sweep stamping an irreversible ``failed`` with ``owed = 0``.
    """

    ACQUIRED = "acquired"
    # ``SET NX`` did not set the key and Redis said so: a SECOND owner demonstrably exists. The one
    # and only case in which a consumer stands down before subscribing.
    HELD_ELSEWHERE = "held_elsewhere"
    # Redis did not answer. Ownership is UNKNOWN, not taken — the caller drives the run WITHOUT a
    # lease (§4.1): exclusion here protects against a competitor that cannot exist (the client never
    # subscribes to Hermes, and a run has exactly one consumer per process by construction), while
    # standing down costs the run its billing and its answer in full.
    UNKNOWN = "unknown"


class LeaseRenewal(Enum):
    """Outcome of a lease renewal (see :meth:`AgentRunEventBus.renew_lease`)."""

    RENEWED = "renewed"
    # The key was gone (Redis restart / FLUSHDB / TTL) and we took it back — NOT a loss.
    REACQUIRED = "reacquired"
    LOST = "lost"
    # Redis did not answer: carry on and try again on the next tick (ADR-067 §4.1). Deliberately
    # NOT LOST — see :meth:`AgentRunEventBus.renew_lease`.
    UNKNOWN = "unknown"


_RENEWAL_BY_CODE: Final = {
    1: LeaseRenewal.RENEWED,
    2: LeaseRenewal.REACQUIRED,
    0: LeaseRenewal.LOST,
}


@dataclass(frozen=True)
class RingEvent:
    """One element of the replay ring: the generation it belongs to, its ``seq`` and raw bytes."""

    epoch: str
    seq: int
    data: bytes


def _encode_data(raw: bytes) -> str:
    """JSON string literal holding the raw SSE block, losslessly and ASCII-only.

    Decoded with ``surrogateescape`` and re-encoded the same way on the read side, so ANY byte
    sequence round-trips unchanged — the relay forwards upstream bytes verbatim (ADR-045) and a
    lossy ``errors="replace"`` would corrupt them on the way through Redis. ``ensure_ascii`` (the
    default) keeps the resulting document pure ASCII, which is what makes it safe to hand to a
    ``decode_responses=True`` client and to concatenate inside the Lua script.
    """
    return json.dumps(raw.decode("utf-8", errors="surrogateescape"))


def _decode_data(value: str) -> bytes:
    """Inverse of :func:`_encode_data` — exactly the bytes the consumer read from upstream."""
    return value.encode("utf-8", errors="surrogateescape")


class AgentRunEventBus:
    """Redis-backed ring + pub/sub + lease for one deployment (ADR-067 §3).

    Holds no per-run state: every method takes ``run_id``. Write failures degrade to ``None``/
    ``False`` and are logged — see the module docstring for why they must never raise.
    """

    def __init__(self, client: redis.Redis, settings: Settings) -> None:
        self._redis = client
        self._settings = settings
        self._publish = client.register_script(_PUBLISH_LUA)
        self._release = client.register_script(_RELEASE_LEASE_LUA)
        self._renew = client.register_script(_RENEW_LEASE_LUA)

    # --- keys ---------------------------------------------------------------------------------

    @staticmethod
    def _key(run_id: str, suffix: str) -> str:
        return f"{_KEY_PREFIX}{run_id}:{suffix}"

    @classmethod
    def channel(cls, run_id: str) -> str:
        """Pub/sub channel of a run — the only key the broker needs to name itself."""
        return cls._key(run_id, "pub")

    # --- generation (epoch) -------------------------------------------------------------------

    async def ensure_epoch(self, run_id: str) -> str | None:
        """Return the run's current generation id, creating it if absent. ``None`` on Redis error.

        Called at consumer start (together with the lease) and again on every lease renewal. It is
        deliberately NOT part of the event pipeline (ADR-067 §3.1): if an event could recreate the
        key, "the epoch key is missing" would stop meaning "the generation is gone" — and both the
        stale-cursor rule (§3.2) and downstream close rule 3 (§3.3) are built on that meaning.

        Re-calling it is how a live consumer notices a Redis restart: the key is gone, ``SET NX``
        wins with a FRESH uuid4, and the caller adopts the returned value for subsequent events. If
        a concurrent worker won the race, the ``GET`` returns THEIR value and we adopt it — which is
        the correct outcome either way, since a generation is a property of the ring, not of a
        worker.
        """
        key = self._key(run_id, "epoch")
        ttl = self._settings.agent_run_event_buffer_ttl_seconds
        try:
            created = await self._redis.set(key, uuid.uuid4().hex, nx=True, ex=ttl)
            if created:
                # We created it; re-read anyway is unnecessary — but a GET keeps one code path.
                value = await self._redis.get(key)
            else:
                value = await self._redis.get(key)
        except RedisError:
            logger.warning("agent run epoch ensure failed run_id=%s", run_id)
            return None
        return str(value) if value is not None else None

    async def current_epoch(self, run_id: str) -> str | None:
        """Current generation id, or ``None`` if the key is absent OR Redis failed.

        The two are deliberately NOT distinguished for the caller: both mean "this cursor cannot be
        trusted", and the broker's response to either is the same — treat the cursor as empty and
        replay (ADR-067 §3.2 step 2). Conflating them here keeps that decision in one place.
        """
        try:
            value = await self._redis.get(self._key(run_id, "epoch"))
        except RedisError:
            logger.warning("agent run epoch read failed run_id=%s", run_id)
            return None
        return str(value) if value is not None else None

    # --- write path ---------------------------------------------------------------------------

    async def publish(self, run_id: str, *, epoch: str, raw: bytes) -> int | None:
        """Append one upstream event to the ring and publish it live. Returns its ``seq``.

        The whole ADR-067 §3.1 pipeline in one atomic script: ``INCR seq`` → append → trim from the
        head by BOTH ceilings (events and bytes) → refresh the TTL of every key → ``PUBLISH``.

        ``None`` means the event did not reach Redis. That is a degraded live stream and nothing
        more: the caller MUST carry on with billing, the snapshot and the terminal status, which is
        why no exception escapes (module docstring).
        """
        try:
            seq = await self._publish(
                keys=[
                    self._key(run_id, "events"),
                    self._key(run_id, "seq"),
                    self._key(run_id, "epoch"),
                    self._key(run_id, "bytes"),
                    self.channel(run_id),
                ],
                args=[
                    epoch,
                    _encode_data(raw),
                    self._settings.agent_run_event_buffer_max,
                    self._settings.agent_run_event_buffer_max_bytes,
                    self._settings.agent_run_event_buffer_ttl_seconds,
                ],
            )
        except RedisError:
            # Logged WITHOUT the payload: ring elements are user content (ADR-067 §3.5).
            logger.warning("agent run event publish failed run_id=%s", run_id)
            return None
        return int(seq)

    # --- read path ----------------------------------------------------------------------------

    async def current_seq(self, run_id: str) -> int:
        """Highest ``seq`` issued in the current generation; 0 when the counter is absent/failed.

        Used to detect a cursor "from the future" (ADR-067 §3.2 step 2) — a client whose ``seq``
        exceeds the counter is holding a cursor from a generation whose counter has been reset.
        """
        try:
            value = await self._redis.get(self._key(run_id, "seq"))
        except RedisError:
            logger.warning("agent run seq read failed run_id=%s", run_id)
            return 0
        return int(value) if value is not None else 0

    async def replay(self, run_id: str) -> list[RingEvent]:
        """Whole ring, oldest first. Empty list when absent or on a Redis error.

        Returns EVERY element rather than filtering by cursor: the broker needs the first available
        ``seq`` to decide whether a ``run.truncated`` marker is due (§3.2 step 4), and that decision
        is impossible once the elements below the cursor have already been dropped here.
        Unparseable elements are skipped with a warning — a corrupt element must not abort a replay
        that is otherwise serviceable.
        """
        try:
            # redis-py types lrange as "awaitable OR plain" (it serves sync and async clients from
            # one signature); on an async client it is always the awaitable branch.
            raw_items: list[Any] = await self._redis.lrange(  # type: ignore[misc]
                self._key(run_id, "events"), 0, -1
            )
        except RedisError:
            logger.warning("agent run ring read failed run_id=%s", run_id)
            return []
        events: list[RingEvent] = []
        for item in raw_items:
            parsed = _parse_element(item)
            if parsed is None:
                logger.warning("agent run ring element unparseable run_id=%s", run_id)
                continue
            events.append(parsed)
        return events

    def subscribe(self, run_id: str) -> Any:
        """Pub/sub subscription to the run's live channel. The caller owns closing it.

        Returns the raw ``redis.asyncio`` pubsub object rather than wrapping it: the broker needs
        its polling semantics (a timeout that yields control so timers can run), and a wrapper that
        hid them would have to reimplement exactly that.
        """
        pubsub = self._redis.pubsub()
        return _SubscribedPubSub(pubsub, self.channel(run_id))

    # --- lease (ADR-067 §4) -------------------------------------------------------------------

    async def acquire_lease(self, run_id: str, owner: str) -> LeaseAcquisition:
        """Take ownership of the single upstream subscription. THREE outcomes (ADR-067 §4.1).

        ``SET NX PX`` — the ownership test and the write are one operation, so two workers starting
        the same run cannot both believe they own it. That matters more here than in a usual mutex:
        the Hermes stream is ONE-SHOT, so a second subscriber does not merely duplicate work, it
        consumes a stream that then yields nothing to anyone.

        ⚠️ A ``RedisError`` is ``UNKNOWN``, NOT "held elsewhere". The distinction is the point of
        the enum: "the key was already there" is EVIDENCE of a second owner, while "Redis did not
        answer" is merely the absence of knowledge, and the two deserve opposite responses. One
        value for both made the run's fate turn on a signal that could not distinguish the case it
        was reacting to — the run was abandoned unread precisely when nothing was competing for it.
        Which way to err is settled by cost: standing down while Redis is down loses the run
        ENTIRELY (no billing, no answer, a one-shot stream nobody will ever read), whereas carrying
        on risks a double subscription that ADR-067 §4.1 shows has no possible perpetrator.
        """
        try:
            taken = await self._redis.set(
                self._key(run_id, "lease"),
                owner,
                nx=True,
                px=self._settings.agent_run_consumer_lease_ttl_seconds * 1000,
            )
        except RedisError:
            logger.warning("agent run lease acquire failed run_id=%s", run_id)
            return LeaseAcquisition.UNKNOWN
        return LeaseAcquisition.ACQUIRED if taken else LeaseAcquisition.HELD_ELSEWHERE

    async def renew_lease(self, run_id: str, owner: str) -> LeaseRenewal:
        """Extend our own lease, atomically. Four outcomes, and the middle two are load-bearing.

        * ``RENEWED`` — the key still holds our id; TTL extended.
        * ``REACQUIRED`` — the key is GONE and we took it back. This is what a Redis restart looks
          like from here (it also wipes the ring, ``seq`` and ``epoch``). Treating an absent key as
          a loss would make every live consumer stand down on a restart, and ADR-067 calls a Redis
          restart a routine scenario (§3.5, §5, §6) that the design survives — worse, it would
          defeat the §5 grace period, whose entire purpose is that a restart must NOT cost live
          runs their finalization. Re-taking is safe: nobody else holds the run, only its own
          worker ever renews it, and the ring it lost is re-established under a NEW generation
          (the caller re-runs :meth:`ensure_epoch` and adopts the new value).
        * ``LOST`` — the key holds SOMEONE ELSE's id. We must stand down; renewing a lease we no
          longer hold would keep the rightful owner out. This is EVIDENCE of a second owner and the
          only renewal outcome that ends a run.
        * ``UNKNOWN`` — Redis did not answer. Carry on and retry on the next tick (ADR-067 §4.1).

        ⚠️ ``UNKNOWN`` used to be folded into ``LOST``, and fixing only :meth:`acquire_lease` would
        have been WORSE THAN USELESS. The supervisor cancels the working task on ``LOST``, so with
        Redis down a run that did manage to subscribe would be killed at its first renewal tick
        (``AGENT_RUN_CONSUMER_LEASE_RENEW_SECONDS``, 10s) — after its one-shot stream had already
        been consumed, i.e. spent for nothing rather than merely unread. Both mappings of a
        ``RedisError`` change together or neither does.
        """
        try:
            outcome = await self._renew(
                keys=[self._key(run_id, "lease")],
                args=[owner, self._settings.agent_run_consumer_lease_ttl_seconds * 1000],
            )
        except RedisError:
            logger.warning("agent run lease renew failed run_id=%s", run_id)
            return LeaseRenewal.UNKNOWN
        return _RENEWAL_BY_CODE.get(int(outcome), LeaseRenewal.LOST)

    async def release_lease(self, run_id: str, owner: str) -> None:
        """Drop our lease (terminal event, stall, shutdown). Owner-checked compare-and-delete.

        A bare ``DEL`` would let a worker whose lease already lapsed and was taken over delete the
        NEW owner's lease, handing the run to a third worker while the second is mid-stream.
        """
        try:
            await self._release(keys=[self._key(run_id, "lease")], args=[owner])
        except RedisError:
            logger.warning("agent run lease release failed run_id=%s", run_id)

    async def lease_alive(self, run_id: str) -> bool | None:
        """Whether ANY worker currently holds the run's lease.

        Tri-state on purpose. ``None`` = Redis did not answer, and callers must not read that as
        "no lease": both users of this signal treat an absent lease as evidence that nobody is
        driving the run — the broker closes the client stream (§3.3 rules 3/5) and the reaper
        finalizes the run as an orphan (§5). Turning an unreachable Redis into that verdict would
        mass-finalize live runs, which is precisely the failure the §5 grace period exists to
        prevent. Callers must fail CLOSED on ``None``.
        """
        try:
            held = await self._redis.exists(self._key(run_id, "lease"))
            return int(held) == 1
        except RedisError:
            logger.warning("agent run lease probe failed run_id=%s", run_id)
            return None

    # --- operational --------------------------------------------------------------------------

    async def uptime_seconds(self) -> int | None:
        """Redis ``INFO server`` → ``uptime_in_seconds``; ``None`` when it cannot be determined.

        The orphan sweep's third condition (ADR-067 §5) and FAIL-CLOSED by contract: ``None`` must
        stop the sweep, never permit it. Server uptime rather than "age of this connection" because
        ``redis.asyncio`` pools connections — there is no current one to age.
        """
        try:
            info = await self._redis.info("server")
        except RedisError:
            logger.warning("agent run redis INFO failed")
            return None
        value = info.get("uptime_in_seconds") if isinstance(info, dict) else None
        return int(value) if isinstance(value, int | str) and str(value).isdigit() else None


def parse_ring_element(item: Any) -> RingEvent | None:
    """Public alias — the broker decodes CHANNEL payloads, which are the same elements."""
    return _parse_element(item)


class _SubscribedPubSub:
    """Subscribes on demand so the caller never awaits in a constructor.

    ⚠️ On demand, but NOT "whenever it happens to poll": ADR-067 §3.2 step 1 requires the
    ``SUBSCRIBE`` to precede the ``LRANGE`` replay, and a subscription that exists as an object
    while the actual ``SUBSCRIBE`` is deferred to the first poll satisfies the letter and misses the
    point — that window is exactly where an event is published to nobody and lost without a trace
    (TD-047). Callers that depend on the ordering must call :meth:`ensure_subscribed` explicitly;
    :meth:`get_message` still subscribes if nobody has, so no caller ends up unsubscribed.
    """

    def __init__(self, pubsub: Any, channel: str) -> None:
        self._pubsub = pubsub
        self._channel = channel
        self._subscribed = False

    async def ensure_subscribed(self) -> None:
        """Perform the real ``SUBSCRIBE`` now. Idempotent; raises ``RedisError`` like any call."""
        if not self._subscribed:
            await self._pubsub.subscribe(self._channel)
            self._subscribed = True

    async def get_message(self, **kwargs: Any) -> Any:
        await self.ensure_subscribed()
        return await self._pubsub.get_message(**kwargs)

    async def unsubscribe(self) -> None:
        if self._subscribed:
            await self._pubsub.unsubscribe(self._channel)

    async def aclose(self) -> None:
        await self._pubsub.aclose()


def _parse_element(item: Any) -> RingEvent | None:
    """Decode one ring element, or None when it is not a well-formed element."""
    if isinstance(item, bytes | bytearray):
        item = item.decode("utf-8", errors="surrogateescape")
    if not isinstance(item, str):
        return None
    try:
        payload = json.loads(item)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    epoch = payload.get("epoch")
    seq = payload.get("seq")
    data = payload.get("data")
    if not isinstance(epoch, str) or not isinstance(seq, int) or not isinstance(data, str):
        return None
    return RingEvent(epoch=epoch, seq=seq, data=_decode_data(data))


_bus_client: redis.Redis | None = None


def url_with_db(url: str, db: int) -> str:
    """``REDIS_URL`` retargeted at another logical DB.

    ⚠️ This exists because passing ``db=`` to ``redis.from_url`` DOES NOT WORK when the URL carries
    a path: redis-py parses the URL and then does ``kwargs.update(url_options)``, so the URL wins
    over the explicit argument and the ``db`` kwarg is silently discarded (verified against
    redis-py 5 — ``from_url("redis://h:6379/0", db=1)`` yields db 0). Both the dev and prod
    ``REDIS_URL`` end in ``/0``, so the agent-run contour would have shared the DB of rate limiting
    and idempotency, the isolation ADR-067 §3.5 requires would simply be absent, and nothing
    anywhere would have reported it — the settings validator checks the CONFIG, not what redis-py
    made of it.

    ``redis://`` / ``rediss://`` carry the DB in the path; a ``unix://`` socket URL carries it in
    the ``db`` query parameter. Any other scheme is returned untouched (the caller then gets
    whatever the URL says, which is the only honest answer for a form we do not know).
    """
    parts = urlsplit(url)
    if parts.scheme in ("redis", "rediss"):
        return urlunsplit(parts._replace(path=f"/{db}"))
    if parts.scheme == "unix":
        query = [(k, v) for k, v in parse_qsl(parts.query) if k != "db"]
        query.append(("db", str(db)))
        return urlunsplit(parts._replace(query=urlencode(query)))
    return url


def get_event_bus_redis(settings: Settings) -> redis.Redis:
    """Process-wide client for the agent-run logical DB (``AGENT_RUN_REDIS_DB``, ADR-067 §3.5).

    A SEPARATE logical DB from the rate-limit/idempotency client: the isolation is operational —
    a ``FLUSHDB`` or a ``SCAN`` sweep of one contour must not touch the other. The DB is selected by
    REWRITING the URL, not by a ``db=`` argument — see :func:`url_with_db` for why the obvious form
    silently does nothing.

    ``decode_responses=True`` is safe for this contour because every value written here is ASCII by
    construction (see :func:`_encode_data`); raw event bytes survive inside the JSON, not as it.
    """
    global _bus_client
    if _bus_client is None:
        _bus_client = redis.from_url(  # type: ignore[no-untyped-call]
            url_with_db(settings.redis_url, settings.agent_run_redis_db),
            decode_responses=True,
        )
    return _bus_client
