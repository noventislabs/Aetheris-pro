"""The capability registry must stay honest about what is built."""

from __future__ import annotations

from aetheris.core.capabilities import (
    CAPABILITIES,
    DELIVERED_PHASES,
    CapabilityStatus,
    get_capability,
    is_operational,
)


def test_keys_are_unique() -> None:
    keys = [c.key for c in CAPABILITIES]
    assert len(keys) == len(set(keys))


def test_unknown_capability_fails_closed() -> None:
    # A typo must not read as "operational".
    assert is_operational("does.not.exist") is False
    assert get_capability("does.not.exist") is None


def test_real_execution_capabilities_are_not_claimed_in_this_build() -> None:
    """Nothing that could move real money may report as built.

    ``paper.engine`` left this list in phase 6 and ``risk.engine`` /
    ``paper.autonomous`` in phase 7, which is the distinction the registry
    exists to make: a simulation that places no order is a delivered
    capability, and so is the authority that refuses one, while anything that
    reaches a venue is not.
    """
    for key in (
        "execution.live",
        "falcon.command_center",
        "portfolio.engine",
    ):
        capability = get_capability(key)
        assert capability is not None
        assert capability.status is CapabilityStatus.PLANNED, (
            f"{key} claims {capability.status} but no engine is implemented"
        )


def test_testnet_execution_is_partial_and_does_not_imply_live() -> None:
    """``execution.testnet`` left the list above when phase 8b built it.

    It is the one capability in this build that can move a position, so it gets
    its own boundary rather than sharing the blanket "nothing executes" claim.
    What must stay true: it is not AVAILABLE, it names the testnet host, it
    disclaims autonomous trading, and live remains untouched and PLANNED.
    """
    testnet = get_capability("execution.testnet")
    assert testnet is not None
    assert testnet.status is CapabilityStatus.PARTIAL
    assert "TESTNET only" in testnet.detail
    assert "demo-fapi.binance.com" in testnet.detail
    assert "no autonomous testnet trading" in testnet.detail

    live = get_capability("execution.live")
    assert live is not None
    assert live.status is CapabilityStatus.PLANNED


def test_durable_order_records_are_storage_and_do_not_imply_execution() -> None:
    """``order.persistence`` left the list above when records became durable.

    It was never an execution capability -- storing an order moves no money --
    but it sat in that list because nothing had been built. Now that something
    has, it needs its own boundary: delivered as storage, and still not a claim
    that anything can be sent anywhere.
    """
    persistence = get_capability("order.persistence")
    assert persistence is not None
    assert persistence.status is CapabilityStatus.PARTIAL
    assert persistence.status is not CapabilityStatus.AVAILABLE
    assert "PostgreSQL" in persistence.detail
    # Storage must not be read as a venue claim, and must not be read as paper
    # durability either. Asserted as the disclaimers being present rather than
    # as words being absent: the honest text has to *mention* submission in
    # order to deny it, so banning the word would push the entry towards saying
    # less about its own limits.
    assert "still in memory" in persistence.detail
    # 8b gave the store a submitter, so "nothing submits" -- the old reason for
    # withholding AVAILABLE -- became false. The limit it was pointing at is
    # still real and is what the entry must now name: the retry columns have no
    # writer, so a failed submission is not picked back up.
    assert "no writer" in persistence.detail
    assert "not automatically" in persistence.detail


def test_the_order_engine_claims_recovery_only_for_what_is_actually_durable() -> None:
    """The claim changed when the implementation did, and no further.

    Phase 8a originally had to say crash recovery was NOT delivered, because
    records were in-memory: the thing a recovery pass queries was exactly the
    thing a restart destroyed. Records are durable now and a recovery pass runs
    at startup, so that sentence would itself be the untruth.

    What must stay true is the boundary. Recovery *states* the uncertainty by
    moving interrupted orders to UNKNOWN; it does not resolve it, because
    resolving needs venue evidence and there is still no venue. And the paper
    account is not covered by any of it.
    """
    engine = get_capability("order.engine")
    assert engine is not None
    assert engine.status is CapabilityStatus.PARTIAL
    assert "does not resolve it" in engine.detail
    # "NO venue" was true until 8b gave these records one. The boundary did not
    # disappear, it moved: what must be asserted now is WHICH venue, named
    # rather than implied, and that live is denied in the same sentence.
    assert "TESTNET" in engine.detail
    assert "NO adapter reports LIVE" in engine.detail
    assert "in-memory" in engine.detail  # the paper account, named explicitly
    assert "Paper account state is separate" in engine.detail


