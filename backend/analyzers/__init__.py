from analyzers.zombie_workflows import ZombieWorkflowAnalyzer
from analyzers.flakiness import FlakinessAnalyzer
from analyzers.external_deps import ExternalDepsAnalyzer
from analyzers.inefficient_triggers import InefficientTriggerAnalyzer
from analyzers.workflow_deps import WorkflowDependencyAnalyzer

ALL_ANALYZERS = [
    FlakinessAnalyzer,
    ZombieWorkflowAnalyzer,
    ExternalDepsAnalyzer,
    InefficientTriggerAnalyzer, 
    WorkflowDependencyAnalyzer,
]
