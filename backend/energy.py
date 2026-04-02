"""
Energy, carbon, and cost estimation for GitHub Actions runners.

Methodology:
  - Region-level emission factors from:
      * US: EPA eGRID2023 Revision 2 (June 2025) -- location-based factors
        per GHG Protocol Corporate Standard, Chapter 6 s6.7
      * EU: Ember European Electricity Review 2024 (2023 data)

  - Power (watts): per-vCPU estimate of 3.5W at CI-typical load (70-90%
    utilization), based on AMD EPYC server-class processors used by Azure.
    Multiplied by core count and PUE 1.17 (Microsoft Azure published avg).


  - Cost: GitHub Actions published per-minute billing rates (March 2026).
    Default: Linux 2-core x64 = $0.006/min.
"""

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Equal-weighted grid carbon intensity (g CO2 / kWh)
#
# Grid carbon intensity represents the average grams of CO2 emitted per
# kilowatt-hour of electricity consumed from the grid.
#
# Region-specific factors are assigned to mapped GitHub Actions / Azure regions:
#   - U.S. regions: EPA eGRID 2023 Revision 2 location-based regional factors
#   - European regions: Ember European Electricity Review 2024 country values
#
# Because individual workflow runs cannot be tied to a specific execution
# region, the implementation uses equal weighting across the 11 mapped regions.
# ---------------------------------------------------------------------------
_N_REGIONS = 11
_EQ_W = round(1.0 / _N_REGIONS, 6)  # 0.090909

_REPORT_PUE = 1.17  #https://datacenters.microsoft.com/sustainability/efficiency/

#https://www.cloudcarbonfootprint.org/docs/methodology/#azure-2
REGION_CARBON_INTENSITY = {
    #  region, g/kWh, weight (equal)
    "eastus": (379.07, _EQ_W),
    "eastus2": (379.07, _EQ_W),
    "centralus": (426.25, _EQ_W),
    "northcentralus": (410.61, _EQ_W),
    "southcentralus": (373.23, _EQ_W),
    "westcentralus": (322.17, _EQ_W),
    "westus": (322.17, _EQ_W),
    "westus2": (322.17, _EQ_W),
    "westus3": (322.17, _EQ_W),
    "westeurope": (329.00, _EQ_W),
    "northeurope": (279.00, _EQ_W),
}

CARBON_INTENSITY_G_PER_KWH = round(
    sum(ci * w for ci, w in REGION_CARBON_INTENSITY.values()),
    2)  # ~320.21 (equal-weighted)

CARBON_INTENSITY_LOWER = min(
    ci for ci, _ in REGION_CARBON_INTENSITY.values())  # 195.05
CARBON_INTENSITY_UPPER = max(
    ci for ci, _ in REGION_CARBON_INTENSITY.values())  # 472.88

# ---------------------------------------------------------------------------
# Power per runner (watts)
# ---------------------------------------------------------------------------
_W_ESTIMATED_MACHINE = 3.57  # https://www.green-coding.io/case-studies/carbon-cost-of-testing-pipelines/

RUNNER_POWER_W = {
    "linux": round(_W_ESTIMATED_MACHINE, 2),
    "default": round(_W_ESTIMATED_MACHINE, 2),
}

# ---------------------------------------------------------------------------
# GitHub Actions billing rates (USD / minute)
# Source: https://docs.github.com/en/billing/reference/actions-runner-pricing
# ---------------------------------------------------------------------------
RUNNER_COST_PER_MIN = {
    "linux": 0.006,  # 2-core x64
    "linux_4_core": 0.012,
    "linux_8_core": 0.022,
    "linux_16_core": 0.042,
    "linux_32_core": 0.082,
    "linux_64_core": 0.162,
    "windows": 0.010,  # 2-core x64
    "macos": 0.062,  # 3/4-core M1 or Intel
    "default": 0.006,
}