def test_autonomous_trading_states_that_it_is_off_by_default() -> None:
    """The registry must not imply a loop is running when none is armed."""
    autonomous = get_capability("paper.autonomous")
    assert autonomous is not None
    assert autonomous.status is CapabilityStatus.AVAILABLE
    assert "OFF by default" in autonomous.detail
    assert "NO real order" in autonomous.detail


def test_the_risk_engine_does_not_claim_to_unlock_leverage() -> None:
    """Phase 7 builds the authority; it does not obtain a venue ceiling.

    Claiming otherwise would be the single most misleading thing this registry
    could say, because the chain still refuses above 1x.
    """
    engine = get_capability("risk.engine")
    assert engine is not None
    assert engine.status is CapabilityStatus.AVAILABLE
    assert "above 1x still fails closed" in engine.detail
    assert "authenticated endpoint" in engine.detail


def test_paper_state_durability_is_disclosed_as_partial() -> None:
    """Durable now, but conditionally, and the entry has to say on what.

    It used to shout that state RESETS ON RESTART, which stopped being true
    when the snapshot store landed. The honest replacement is not silence:
    durability depends on DATABASE_URL, degrades when a write fails, and
    two pieces of state are still deliberately not persisted. A reader who
    only saw "durable" would over-trust it.
    """
    durability = get_capability("paper.persistence")
    assert durability is not None
    assert durability.status is CapabilityStatus.PARTIAL
    assert durability.status is not CapabilityStatus.AVAILABLE
    assert "PostgreSQL" in durability.detail
    assert "restored at startup" in durability.detail
    # The three reasons it is not AVAILABLE, each stated.
    assert "CONDITIONAL ON DATABASE_URL" in durability.detail
    assert "reports IN-MEMORY" in durability.detail
    assert "autonomy arm state" in durability.detail
    assert "RESETS ON RESTART" not in durability.detail


def test_the_paper_engine_states_that_it_places_no_real_order() -> None:
    """The claim is about this engine, not about the whole build.

    It used to justify itself with "no credential exists, and no testnet or
    live path is wired", which phase 8b falsified -- leaving a true claim
    resting on three false ones, which is the harder kind of drift to notice.
    The engine still places no real order. The reason is now its own isolation
    rather than the absence of any venue anywhere in the system.
    """
    engine = get_capability("paper.engine")
    assert engine is not None
    assert engine.status is CapabilityStatus.AVAILABLE
    assert "NO real order" in engine.detail
    assert "no credential exists" not in engine.detail
    assert "no testnet or live path is wired" not in engine.detail


def test_available_capabilities_come_only_from_delivered_phases() -> None:
    """The registry may not run ahead of what has actually shipped.

    DELIVERED_PHASES is edited deliberately when a phase lands, so marking a
    capability available early fails here rather than misleading a user.
    """
    for capability in CAPABILITIES:
        if capability.status is CapabilityStatus.AVAILABLE:
            assert capability.phase in DELIVERED_PHASES, (
                f"{capability.key} claims AVAILABLE but phase "
                f"{capability.phase} has not been delivered"
            )


def test_terminal_is_reported_as_available() -> None:
    terminal = get_capability("ui.market_terminal")
    assert terminal is not None
    assert terminal.status is CapabilityStatus.AVAILABLE


def test_watchlist_is_only_partial() -> None:
    """Browser-local preferences are not a persisted watchlist.

    Claiming AVAILABLE here would tell a user their list is saved when it
    exists only in one browser and disappears when storage is cleared.
    """
    watchlist = get_capability("ui.watchlist")
    assert watchlist is not None
    assert watchlist.status is CapabilityStatus.PARTIAL
    assert "not a persisted" in watchlist.detail


