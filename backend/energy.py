"""
Energy and carbon estimation for GitHub Actions runners.
# might need to rework estimates based on actual data TODO ~the following are ESTIMATES from some pages but need to confirm them
"""

from dataclasses import dataclass

RUNNER_POWER_W = {
    "linux":   3.57,
    "windows": 3.57,
    "macos":   3.0,   
    "default": 3.57,
}
# GitHub Actions billing rates (USD / minute) for cost context
RUNNER_COST_PER_MIN = {
    "linux": 0.008,
    "windows": 0.016,
    "macos": 0.08,
    "default": 0.008,
}

CARBON_INTENSITY_G_PER_KWH = 442 

@dataclass
class EnergyEstimate:
    duration_seconds: float
    runner_type: str
    energy_kwh: float
    carbon_grams: float
    cost_usd: float

    def to_dict(self):
        return {
            "duration_seconds": round(self.duration_seconds, 1),
            "duration_minutes": round(self.duration_seconds / 60, 2),
            "runner_type": self.runner_type,
            "energy_kwh": round(self.energy_kwh, 6),
            "energy_wh": round(self.energy_kwh * 1000, 3),
            "energy_joules": round(self.energy_kwh * 3_600_000, 1),
            "carbon_grams_co2": round(self.carbon_grams, 3),
            "estimated_cost_usd": round(self.cost_usd, 4),
        }


def detect_runner_type(labels: list[str] | None) -> str:
    """Guess runner OS from job/run labels."""
    if not labels:
        return "default"
    joined = " ".join(str(l).lower() for l in labels)
    if "macos" in joined or "mac" in joined:
        return "macos"
    if "windows" in joined or "win" in joined:
        return "windows"
    return "linux"


def estimate_energy(duration_seconds: float, runner_type: str = "linux") -> EnergyEstimate:
    """Estimate energy, CO2, and cost for a single run/job."""
    power_w = RUNNER_POWER_W.get(runner_type, RUNNER_POWER_W["default"])
    hours = duration_seconds / 3600
    energy_kwh = (power_w * hours) / 1000
    carbon_g = energy_kwh * CARBON_INTENSITY_G_PER_KWH
    minutes = duration_seconds / 60
    cost = minutes * RUNNER_COST_PER_MIN.get(runner_type, RUNNER_COST_PER_MIN["default"])

    return EnergyEstimate(
        duration_seconds=duration_seconds,
        runner_type=runner_type,
        energy_kwh=energy_kwh,
        carbon_grams=carbon_g,
        cost_usd=cost,
    )


def aggregate_estimates(estimates: list[EnergyEstimate]) -> dict:
    """Sum up a list of energy estimates into a single summary."""
    total = {
        "total_duration_seconds": 0.0,
        "total_energy_kwh": 0.0,
        "total_carbon_grams_co2": 0.0,
        "total_cost_usd": 0.0,
        "count": len(estimates),
    }
    for e in estimates:
        total["total_duration_seconds"] += e.duration_seconds
        total["total_energy_kwh"] += e.energy_kwh
        total["total_carbon_grams_co2"] += e.carbon_grams
        total["total_cost_usd"] += e.cost_usd

    total["total_duration_minutes"] = round(total["total_duration_seconds"] / 60, 2)
    total["total_duration_hours"] = round(total["total_duration_seconds"] / 3600, 2)
    total["total_energy_wh"] = round(total["total_energy_kwh"] * 1000, 3)
    total["total_energy_joules"] = round(total["total_energy_kwh"] * 3_600_000, 1)

    # Round everything
    for k in total:
        if isinstance(total[k], float):
            total[k] = round(total[k], 4)

    return total
