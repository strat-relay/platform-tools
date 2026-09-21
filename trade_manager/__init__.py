"""Strategy-agnostic advisory/shadow trade management."""

from .engine import TradeManager, ManagementPolicy
from .observation import CausalObserver, build_observation, causal_candles, detect_events, structure_snapshot
from .observation_storage import ObservationStore
from .collector import CausalObservationCollector
from .semantics import PriceSemantics, price_semantics
from .policies import ManagementPolicyConfig, PolicyRegistry, default_registry, context_v1_experiment
from .phase2 import Phase2TradeManager, counterfactual_for
from .trailing import evaluate_trailing
from .metrics import mfe_surrender
from .experiments import ExperimentSpec, MultiPolicyExperimentRunner, default_experiments
from .prospective import ProspectiveActivation, ProspectiveExperimentCollector

__all__ = ["TradeManager", "ManagementPolicy", "CausalObserver", "ObservationStore", "CausalObservationCollector",
           "PriceSemantics", "price_semantics", "ManagementPolicyConfig", "PolicyRegistry", "default_registry",
           "context_v1_experiment", "Phase2TradeManager", "counterfactual_for", "evaluate_trailing", "mfe_surrender",
           "ExperimentSpec", "MultiPolicyExperimentRunner", "default_experiments", "ProspectiveActivation", "ProspectiveExperimentCollector",
           "build_observation", "causal_candles", "detect_events", "structure_snapshot"]