def test_smc_remains_unclaimed() -> None:
    """SMC is still unbuilt in phase 4, and the terminal must still say so.

    Indicators moved to AVAILABLE in the same commit that landed their
    implementation and hand-calculated tests; SMC did not, so it stays PLANNED
    and no overlay is drawn for it.
    """
    capability = get_capability("analysis.smc")
    assert capability is not None
    assert capability.status is CapabilityStatus.PLANNED


def test_indicators_became_available_with_their_implementation() -> None:
    capability = get_capability("analysis.indicators")
    assert capability is not None
    assert capability.status is CapabilityStatus.AVAILABLE
    assert capability.phase == 4


def test_database_persistence_is_partial_and_states_all_three_of_its_limits() -> None:
    """It reported PLANNED, "No database is configured", while 0001-0004 ran.

    Correcting it invites the opposite error, so the entry has to carry three
    limits at once -- each of them a distinct way a reader could over-trust it:
    durability is conditional on DATABASE_URL, it covers order records and not
    the paper account, and phase 1's authentication is still absent.
    """
    database = get_capability("persistence.database")
    assert database is not None
    assert database.status is CapabilityStatus.PARTIAL
    assert database.status is not CapabilityStatus.AVAILABLE
    assert "PostgreSQL" in database.detail
    assert "0001-0005" in database.detail

    # 1. Conditional on configuration, not unconditional.
    assert "DATABASE_URL" in database.detail
    assert "NOT_CONFIGURED" in database.detail

    # 2. Scope. This used to exclude the paper account entirely; the paper
    #    snapshot store brought it in, so the entry now says so rather than
    #    continuing to warn about a limitation that was lifted.
    assert "paper account state" in database.detail
    assert "STILL IN MEMORY" not in database.detail

    # 3. Phase 1 is not finished.
    assert "Argon2id" in database.detail

    # The claim it must never make again.
    assert "No database is configured" not in database.detail


def test_the_registry_does_not_contradict_itself_about_the_database() -> None:
    """Two entries disagreed about whether a database existed.

    ``persistence.database`` said none was configured and nothing survived a
    restart, while ``order.persistence`` in the same tuple described orders
    stored in PostgreSQL behind row-level security. Separate keys, so nothing
    forced them to agree -- and for two commits they did not.
    """
    database = get_capability("persistence.database")
    orders = get_capability("order.persistence")
    assert database is not None
    assert orders is not None
    assert "PostgreSQL" in database.detail
    assert "PostgreSQL" in orders.detail
    assert database.status.is_operational
    assert orders.status.is_operational


def test_exchange_abstraction_names_the_testnet_adapter_and_denies_live() -> None:
    """It said the trading port was "implemented by nothing" after one landed.

    The risk in fixing this is the opposite error, so the assertion is
    two-sided: the adapter is named and bounded to the demo host, and LIVE is
    denied in the same breath rather than merely left unmentioned.
    """
    abstraction = get_capability("exchange.abstraction")
    assert abstraction is not None
    assert abstraction.status is CapabilityStatus.AVAILABLE
    assert "implemented by nothing" not in abstraction.detail
    assert "BinanceTestnetTradingAdapter" in abstraction.detail
    assert "TESTNET" in abstraction.detail
    assert "demo-fapi.binance.com" in abstraction.detail
    assert "NO adapter reports LIVE" in abstraction.detail

    live = get_capability("execution.live")
    assert live is not None
    assert live.status is CapabilityStatus.PLANNED


