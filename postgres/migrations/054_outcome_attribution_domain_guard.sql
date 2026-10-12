-- Enforce the boundary between theoretical outcome ownership and broker
-- attribution projection.  Migration 037 already provides the durable
-- execution_v2.broker_close_fact ledger; this guard protects the compatibility
-- projection on strategy.entry_signal_outcomes without changing its shape.
CREATE OR REPLACE FUNCTION platform.enforce_entry_signal_outcome_domain_separation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    domain text := current_setting('platform.outcome_writer_domain', true);
BEGIN
    IF domain IS NULL OR domain NOT IN ('LEGACY_COMPAT', 'RESOLVER_PRIMARY', 'BROKER_ATTRIBUTION') THEN
        RAISE EXCEPTION 'canonical outcome writer domain is missing or invalid: %', coalesce(domain, '<NULL>');
    END IF;

    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'canonical outcome rows are immutable and cannot be deleted';
    END IF;

    IF TG_OP = 'INSERT' AND domain = 'BROKER_ATTRIBUTION' THEN
        RAISE EXCEPTION 'broker attribution cannot insert canonical outcome rows';
    END IF;

    IF TG_OP = 'INSERT' AND domain IN ('LEGACY_COMPAT', 'RESOLVER_PRIMARY') AND (
        NEW.execution_outcome IS NOT NULL OR
        NEW.broker_realized_r IS NOT NULL OR
        NEW.broker_fill_timestamp IS NOT NULL OR
        NEW.broker_exit_timestamp IS NOT NULL OR
        NEW.broker_exit_reason IS NOT NULL OR
        NEW.attribution_status IS DISTINCT FROM 'UNCLASSIFIED' OR
        NEW.attribution_error IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'theoretical outcome writer cannot insert broker attribution fields';
    END IF;

    IF TG_OP = 'UPDATE' AND domain = 'BROKER_ATTRIBUTION' AND (
        NEW.status IS DISTINCT FROM OLD.status OR
        NEW.realized_r IS DISTINCT FROM OLD.realized_r OR
        NEW.exit_timestamp IS DISTINCT FROM OLD.exit_timestamp OR
        NEW.strategy_outcome IS DISTINCT FROM OLD.strategy_outcome OR
        NEW.strategy_realized_r IS DISTINCT FROM OLD.strategy_realized_r OR
        NEW.strategy_exit_timestamp IS DISTINCT FROM OLD.strategy_exit_timestamp OR
        NEW.outcome_type IS DISTINCT FROM OLD.outcome_type OR
        NEW.outcome_contract_version IS DISTINCT FROM OLD.outcome_contract_version OR
        NEW.source IS DISTINCT FROM OLD.source OR
        NEW.source_kind IS DISTINCT FROM OLD.source_kind OR
        NEW.resolution_state IS DISTINCT FROM OLD.resolution_state OR
        NEW.resolution_method IS DISTINCT FROM OLD.resolution_method OR
        NEW.resolution_evidence IS DISTINCT FROM OLD.resolution_evidence OR
        NEW.price_basis IS DISTINCT FROM OLD.price_basis OR
        NEW.exit_price IS DISTINCT FROM OLD.exit_price OR
        NEW.activation_price IS DISTINCT FROM OLD.activation_price OR
        NEW.outcome_kind IS DISTINCT FROM OLD.outcome_kind OR
        NEW.signal_id IS DISTINCT FROM OLD.signal_id
    ) THEN
        RAISE EXCEPTION 'broker attribution cannot mutate theoretical outcome fields';
    END IF;

    IF TG_OP = 'UPDATE' AND domain IN ('LEGACY_COMPAT', 'RESOLVER_PRIMARY') AND (
        NEW.execution_outcome IS DISTINCT FROM OLD.execution_outcome OR
        NEW.broker_realized_r IS DISTINCT FROM OLD.broker_realized_r OR
        NEW.broker_fill_timestamp IS DISTINCT FROM OLD.broker_fill_timestamp OR
        NEW.broker_exit_timestamp IS DISTINCT FROM OLD.broker_exit_timestamp OR
        NEW.broker_exit_reason IS DISTINCT FROM OLD.broker_exit_reason OR
        NEW.attribution_status IS DISTINCT FROM OLD.attribution_status OR
        NEW.attribution_error IS DISTINCT FROM OLD.attribution_error
    ) THEN
        RAISE EXCEPTION 'theoretical outcome writer cannot mutate broker attribution fields';
    END IF;

    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS entry_signal_outcomes_domain_guard ON strategy.entry_signal_outcomes;
CREATE TRIGGER entry_signal_outcomes_domain_guard
BEFORE INSERT OR UPDATE OR DELETE ON strategy.entry_signal_outcomes
FOR EACH ROW
EXECUTE FUNCTION platform.enforce_entry_signal_outcome_domain_separation();
