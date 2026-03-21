from analyzers.zombie_workflows import ZombieWorkflowAnalyzer
# from analyzers.flakiness import FlakinessAnalyzer
from analyzers.external_deps import ExternalDepsAnalyzer
# from analyzers.inefficient_triggers import InefficientTriggerAnalyzer
# from analyzers.rate_limit import RateLimitAnalyzer

ALL_ANALYZERS = [
    # FlakinessAnalyzer, # Nico
    ZombieWorkflowAnalyzer, # Maja
    ExternalDepsAnalyzer,
    # InefficientTriggerAnalyzer, # Jay
    # RateLimitAnalyzer, # Erkin / Unless we change this?
]