def test_shipped_but_unfinished_phases_stay_out_of_delivered_phases() -> None:
    """Phases 1 and 8 both ship running code and neither is finished.

    DELIVERED_PHASES gates AVAILABLE and nothing else, so the honest way to
    say "shipped but unfinished" is a PARTIAL capability whose phase is *not*
    listed. Adding 1 or 8 would change no entry's status and would remove the
    guard from every future edit in those phases, which is the one thing the
    list exists to provide.
    """
    assert 1 not in DELIVERED_PHASES
    assert 8 not in DELIVERED_PHASES

    for key in (
        "persistence.database",
        "order.engine",
        "order.persistence",
        "execution.testnet",
    ):
        capability = get_capability(key)
        assert capability is not None
        assert capability.phase not in DELIVERED_PHASES
        assert capability.status is CapabilityStatus.PARTIAL, (
            f"{key} sits in an undelivered phase and must not claim AVAILABLE"
        )


def test_phase_nine_is_not_claimed_in_any_form() -> None:
    """Nothing in phase 9 is built, and no entry may suggest otherwise."""
    assert 9 not in DELIVERED_PHASES
    phase_nine = [c for c in CAPABILITIES if c.phase == 9]
    assert phase_nine, "phase 9 entries disappeared from the registry"
    for capability in phase_nine:
        assert capability.status is CapabilityStatus.PLANNED, (
            f"{capability.key} claims {capability.status} but phase 9 is not started"
        )


def test_rule_based_regime_is_not_confused_with_the_ai_capability() -> None:
    """Both describe "regime classification". Only one is a model.

    ``analysis.regime`` is arithmetic over published thresholds and ships now.
    ``ai.analysis`` is learned classification and does not exist. Marking the
    second available because the first landed is precisely the drift the
    registry is here to prevent.
    """
    regime = get_capability("analysis.regime")
    assert regime is not None
    assert regime.status is CapabilityStatus.AVAILABLE
    assert "ARITHMETIC, NOT A MODEL" in regime.detail
    assert "UNKNOWN" in regime.detail

    ai = get_capability("ai.analysis")
    assert ai is not None
    assert ai.status is CapabilityStatus.PLANNED


def test_setup_scoring_states_what_its_number_is_not() -> None:
    """A 0-100 score is the single most misreadable thing here."""
    score = get_capability("analysis.setup_score")
    assert score is not None
    assert score.status is CapabilityStatus.AVAILABLE
    assert "NOT A PROBABILITY OF" in score.detail
    # And it must say that past performance is not folded in, because a score
    # that quietly included it would read as a forecast.
    assert "reported separately as backtest metrics" in score.detail


def test_optimisation_is_partial_and_names_what_is_missing() -> None:
    """Walk-forward now drives the search. The remaining gaps are integration.

    The entry used to say walk-forward was "NOT yet driven by the optimizer",
    which stopped being true when it started driving it. What is still
    missing is not the capability but the way in: no route, no persistence.
    Those keep it PARTIAL, because a capability nobody can reach is not one
    a user has.
    """
    optimisation = get_capability("optimize.hyperparameters")
    assert optimisation is not None
    assert optimisation.status is CapabilityStatus.PARTIAL
    assert optimisation.status is not CapabilityStatus.AVAILABLE
    assert "train/validation/test" in optimisation.detail
    assert "NOT yet driven by the optimizer" not in optimisation.detail
    assert "walk-forward now DRIVES" in optimisation.detail
    assert "no API route" in optimisation.detail
    assert "NOT persisted" in optimisation.detail


def test_testnet_names_the_order_types_it_does_not_have() -> None:
    """The gaps an operator would otherwise discover by placing a trade.

    Protective levels live in the risk engine, not at the venue, so they stop
    being enforced the moment this process does. That is the kind of thing a
    capability entry has to say out loud rather than leave to be inferred
    from the absence of a field.
    """
    testnet = get_capability("execution.testnet")
    assert testnet is not None
    assert testnet.status is CapabilityStatus.PARTIAL
    assert "NO reduce-only" in testnet.detail
    assert "NO partial close" in testnet.detail
    assert "not enforced if this process stops" in testnet.detail


def test_smc_is_still_unbuilt_after_the_strategy_work() -> None:
    """Scoring and regime are not structure analysis, however adjacent."""
    smc = get_capability("analysis.smc")
    assert smc is not None
    assert smc.status is CapabilityStatus.PLANNED
