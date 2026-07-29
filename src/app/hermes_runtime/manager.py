"""HermesInstanceManager — lifecycle orchestration of per-user Hermes runtimes (ADR-046 §1).

Coordinates the registry (``hermes_instances``), the ``RuntimeBackend`` (Docker), and envelope
encryption of the per-instance ``API_SERVER_KEY`` (reusing ``byok.kms``, ADR-003). The plaintext
key exists in memory only while building an :class:`InstanceEndpoint` for the caller (Agent Proxy)
and is never persisted or logged (redaction `*key*`).

``ensure_running`` is race-safe: ``user_id`` is the PK and the row is taken ``FOR UPDATE`` so
concurrent calls for the same user serialize on one row rather than creating two containers.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import secrets
import time
import uuid
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.byok.kms import KmsClient
from app.config import (
    HERMES_INSTANCE_LLM_KEY_ENV,
    HERMES_PROVIDER_ALLOWLIST,
    HERMES_PROVIDER_FORBIDDEN,
    HERMES_PROVIDERS_CONFIG_API_KEY,
    HERMES_PROVIDERS_REQUIRING_BASE_URL,
    Settings,
    hermes_provider_key_env,
)
from app.errors import UpstreamError, UpstreamTimeoutError
from app.hermes_runtime.config_yaml import render_instance_config
from app.hermes_runtime.docker_backend import (
    HERMES_API_PORT,
    ContainerRef,
    ProvisionSpec,
    RuntimeBackend,
)
from app.hermes_runtime.registry import HermesInstanceRegistry
from app.models import HermesInstance

logger = logging.getLogger("app.hermes_runtime.manager")

_DEK_LEN = 32
_NONCE_LEN = 12
# Floor on the generated API_SERVER_KEY entropy (ADR-046 §1: >=16 chars). token_urlsafe(n)
# yields ceil(4n/3) chars, so 16 bytes already exceeds 16 chars; we never go below 16 bytes.
_MIN_API_KEY_BYTES = 16
# Bound a single reaper batch so one tick cannot stall on a huge idle backlog.
_REAPER_BATCH_LIMIT = 100
# Postgres SQLSTATE for `lock_timeout` expiry (lock_not_available). Matched on the sqlstate rather
# than on an exception class: the asyncpg driver's error is wrapped by SQLAlchemy's DBAPI adapter,
# so the concrete class depends on the adapter's internals while the SQLSTATE is the contract.
_PG_LOCK_NOT_AVAILABLE = "55P03"


def _is_lock_timeout(exc: DBAPIError) -> bool:
    """True when a DBAPI error is Postgres' ``lock_timeout`` expiry (SQLSTATE 55P03).

    Walks the ``__cause__`` chain: SQLAlchemy's asyncpg adapter re-raises the driver error inside
    its own DBAPI exception, so the asyncpg object carrying ``sqlstate`` may sit one or two links
    down. Any other DB failure returns False and is re-raised untouched — a genuine DB error must
    never be laundered into a 502.
    """
    seen: BaseException | None = exc.orig or exc
    for _ in range(4):  # bounded walk: adapter wrapper → driver error is at most a couple of links
        if seen is None:
            return False
        state = getattr(seen, "sqlstate", None) or getattr(seen, "pgcode", None)
        if state == _PG_LOCK_NOT_AVAILABLE:
            return True
        seen = seen.__cause__
    return False


@dataclass(frozen=True)
class InstanceEndpoint:
    """Address + bearer key of a running instance (consumed by Agent Proxy, in-memory only).

    ``base_url`` is the container DNS URL (``http://hermes-user-<id>:8642``); ``api_key`` is the
    decrypted ``API_SERVER_KEY`` — must not be persisted or logged by the caller.
    """

    base_url: str
    api_key: str


def _container_name(user_id: uuid.UUID) -> str:
    """Per-user container/DNS name with a prefix to avoid collisions on a shared daemon."""
    return f"hermes-user-{user_id}"


class HermesInstanceManager:
    """Orchestrates provision / wake / hibernate / deprovision / health (ADR-046 §1)."""

    def __init__(
        self,
        session: AsyncSession,
        registry: HermesInstanceRegistry,
        backend: RuntimeBackend,
        kms: KmsClient,
        settings: Settings,
    ) -> None:
        self._session = session
        self._registry = registry
        self._backend = backend
        self._kms = kms
        self._settings = settings

    async def ensure_running(
        self, user_id: uuid.UUID, *, deadline: float | None = None
    ) -> InstanceEndpoint:
        """Return a ready endpoint for the user, provisioning or waking the container as needed.

        Flow (ADR-046 §1): row-lock the user's instance; missing → provision; stopped → start +
        ``status=running``; always bump ``last_active_at``; decrypt the key into an
        :class:`InstanceEndpoint`. Race-safe via the PK + ``FOR UPDATE`` (a concurrent caller waits
        for the lock, then observes the freshly provisioned/woken row).

        ``deadline`` (a ``time.monotonic()`` instant) is the CALLER's end-to-end budget — the proxy
        passes the same instant it will later use for the HTTP call itself, so instance resolution
        and the request no longer stack into an unbounded total (the prod worst case: a ~90s wake
        readiness gate followed by a ~94s launch retry cycle). It bounds THREE things here: the wait
        for the row lock (``lock_timeout``, see below), the readiness polls, and — via the caller —
        the HTTP call. It only ever SHORTENS the configured readiness budget, never extends it, and
        the polls honour it by RETURNING rather than by being cancelled, so every ADR-056/ADR-062
        cleanup path (container removal, conditional re-hibernate) still runs and no half-owned
        ``provisioning`` row is left behind. ``None`` keeps the pre-existing behaviour.

        The lock wait is bounded because it happens BEFORE any HTTP client exists: a caller queued
        behind the row lock cannot be rescued by any request timeout, and Postgres waits forever by
        default. Exhausting it is reported as ``upstream_timeout`` — the instance is busy serving
        someone else's call and this request got no answer in its budget, which is exactly what the
        code means.
        """
        try:
            row = await self._registry.get_for_update(
                user_id, lock_timeout_ms=self._lock_timeout_ms(deadline)
            )
        except DBAPIError as exc:
            if not _is_lock_timeout(exc):
                raise
            # The statement aborted the transaction — roll back before the caller's teardown so the
            # session is reusable (the audit/agent_runs writes of a later request share it).
            await self._session.rollback()
            logger.warning("hermes instance row lock wait timed out user_id=%s", user_id)
            raise UpstreamTimeoutError("hermes instance is busy with another request") from exc
        if row is None:
            return await self._provision_locked(user_id, deadline=deadline)

        if row.status == "provisioning" and self._is_stale_provisioning(row):
            # TD-031: a `provisioning` row older than the stale threshold is the residue of a crash
            # between create_provisioning and mark_running (endpoint=NULL → DNS fallback to a
            # container that may not exist → a clean 502). Replay the full lifecycle under the held
            # row-lock: deprovision (remove any half-created container by container_id, then drop
            # the row) and provision afresh. Idempotent under the user_id PK; provision rewrites
            # container_id/endpoint/api_key_enc. A fresh provisioning row (younger than the
            # threshold) is left as a live concurrent provisioning (handled below, unchanged).
            logger.warning(
                "hermes instance stale provisioning, replaying user_id=%s container_id=%s",
                user_id,
                row.container_id,
            )
            await self._replay_stale_provisioning(row)
            return await self._provision_locked(user_id, deadline=deadline)

        if row.status == "provisioning":
            # ADR-056 §2: a FRESH `provisioning` row (younger than stale) is a concurrent
            # provisioner currently in its readiness-poll. Do NOT proxy to a possibly-unready
            # instance nor re-provision — release this row-lock (else the owner's mark_running
            # UPDATE would deadlock against our FOR UPDATE) and wait for the row to reach `running`.
            await self._session.rollback()
            return await self._await_concurrent_ready(user_id, deadline=deadline)

        if row.status == "stopped":
            # ADR-062 §1: wake behind a readiness-gate (short-transaction, own commit/poll cycle).
            return await self._wake_locked(row, deadline=deadline)

        # running (the FAST PATH, and the one a wedged instance takes — its container is up, so the
        # row says `running` and nothing above fires): refresh the activity stamp and COMMIT.
        #
        # The commit is load-bearing, not tidiness. This branch used to `flush` only, so the
        # `FOR UPDATE` lock taken above was held until the request session's teardown — i.e. across
        # the caller's entire HTTP call to the instance. On a wedged instance that is the full
        # launch budget, and every OTHER request from the same user then queued on the lock and hung
        # too, with no timeout able to help (see the docstring). Waking and provisioning already
        # release the lock before their long phase (ADR-062 §1 step 4, ADR-056 §1); this path is now
        # consistent with them.
        #
        # Nothing is lost by committing here: this branch's only write is `touch_active` (an
        # activity stamp, independently useful and never rolled back for correctness), and the
        # callers reach it with no uncommitted work of their own — the policy gate only READS, the
        # blocked branches return before ensure_running, /resume commits its CAS first, and every
        # write that follows (audit, agent_runs, snapshots) happens after this returns.
        await self._registry.touch_active(user_id)
        await self._session.flush()
        row = await self._reload(user_id)
        # Decrypt BEFORE the commit while the row is guaranteed loaded (expire_on_commit=False makes
        # this safe either way, but the order removes the dependency on that setting).
        endpoint = self._endpoint_from_row(row)
        await self._session.commit()
        return endpoint

    async def provision(
        self, user_id: uuid.UUID, *, deadline: float | None = None
    ) -> InstanceEndpoint:
        """Provision a brand-new instance for the user (no existing row expected).

        Public entry point; ``ensure_running`` is the normal path. Delegates to the locked
        provisioning routine so the ``ON CONFLICT`` race guard applies here too. ``deadline``
        has the same meaning as in :meth:`ensure_running` (caller's end-to-end budget).
        """
        existing = await self._registry.get_for_update(user_id)
        if existing is not None:
            # Idempotent: an instance already exists — return its endpoint instead of duplicating.
            return self._endpoint_from_row(existing)
        return await self._provision_locked(user_id, deadline=deadline)

    async def _provision_locked(
        self, user_id: uuid.UUID, *, deadline: float | None = None
    ) -> InstanceEndpoint:
        """Insert the provisioning row (race-safe), start the container, gate on readiness.

        Flow (ADR-056 §1): insert the ``provisioning`` row (encrypted key first, so it is never
        NULL) and COMMIT it BEFORE ``docker run`` — the committed row is the race arbiter (a
        concurrent ``ensure_running`` sees it and waits, ADR-056 §2). ``docker run`` then starts the
        container; we POLL ``health`` until 200 (cold-start gate) and only then ``mark_running``. On
        timeout we remove the container and drop the row (no inconsistent ``running``) and raise. On
        ``ON CONFLICT`` we yield to the winning provisioner and wait for ITS readiness.
        """
        self._require_provision_config()
        api_key = self._generate_api_key()
        api_key_enc, encrypted_dek, nonce = self._encrypt_key(api_key)

        inserted = await self._registry.create_provisioning(
            user_id,
            api_key_enc=api_key_enc,
            encrypted_dek=encrypted_dek,
            nonce=nonce,
        )
        if inserted is None:
            # A concurrent caller won the insert and is now driving the readiness-poll. Do NOT
            # re-provision or return a possibly-unready endpoint — wait for that row to become
            # `running` (ADR-056 §2), then return its endpoint.
            await self._session.rollback()
            return await self._await_concurrent_ready(user_id, deadline=deadline)

        # ADR-056 §1: commit the `provisioning` row BEFORE docker run so it is the visible race
        # arbiter while this caller proceeds (a concurrent ensure_running observes it and waits).
        await self._session.commit()

        name = _container_name(user_id)
        spec = ProvisionSpec(
            name=name,
            image=self._settings.hermes_image,
            env=self._container_env(api_key),
            volume_host_path=self._volume_path(user_id),
            network=self._settings.hermes_docker_network,
            # ADR-055: pin the instance LLM via config.yaml model.* (provider/model/base_url). The
            # bare HERMES_MODEL is joined with the provider into model.default inside the renderer.
            # §6: api_key is passed so config-api-key providers (custom) emit model.api_key as the
            # env-ref ${HERMES_INSTANCE_LLM_KEY} (the key value is NEVER written to the file).
            config_yaml=render_instance_config(
                toolset=self._settings.hermes_default_toolset(),
                provider=self._settings.hermes_llm_provider.strip(),
                model=self._settings.hermes_model.strip(),
                base_url=self._settings.hermes_llm_base_url.strip(),
                api_key=self._settings.hermes_llm_api_key,
            ),
        )
        container_ref = await self._backend.provision(spec)

        # ADR-056 §1: readiness gate. Poll GET /health (Bearer api_key) until 200 within the budget,
        # NOT holding a DB transaction during the poll (health is an HTTP call; the provisioning row
        # is already committed). Only after 200 do we mark_running. On timeout: remove the container
        # and drop the row (cleanup → no inconsistent state), then raise → 502 at the proxy.
        if not await self._wait_for_ready(container_ref.endpoint, api_key, deadline=deadline):
            await self._cleanup_failed_provision(user_id, container_ref)
            # A readiness budget that runs out means the instance never answered a probe — the
            # `upstream_timeout` case by definition, whichever budget (own or caller's) expired
            # first. Reported as such so the code keeps its meaning on the cold-start path too.
            raise UpstreamTimeoutError("hermes instance did not become ready in time")

        await self._registry.mark_running(
            user_id,
            container_id=container_ref.container_id,
            endpoint=container_ref.endpoint,
            port=None,
        )
        await self._session.commit()
        logger.info(
            "hermes instance provisioned user_id=%s container_id=%s",
            user_id,
            container_ref.container_id,
        )
        return InstanceEndpoint(base_url=container_ref.endpoint, api_key=api_key)

    async def _wake_locked(
        self, row: HermesInstance, *, deadline: float | None = None
    ) -> InstanceEndpoint:
        """Wake a ``stopped`` instance behind the readiness-gate (ADR-062 §1). Short-transaction.

        Flow (mirrors ``_provision_locked``'s gate on the wake path, ADR-062 §1):
        0. ``T := now(UTC)`` — start of THIS wake attempt (the stale-anchor + the cleanup guard).
        1-2. Under the held row-lock: ``RuntimeBackend.start`` the hibernated container (returns on
             process start, NOT on api_server readiness).
        3-4. ``mark_provisioning(user_id, provisioning_started_at=T)`` (``stopped → provisioning``,
             keeping ``container_id``/``endpoint``/``port``) then ``commit`` — this RELEASES the
             row-lock and makes the ``provisioning`` row the race arbiter (a concurrent
             ``ensure_running`` waits via ``_await_concurrent_ready``). The lock is NEVER held
             across the poll (ADR-054/ADR-056 §transactional-invariant).
        5-6. ``_wait_for_ready`` polls ``health`` with NO DB held; on 200 → ``mark_running`` +
             ``commit`` → return the endpoint.
        7.   Timeout → CONDITIONAL re-hibernate guarded by attempt identity (§1b):
             ``mark_stopped_if_provisioning(user_id, T)``. rowcount=1 (still our attempt) → stop the
             (valid) container best-effort + ``commit``; rowcount=0 (a concurrent replay/provision
             took the row, maybe a NEW ``running`` container) → leave row+container untouched +
             ``rollback``. Either way raise ``UpstreamError`` → 502. The container is NOT removed
             (a valid hibernated instance; volume/memory preserved) — the next request re-wakes.

        ``deadline`` (the caller's end-to-end budget) only shortens step 5-6; step 7 is unchanged
        and still runs, so a deadline-shortened wake leaves exactly the state an ordinary readiness
        timeout does. The config invariant ``launch budget >= ready + proxy`` keeps that shortening
        to the docker-start time, never to a slice of the cold start itself.
        """
        user_id = row.user_id
        container_ref = self._container_ref_from_row(row)
        endpoint = row.endpoint or container_ref.endpoint
        # Decrypt the bearer BEFORE the commit (returned to the caller on success; used only for
        # the health probe otherwise). In-memory only — never persisted/logged (ADR-003 redaction).
        api_key = self._decrypt_key(row)
        attempt_started_at = datetime.datetime.now(datetime.UTC)

        await self._backend.start(container_ref)
        await self._registry.mark_provisioning(user_id, provisioning_started_at=attempt_started_at)
        # ADR-062 §1 step 4: commit the provisioning arbiter, releasing the row-lock BEFORE polling.
        await self._session.commit()

        if await self._wait_for_ready(endpoint, api_key, deadline=deadline):
            await self._registry.mark_running(
                user_id,
                container_id=container_ref.container_id,
                endpoint=endpoint,
                port=row.port,
            )
            await self._session.commit()
            logger.info("hermes instance woken user_id=%s", user_id)
            return InstanceEndpoint(base_url=endpoint, api_key=api_key)

        # ADR-062 §1 step 7 / §1b: readiness timeout → conditional re-hibernate (identity guard T).
        rows = await self._registry.mark_stopped_if_provisioning(
            user_id, provisioning_started_at=attempt_started_at
        )
        if rows == 1:
            # We still own this attempt → honest re-hibernate. Stop the (valid) container
            # best-effort (a stop failure must not mask the timeout). No remove; volume/memory kept.
            try:
                await self._backend.stop(container_ref)
            except UpstreamError:
                logger.warning(
                    "hermes wake cleanup: container stop failed after readiness timeout "
                    "user_id=%s",
                    user_id,
                )
            await self._session.commit()
            logger.warning("hermes instance wake timed out, re-hibernated user_id=%s", user_id)
        else:
            # rowcount=0: a concurrent replay/provision already owns the row (possibly a NEW running
            # container). Touching row or container would clobber a healthy instance (§1b MAJOR).
            await self._session.rollback()
            logger.warning(
                "hermes instance wake timed out, row taken by concurrent actor user_id=%s",
                user_id,
            )
        # Same rule as the cold-start gate: the wake ended on a deadline with the instance silent.
        raise UpstreamTimeoutError("hermes instance did not become ready in time")

    async def stop_idle(self, threshold_seconds: int) -> int:
        """Stop running instances idle longer than the threshold (reaper). Returns the count.

        State lives in the DB (``hermes_instances``), so the reaper survives process restarts. A
        per-tick batch limit bounds the work. A backend stop failure on one instance does not abort
        the batch — it is logged and the row is left ``running`` for the next tick.
        """
        idle = await self._registry.list_idle_running(threshold_seconds, _REAPER_BATCH_LIMIT)
        stopped = 0
        for row in idle:
            container_ref = self._container_ref_from_row(row)
            try:
                await self._backend.stop(container_ref)
            except UpstreamError:
                logger.warning("reaper: stop failed, leaving running user_id=%s", row.user_id)
                continue
            await self._registry.mark_stopped(row.user_id)
            stopped += 1
        if stopped:
            await self._session.flush()
            logger.info("reaper stopped %d idle hermes instance(s)", stopped)
        return stopped

    async def deprovision(self, user_id: uuid.UUID) -> None:
        """Remove the container and the registry row. The host volume is kept (Q-046-2)."""
        row = await self._registry.get_for_update(user_id)
        if row is None:
            return
        if row.container_id:
            await self._backend.remove(self._container_ref_from_row(row))
        await self._registry.delete(user_id)
        await self._session.flush()
        logger.info("hermes instance deprovisioned user_id=%s", user_id)

    async def health(self, user_id: uuid.UUID) -> bool:
        """Probe the instance's ``GET /health``. False if not provisioned/unreachable."""
        row = await self._registry.get(user_id)
        if row is None or not row.endpoint:
            return False
        api_key = self._decrypt_key(row)
        try:
            return await self._backend.health(row.endpoint, api_key)
        finally:
            del api_key

    # --- internals ---------------------------------------------------------------------------

    def _is_stale_provisioning(self, row: HermesInstance) -> bool:
        """True when a `provisioning` row is older than the stale threshold (TD-031, ADR-062 §1a).

        Anchored on ``provisioning_started_at`` — the start of the CURRENT provisioning attempt
        (set by ``create_provisioning`` on cold-start AND by ``mark_provisioning`` on wake), NOT on
        the immutable ``created_at``. For a woken instance ``created_at`` is the ORIGINAL provision
        time (hours/days ago); anchoring on it would falsely flag a live wake-wait as stale and let
        a concurrent ``ensure_running`` replay-remove its in-flight container (ADR-062 §1a). Falls
        back to ``created_at`` defensively when the anchor is NULL (pre-0017 rows / non-provisioning
        residue). A non-positive threshold disables the replay (any provisioning row is fresh).
        """
        threshold = self._settings.hermes_provisioning_stale_seconds
        if threshold <= 0:
            return False
        anchor = row.provisioning_started_at or row.created_at
        age = datetime.datetime.now(datetime.UTC) - anchor
        return age > datetime.timedelta(seconds=threshold)

    async def _replay_stale_provisioning(self, row: HermesInstance) -> None:
        """Tear down a stale `provisioning` row under the held lock (TD-031, deprovision half).

        Removes a possibly half-created container (only when ``container_id`` is set) and deletes
        the registry row, mirroring :meth:`deprovision` but operating on the already-row-locked row
        so we do not re-fetch (and re-lock) it. The host volume is preserved (Q-046-2). Followed by
        a fresh ``_provision_locked`` in the caller (full replay).
        """
        if row.container_id:
            await self._backend.remove(self._container_ref_from_row(row))
        await self._registry.delete(row.user_id)
        await self._session.flush()

    def _lock_timeout_ms(self, caller_deadline: float | None) -> int | None:
        """Postgres ``lock_timeout`` for the ``FOR UPDATE``, in ms: what is left of the budget.

        ``None`` (no caller budget) keeps the pre-existing unbounded wait. A budget already spent
        floors at 1ms rather than 0 — 0 means "wait forever" in Postgres, so rounding an exhausted
        budget down to it would turn the tightest deadline into no deadline at all, the one
        direction this must never fail in.
        """
        if caller_deadline is None:
            return None
        return max(int((caller_deadline - time.monotonic()) * 1000), 1)

    def _poll_deadline(self, caller_deadline: float | None) -> float:
        """The instant a readiness poll must stop: own budget, shortened by the caller's deadline.

        The caller's end-to-end budget can only ever make the wait SHORTER — a caller may not buy
        the gate more time than ADR-056 grants it (that budget is a property of the cold start, not
        of the request). ``None`` ⇒ the configured budget alone.

        In a VALID configuration this clamp is insurance, not the working mechanism: the config
        invariant guarantees ``budget - ready >= 2 × proxy``, so the caller's deadline is still in
        the future when the poll starts and only an unbounded phase BEFORE the poll (a slow
        ``docker run``/``start`` — TD-041) can make it bite. The mechanism that actually fixed the
        stacking is that the caller's HTTP call shares this same deadline. Note also that the two
        poll loops are ALTERNATIVE branches (own provision/wake vs. waiting on a concurrent one),
        so they never sum: the real prod worst case was one readiness gate (~90s) followed by the
        launch retry cycle (~94s), not two gates.
        """
        own = time.monotonic() + self._settings.hermes_provision_ready_timeout_seconds
        return own if caller_deadline is None else min(own, caller_deadline)

    async def _wait_for_ready(
        self, endpoint: str, api_key: str, *, deadline: float | None = None
    ) -> bool:
        """Poll ``health`` until 200 within the readiness budget (ADR-056 §1). No DB held.

        Each iteration is one ``backend.health(endpoint, api_key)`` (its own bounded HTTP timeout);
        not-ready/error → sleep the interval and retry until the total budget elapses. Returns True
        on the first 200, False if the budget is exhausted. Pure HTTP — no DB transaction is held
        during the wait (the provisioning row was already committed). A non-positive budget disables
        the gate (single probe → backward-compatible immediate behaviour).

        ``deadline`` (the caller's end-to-end budget) can only shorten the wait. Exhausting it is
        reported as a plain not-ready (False), NOT as a cancellation: the caller's cleanup — remove
        on cold start, conditional re-hibernate on wake (ADR-062 §1b) — must still run, and both are
        correct here (an instance that did not answer a probe within the budget is not usable for
        this request either way). At least ONE probe always happens, so a caller arriving with an
        already-spent budget still gets an answer instead of a guess.
        """
        stop_at = self._poll_deadline(deadline)
        interval = max(self._settings.hermes_provision_ready_interval_seconds, 1)
        while True:
            try:
                if await self._backend.health(endpoint, api_key):
                    return True
            except UpstreamError:
                # health probe transport failure — treat as not-ready and keep polling.
                pass
            if time.monotonic() + interval > stop_at:
                return False
            await asyncio.sleep(interval)

    async def _cleanup_failed_provision(
        self, user_id: uuid.UUID, container_ref: ContainerRef
    ) -> None:
        """Remove an unready container and drop its registry row (ADR-056 §1 timeout cleanup).

        Leaves NO inconsistent ``running`` row and NO lingering ``provisioning`` row: the next
        ``ensure_running`` starts a clean provision. The host volume is preserved (only the
        container is removed). Best-effort container removal — a remove failure must not mask the
        underlying readiness timeout, so it is logged and the row is still dropped.
        """
        try:
            await self._backend.remove(container_ref)
        except UpstreamError:
            logger.warning(
                "hermes cleanup: container remove failed after readiness timeout user_id=%s",
                user_id,
            )
        await self._registry.delete(user_id)
        await self._session.commit()
        logger.warning(
            "hermes instance provisioning timed out, cleaned up user_id=%s container_id=%s",
            user_id,
            container_ref.container_id,
        )

    async def _await_concurrent_ready(
        self, user_id: uuid.UUID, *, deadline: float | None = None
    ) -> InstanceEndpoint:
        """Wait for a concurrent provisioner's row to reach ``running`` (ADR-056 §2). No DB held.

        Re-reads the registry row (a short read each poll, then commit to release the connection)
        until it is ``running`` (→ return its endpoint) or the readiness budget elapses (→ raise).
        Does NOT drive cleanup — the owning ``_provision_locked`` handles its own timeout.
        Idempotent: never creates a second container.

        ``expire_all()`` is called BEFORE each read: with ``expire_on_commit=False`` (db.py) the
        prior ``get``/``commit`` leaves the row cached in the session identity-map at its stale
        ``provisioning`` status, so a plain re-``get`` would NEVER observe the winner's committed
        ``provisioning → running`` transition and the loser would block the whole budget → a false
        502 (real production race found by qa). Expiring first forces every poll to re-SELECT the
        committed row from the DB.

        ``deadline`` (the caller's end-to-end budget) can only shorten the wait — a loser must not
        be able to burn the whole readiness budget on top of what the caller has already spent.
        """
        stop_at = self._poll_deadline(deadline)
        interval = max(self._settings.hermes_provision_ready_interval_seconds, 1)
        while True:
            # Drop cached identity-map state so this poll re-reads the committed row (docstring).
            self._session.expire_all()
            row = await self._registry.get(user_id)
            await self._session.commit()  # release the connection between polls (no DB held)
            if row is None:
                # The concurrent provisioner cleaned up after its own timeout → nothing to wait on.
                # A DEFINITE outcome, not a deadline: the generic 502 is the honest code here.
                raise UpstreamError("hermes instance failed to become ready")
            if row.status == "running":
                return self._endpoint_from_row(row)
            if time.monotonic() + interval > stop_at:
                # We stopped because time ran out, not because anything answered → upstream_timeout.
                raise UpstreamTimeoutError("hermes instance did not become ready in time")
            await asyncio.sleep(interval)

    def _require_provision_config(self) -> None:
        """Fail fast on invalid provisioning config BEFORE docker run (ADR-055 §2/§5).

        Validates image + provider key + the LLM model contract so a misconfiguration surfaces as a
        clear control-plane error in the logs rather than an opaque runtime 401 from the instance:
        - HERMES_IMAGE / HERMES_LLM_API_KEY non-empty (existing);
        - HERMES_LLM_PROVIDER ∈ allowlist AND not forbidden (`auto`); `openai` is not in the
          allowlist → rejected here;
        - HERMES_MODEL non-empty (empty model = the "Model: (empty)" 401 bug);
        - providers that require a base_url (custom/azure-foundry) ⇒ HERMES_LLM_BASE_URL non-empty.
        """
        if not self._settings.hermes_image.strip():
            raise UpstreamError("hermes runtime is not configured (HERMES_IMAGE)")
        if not self._settings.hermes_llm_api_key.strip():
            raise UpstreamError("hermes runtime is not configured (HERMES_LLM_API_KEY)")
        provider = self._settings.hermes_llm_provider.strip()
        if provider in HERMES_PROVIDER_FORBIDDEN or provider not in HERMES_PROVIDER_ALLOWLIST:
            allowed = ", ".join(sorted(HERMES_PROVIDER_ALLOWLIST - HERMES_PROVIDER_FORBIDDEN))
            raise UpstreamError(
                f"hermes runtime is not configured (HERMES_LLM_PROVIDER={provider!r} invalid; "
                f"allowed: {allowed})"
            )
        if not self._settings.hermes_model.strip():
            raise UpstreamError("hermes runtime is not configured (HERMES_MODEL)")
        if (
            provider in HERMES_PROVIDERS_REQUIRING_BASE_URL
            and not self._settings.hermes_llm_base_url.strip()
        ):
            raise UpstreamError(
                f"hermes runtime is not configured (HERMES_LLM_BASE_URL required for "
                f"provider {provider!r})"
            )

    def _generate_api_key(self) -> str:
        """CSPRNG ``API_SERVER_KEY`` (ADR-046 §1, >=16 chars). URL-safe, no padding issues."""
        nbytes = max(self._settings.hermes_api_key_bytes, _MIN_API_KEY_BYTES)
        return secrets.token_urlsafe(nbytes)

    def _encrypt_key(self, api_key: str) -> tuple[bytes, bytes, bytes]:
        """Envelope-encrypt the key (ADR-003): random DEK → AES-256-GCM(key); DEK → KMS."""
        dek = secrets.token_bytes(_DEK_LEN)
        nonce = secrets.token_bytes(_NONCE_LEN)
        try:
            aead = AESGCM(dek)
            api_key_enc = aead.encrypt(nonce, api_key.encode("utf-8"), None)
            encrypted_dek = self._kms.encrypt_dek(dek)
        finally:
            dek = b"\x00" * _DEK_LEN
        return api_key_enc, encrypted_dek, nonce

    def _decrypt_key(self, row: HermesInstance) -> str:
        """Decrypt the stored key in-memory (ADR-003). Caller must not persist/log the result."""
        dek = self._kms.decrypt_dek(bytes(row.encrypted_dek))
        try:
            aead = AESGCM(dek)
            plaintext = aead.decrypt(bytes(row.nonce), bytes(row.api_key_enc), None)
        finally:
            dek = b"\x00" * _DEK_LEN
        return plaintext.decode("utf-8")

    def _container_env(self, api_key: str) -> dict[str, str]:
        """Container env per the Hermes API-server contract (ADR-055 §4/§6 / ADR-056 §4, hermes/02).

        API_SERVER_* + the LLM key (channel depends on the provider) + HERMES_UID/HERMES_GID.
        ``LLM_MODEL`` and ``LLM_PROVIDER`` are NOT set: the image ignores ``LLM_MODEL`` and resolves
        the provider from ``config.yaml`` ``model.provider`` (ADR-055) — duplicating them in env
        risks drift. HERMES_UID/HERMES_GID (ADR-056 §4) make the image's s6 stage2 chown /opt/data
        to the api process's uid/gid → single volume owner, no reuse PermissionError. No value here
        is logged (redaction `*key*`).

        LLM key channel (ADR-055 §4/§6):
        - config-api-key provider (``custom`` ∈ HERMES_PROVIDERS_CONFIG_API_KEY): the image has no
          env-key (env_vars=()), so the key goes under the FIXED neutral name
          HERMES_INSTANCE_LLM_KEY (referenced from config.yaml as ${...}); ``<PROVIDER>_API_KEY``
          is NOT set.
        - env-key provider (anthropic/openrouter/…): the key goes under its mapped
          ``<PROVIDER>_API_KEY`` name (explicit map, not f"{provider.upper()}_API_KEY").
        """
        provider = self._settings.hermes_llm_provider.strip()
        env: dict[str, str] = {
            "API_SERVER_ENABLED": "true",
            "API_SERVER_KEY": api_key,
            "API_SERVER_HOST": "0.0.0.0",
            "API_SERVER_PORT": str(HERMES_API_PORT),
            # ADR-056 §4(1): align the instance volume owner with the api process uid/gid.
            "HERMES_UID": str(self._settings.hermes_uid),
            "HERMES_GID": str(self._settings.hermes_gid),
        }
        if provider in HERMES_PROVIDERS_CONFIG_API_KEY:
            # ADR-055 §6: key via config.yaml model.api_key env-ref; no <PROVIDER>_API_KEY.
            env[HERMES_INSTANCE_LLM_KEY_ENV] = self._settings.hermes_llm_api_key
        else:
            env[hermes_provider_key_env(provider)] = self._settings.hermes_llm_api_key
        return env

    def _volume_path(self, user_id: uuid.UUID) -> str:
        """Per-user HERMES_HOME volume host path: ``<HERMES_VOLUME_ROOT>/<user_id>``."""
        root = self._settings.hermes_volume_root.rstrip("/")
        return f"{root}/{user_id}"

    def _container_ref_from_row(self, row: HermesInstance) -> ContainerRef:
        return ContainerRef(
            container_id=row.container_id or "",
            name=_container_name(row.user_id),
            endpoint=row.endpoint or f"http://{_container_name(row.user_id)}:{HERMES_API_PORT}",
        )

    def _endpoint_from_row(self, row: HermesInstance) -> InstanceEndpoint:
        base_url = row.endpoint or self._container_ref_from_row(row).endpoint
        return InstanceEndpoint(base_url=base_url, api_key=self._decrypt_key(row))

    async def _reload(self, user_id: uuid.UUID) -> HermesInstance:
        row = await self._registry.get(user_id)
        if row is None:  # pragma: no cover - row must exist after ensure_running
            raise UpstreamError("hermes instance state inconsistent")
        return row
