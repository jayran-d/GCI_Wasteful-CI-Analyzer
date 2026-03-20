from analyzers.flakiness import FlakinessAnalyzer
from analyzers.zombie import ZombieWorkflowAnalyzer
from analyzers.external_deps import ExternalDepsAnalyzer
from analyzers.inefficient_triggers import InefficientTriggerAnalyzer
from analyzers.rate_limit import RateLimitAnalyzer

ALL_ANALYZERS = [
    FlakinessAnalyzer,
    ZombieWorkflowAnalyzer,
    ExternalDepsAnalyzer,
    InefficientTriggerAnalyzer,
    RateLimitAnalyzer,
]