@dataclass
class EnergyEstimate:
    duration_seconds: float
    runner_type: str
    energy_kwh: float
    carbon_grams: float
    carbon_grams_lower: float
    carbon_grams_upper: float
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
            "carbon_grams_co2_lower": round(self.carbon_grams_lower, 3),
            "carbon_grams_co2_upper": round(self.carbon_grams_upper, 3),
            "estimated_cost_usd": round(self.cost_usd, 4),
        }


def detect_runner_type(labels: list[str] | None) -> str:
    """Map job/run labels to a runner category key.  Defaults to 'linux'."""

    if not labels:
        return "linux"
    joined = " ".join(str(l).lower() for l in labels)
    if "macos" in joined or "mac" in joined:
        return "macos"
    if "windows" in joined or "win" in joined:
        return "windows"
    for cores in (64, 32, 16, 8, 4):
        if f"{cores}-core" in joined or f"_{cores}_core" in joined:
            return f"linux_{cores}_core"
    return "linux"


def estimate_energy(duration_seconds: float,
                    runner_type: str = "linux") -> EnergyEstimate:
    """Estimate energy, CO2 (with confidence range), and cost for a run/job."""
    power_w = RUNNER_POWER_W.get(runner_type, RUNNER_POWER_W["default"])
    hours = duration_seconds / 3600
    energy_kwh = (power_w * hours * _REPORT_PUE) / 1000

    carbon_g = energy_kwh * CARBON_INTENSITY_G_PER_KWH
    carbon_g_lower = energy_kwh * CARBON_INTENSITY_LOWER
    carbon_g_upper = energy_kwh * CARBON_INTENSITY_UPPER

    minutes = duration_seconds / 60
    cost = minutes * RUNNER_COST_PER_MIN.get(runner_type,
                                             RUNNER_COST_PER_MIN["default"])

    return EnergyEstimate(
        duration_seconds=duration_seconds,
        runner_type=runner_type,
        energy_kwh=energy_kwh,
        carbon_grams=carbon_g,
        carbon_grams_lower=carbon_g_lower,
        carbon_grams_upper=carbon_g_upper,
        cost_usd=cost,
    )


def aggregate_estimates(estimates: list[EnergyEstimate]) -> dict:
    """Sum a list of energy estimates into a single summary dict."""
    total = {
        "total_duration_seconds": 0.0,
        "total_energy_kwh": 0.0,
        "total_carbon_grams_co2": 0.0,
        "total_carbon_grams_co2_lower": 0.0,
        "total_carbon_grams_co2_upper": 0.0,
        "total_cost_usd": 0.0,
        "count": len(estimates),
    }
    for e in estimates:
        total["total_duration_seconds"] += e.duration_seconds
        total["total_energy_kwh"] += e.energy_kwh
        total["total_carbon_grams_co2"] += e.carbon_grams
        total["total_carbon_grams_co2_lower"] += e.carbon_grams_lower
        total["total_carbon_grams_co2_upper"] += e.carbon_grams_upper
        total["total_cost_usd"] += e.cost_usd

    total["total_duration_minutes"] = round(
        total["total_duration_seconds"] / 60, 2)
    total["total_duration_hours"] = round(
        total["total_duration_seconds"] / 3600, 2)
    total["total_energy_wh"] = round(total["total_energy_kwh"] * 1000, 3)
    total["total_energy_joules"] = round(total["total_energy_kwh"] * 3_600_000,
                                         1)

    total["methodology"] = {
        "carbon_intensity_g_per_kwh":
        CARBON_INTENSITY_G_PER_KWH,
        "carbon_intensity_range":
        [CARBON_INTENSITY_LOWER, CARBON_INTENSITY_UPPER],
        "weighting":
        "Equal weight across 11 GitHub Actions Azure regions",
        "power_per_vcpu_w":
        _W_ESTIMATED_MACHINE,
        "sources": [
            "EPA eGRID2023 Rev2 (US regions)",
            "Ember European Electricity Review 2024 (EU regions)",
            "GHG Protocol Corporate Standard Ch.6 s6.7",
        ],
    }

    for k in total:
        if isinstance(total[k], float):
            total[k] = round(total[k], 4)

    return total
