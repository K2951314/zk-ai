"""Cooldown policy: how long a credential or deployment rests after a failure.

Cooldowns grow with repeated throttling (exponential, capped) so a persistently
rate-limited key backs off instead of hammering the provider.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from app.core.logging import get_logger

logger = get_logger("credentials.cooldown")

_VALID_SCOPES = frozenset({"credential", "account", "provider"})

#: 「重置时刻刚过」的最小等待（秒），见 :meth:`CooldownPolicy.seconds_until_quota_reset`。
_MIN_RESET_WAIT = 60.0


@dataclass(slots=True)
class CooldownPolicy:
    """Tunables for credential / deployment cooldowns (seconds)."""

    rate_limit_base: float = 60.0
    rate_limit_max: float = 900.0
    rate_limit_multiplier: float = 2.0
    #: Decay window: a 429 arriving longer than this after the previous one is
    #: a *new* burst, not a continuation, so the ladder restarts at the base.
    #: Set ~ 2x rate_limit_max: anything quieter has effectively recovered.
    rate_limit_decay: float = 1800.0
    #: "Quota exhausted" 429s reset on an hours/week cycle, not a per-minute one.
    #: Parking the key for a fixed, long stretch stops every request from
    #: re-sweeping the sibling keys and escalating their counters for nothing.
    quota_cooldown: float = 1800.0
    quota_cooldown_max: float = 14400.0
    #: 「每日/每窗口额度几点重置」，如 ``"00:00"``（本地时间）。
    #: 空字符串 = 不识别（回到旧的固定 ``quota_cooldown`` 行为）。
    #:
    #: 为什么需要（2026-11-10 实测）：ModelScope 是「每日 200 魔粒」，额度耗尽
    #: 后上游回 ``insufficient balance``。旧逻辑把它当普通额度耗尽，长休 1800s
    #: 后重试——离次日重置还有 6 小时的话，接下来 12 次重试全是白撞。
    #: 运营者的观感就是「每天都要我手动启用一次」。
    #: 有了这个字段，长休 = **距下一个重置点的秒数**，到点自然解禁。
    #:
    #: 只影响 ``quota_exhausted`` 这一类（``entitlement exhausted`` /
    #: ``insufficient balance`` / ``额度用尽``），不影响 tpm/rpm 的分钟级退避：
    #: 那是「等一会儿就好」，这是「等窗口翻转」，两种时间尺度不能混。
    quota_reset_at: str = ""
    #: 重置时区偏移（小时），默认 +8（北京时间）。
    #: 不用 zoneinfo：Windows 上 ``tzdata`` 不是标准库的一部分，实测
    #: ``ZoneInfoError: No time zone found with key Asia/Shanghai``——
    #: 为了一个固定偏移引入一个包不值得（与 config_watch 不用 watchdog 同理）。
    quota_reset_utc_offset: float = 8.0
    auth_cooldown: float = 0.0
    server_error_cooldown: float = 15.0
    deployment_cooldown: float = 30.0
    #: Add +/- this ratio of jitter so sibling keys do not retry in lockstep.
    jitter: float = 0.15
    #: Treat a credential with no cooldown set as healthy after this many seconds.
    auto_recover_after: float = 300.0
    #: How far a 429's cooldown reaches, keyed by ``app.retry.classifier``
    #: ``ThrottleKind`` value. Two live decisions in one table:
    #:
    #: 2026-09-28 实测修正：这里原来是 ``quota``/``throughput`` -> ``provider``，
    #: 依据是「商汤 tpm 账号间共享」。**那个依据是错的**：同一时刻账号 04/05
    #: 正以并发 16 稳定烧穿，而 03/06/07/08/09 全部 429——桶明显是按账号分的。
    #: 判成 provider 的代价实测很重：网关一次 kimi-k3 请求撞上 429（那把 Key 与
    #: 积分消耗器共用同一个账号），于是 8 把 sensenova 全部停靠 52s，其中 6 个
    #: 账号本来是健康的，请求被迫去打已经超时死的 nvidia，客户端十几分钟收不到
    #: 任何字节。所以默认退回 ``account``：只停靠能证明共享桶的 Key。
    #:
    #: * ``credential`` - park only this key. Right when the provider counts the
    #:   limit per key (NVIDIA's 40 rpm), because a sibling key on another
    #:   account really can serve the next attempt.
    #: * ``account``    - park every key sharing this credential's
    #:   ``account-*`` tag. Right when the quota is per account, so cooling one
    #:   key while its twin keeps hammering just feeds the next 429.
    #: * ``provider``   - park the whole provider. Right when the bucket is
    #:   shared across accounts: sweeping seven keys against one bucket is the
    #:   19.7%-of-all-attempts spin measured on SenseNova. The request fails over
    #:   to the next deployment instead.
    #:
    #: Keys absent from this mapping fall back to ``credential``.
    throttle_scopes: dict[str, str] = field(
        default_factory=lambda: {
            "quota": "account",
            "throughput": "account",
            "frequency": "credential",
            "unknown": "credential",
        }
    )

    @classmethod
    def from_mapping(cls, data: dict | None) -> CooldownPolicy:
        """Build from the ``cooldown:`` section of ``config.yaml`` (unknown keys ignored)."""
        if not data:
            return cls()
        allowed = set(cls.__dataclass_fields__)
        # ``throttle_scopes`` is handled separately below: it is a
        # *partial-override* table, so it must not be forwarded as a whole-field
        # replacement or supplying one key silently resets the other three.
        kwargs = {
            k: v for k, v in data.items()
            if k in allowed and k != "throttle_scopes"
        }
        policy = cls(**kwargs)
        supplied = data.get("throttle_scopes")
        if isinstance(supplied, dict):
            merged = dict(policy.throttle_scopes)
            for kind, scope in supplied.items():
                # A typo must not *widen* the blast radius: an unrecognised
                # value degrades to the per-key default, which is the pre-existing
                # behaviour, rather than keeping whatever wide default is there.
                if scope in _VALID_SCOPES:
                    merged[str(kind)] = str(scope)
                else:
                    logger.warning(
                        "cooldown.throttle_scopes.%s = %r is not one of %s; "
                        "falling back to credential",
                        kind, scope, sorted(_VALID_SCOPES),
                    )
                    merged[str(kind)] = "credential"
            policy.throttle_scopes = merged
        return policy

    def throttle_scope(self, kind: str) -> str:
        """Cooldown breadth for a throttle *kind* (``credential``/``account``/``provider``)."""
        scope = self.throttle_scopes.get(str(kind), "credential")
        return scope if scope in _VALID_SCOPES else "credential"

    def seconds_until_quota_reset(
        self, now: float | None = None, *, spec: str | None = None
    ) -> float | None:
        """距下一个「额度重置时刻」还有几秒；没配置就返回 None。

        *spec* 是 ``"HH:MM"``，不传则用策略自带的 :attr:`quota_reset_at`。
        供应商级的值（``providers.yaml`` 的 ``options.quota_reset_at``）优先，
        经 ``ErrorInfo.quota_reset_at`` 穿到这里——各家的窗口时刻本来就不同，
        ModelScope 是每日 08:00（实测，见 providers.yaml 注释），
        商汤是 5 小时滚动窗口。

        按 :attr:`quota_reset_utc_offset` 指定的时区解释（默认 +8，北京时间）。
        今天那一刻已经过了就顺延到明天。

        刻意不返回 0：调用方拿到 0 会当「不用等」，而重置刚过、上游还没翻账
        的那几十秒正是最容易误判的窗口。至少给 ``_MIN_RESET_WAIT`` 秒。
        """
        raw = self.quota_reset_at if spec is None else spec
        spec_text = (raw or "").strip()
        if not spec_text:
            return None
        parts = spec_text.split(":")
        if len(parts) != 2:
            logger.warning("quota_reset_at=%r 不是 HH:MM 格式，忽略", raw)
            return None
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError:
            logger.warning("quota_reset_at=%r 不是 HH:MM 格式，忽略", raw)
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            logger.warning("quota_reset_at=%r 超出合法范围，忽略", raw)
            return None

        moment = time.time() if now is None else now
        offset = self.quota_reset_utc_offset * 3600.0
        local = moment + offset
        day_start = local - (local % 86400.0)  # 该时区当天的零点
        target = day_start + hour * 3600.0 + minute * 60.0
        if target <= local:
            target += 86400.0
        return max(_MIN_RESET_WAIT, target - local)

    def _apply_jitter(self, seconds: float) -> float:
        """Spread recovery timestamps so sibling keys do not wake in lockstep."""
        if self.jitter > 0 and seconds > 1:
            seconds *= 1 + random.uniform(-self.jitter, self.jitter)  # noqa: S311 - not security
        return round(max(1.0, seconds), 2)

    def rate_limit_cooldown(self, consecutive_rate_limits: int, retry_after: float | None) -> float:
        """Cooldown for a 429. ``Retry-After`` wins when the provider sends one."""
        if retry_after is not None and retry_after > 0:
            return min(max(retry_after, 1.0), self.rate_limit_max)
        exponent = max(0, consecutive_rate_limits - 1)
        base = self.rate_limit_base * (self.rate_limit_multiplier**exponent)
        base = min(base, self.rate_limit_max)
        if self.jitter > 0:
            base *= 1 + random.uniform(-self.jitter, self.jitter)  # noqa: S311 - not security
        return round(max(1.0, base), 2)

    def cooldown_for(
        self,
        *,
        error_type: str,
        scope: str,
        retry_after: float | None = None,
        consecutive_rate_limits: int = 0,
        configured: float | None = None,
        quota_exhausted: bool = False,
        quota_reset_at: str | None = None,
    ) -> float:
        """Resolve the cooldown duration for a classified error."""
        if configured is not None and configured > 0:
            return configured
        if scope == "deployment":
            return self.deployment_cooldown
        if error_type in {"rate_limit_error", "overloaded"}:
            if quota_exhausted:
                # 配了「重置时刻」就以它为准：额度型长休的全部意义是「等窗口
                # 翻转」，等过头是白撞、等不够也是白撞。
                #
                # 这里**刻意不再套 quota_cooldown_max**：那个上限（默认 4h）是
                # 给「不知道几点恢复」的猜测性长休设的保险丝，而重置时刻是已知
                # 事实。套上去的后果实测过——北京 11:23 算出还要等 20.6h，被截断
                # 成 4h，于是每 4 小时醒一次白撞 429，一天白烧 4 次上游往返。
                # 反过来，上游给了 Retry-After 就以它为准：窗口可能比日重置
                # 更早打开，猜得再准也不如上游自己说。
                reset_wait = self.seconds_until_quota_reset(spec=quota_reset_at)
                if reset_wait is not None:
                    if retry_after is not None and retry_after > 0:
                        return self._apply_jitter(retry_after)
                    return self._apply_jitter(reset_wait)
                # The plan/credit quota will not return in 60s; back it off hard.
                if retry_after is not None and retry_after > 0:
                    return self._apply_jitter(
                        min(max(retry_after, self.quota_cooldown), self.quota_cooldown_max)
                    )
                return self._apply_jitter(self.quota_cooldown)
            return self.rate_limit_cooldown(consecutive_rate_limits, retry_after)
        if error_type in {"authentication_error", "permission_denied"}:
            # Bad key: no timer will help, the credential is parked until an
            # operator (or a health check) puts it back.
            return self.auth_cooldown
        if error_type in {"upstream_error", "timeout", "connection_error"}:
            return self.server_error_cooldown
        return 0.0
