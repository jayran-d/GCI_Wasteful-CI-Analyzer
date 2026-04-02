"""
Impact comparisons and sustainability scoring.

Converts energy and CO2 into relatable equivalents and computes scores
across the 5 dimensions of software sustainability:
  - Economic: cost savings, resource efficiency
  - Environmental: carbon emissions, energy consumption
  - Social: team productivity, community signal
  - Individual: developer time saved, cognitive load
  - Technical: CI reliability, pipeline health

Sources (EPA GHG Equivalencies Calculator 2024):
  - Smartphone charge: ~12.4g CO2
  - Car driving: 404g CO2/mile (EPA 2024)
  - LED bulb: 10W
  - Google search: ~0.3g CO2 (Carbonfact / IEA)
  - Streaming video: ~36g CO2/hour (IEA / Shift Project)
  - Tree absorption: ~22,000g CO2/year (EPA)
"""


def compute_impact(energy_kwh: float, carbon_grams: float) -> dict:
    energy_wh = energy_kwh * 1000
    energy_joules = energy_kwh * 3_600_000

    smartphone_charges = carbon_grams / 12.4
    car_miles = carbon_grams / 404.0
    car_km = car_miles * 1.609
    led_bulb_hours = energy_wh / 10.0
    google_searches = carbon_grams / 0.3
    streaming_hours = carbon_grams / 36.0
    tree_days = (carbon_grams / 22000) * 365  # fraction of a tree-year

    comparisons = []

    if smartphone_charges >= 0.1:
        comparisons.append({
            # "icon": "\U0001f4f1",
            "value": round(smartphone_charges, 1),
            "unit": "smartphone charges",
            "text": f"Equivalent to charging a phone {_fmt(smartphone_charges)} times",
        })
    if led_bulb_hours >= 0.1:
        comparisons.append({
            # "icon": "\U0001f4a1",
            "value": round(led_bulb_hours, 1),
            "unit": "hours of LED light",
            "text": f"Could power an LED bulb for {_fmt(led_bulb_hours)} hours",
        })
    if car_km >= 0.01:
        comparisons.append({
            # "icon": "\U0001f697",
            "value": round(car_km, 2),
            "unit": "km driven",
            "text": f"Same CO\u2082 as driving a car {_fmt(car_km)} km",
        })
    if google_searches >= 1:
        comparisons.append({
            # "icon": "\U0001f50d",
            "value": round(google_searches),
            "unit": "Google searches",
            "text": f"Equivalent to {_fmt(google_searches)} Google searches",
        })
    if streaming_hours >= 0.1:
        comparisons.append({
            # "icon": "\U0001f3ac",
            "value": round(streaming_hours, 1),
            "unit": "hours of streaming",
            "text": f"Same as streaming video for {_fmt(streaming_hours)} hours",
        })
    if tree_days >= 0.01:
        comparisons.append({
            # "icon": "\U0001f333",
            "value": round(tree_days, 2),
            "unit": "tree-days to offset",
            "text": f"Needs {_fmt(tree_days)} tree-days of CO\u2082 absorption to offset",
        })

    return {
        "energy_joules": round(energy_joules, 1),
        "energy_wh": round(energy_wh, 3),
        "energy_kwh": round(energy_kwh, 6),
        "carbon_grams": round(carbon_grams, 2),
        "comparisons": comparisons,
    }


def compute_sustainability_scores(
    total_runs: int,
    total_failed: int,
    failure_rate: float,
    categorized_waste_minutes: float,
    total_fail_minutes: float,
    carbon_grams: float,
    energy_kwh: float,
    cost_usd: float,
    analyzer_results: dict,
) -> dict:
    """
    Compute 0-100 scores for each sustainability dimension.

    Higher = more sustainable (less waste).  Each dimension blends multiple
    signals from the analysis results.
    """

    # --- ENVIRONMENTAL (carbon + energy) ---
    # Penalize proportional to carbon per run; best-case 0g, worst ~5g/run
    carbon_per_run = carbon_grams / max(total_runs, 1)
    env_score = max(0, 100 - carbon_per_run * 20)

    # --- ECONOMIC (cost + wasted compute) ---
    cost_per_run = cost_usd / max(total_runs, 1)
    waste_ratio = categorized_waste_minutes / max(total_fail_minutes, 1)
    econ_score = max(0, 100 - (cost_per_run * 500) - (waste_ratio * 40))

    # --- TECHNICAL (reliability, CI health) ---
    # Based on failure rate and flakiness
    flaky = _get_nested(analyzer_results, "flakiness", "summary", "flaky_failures") or 0
    flaky_rate = flaky / max(total_runs, 1) * 100
    tech_score = max(0, 100 - failure_rate * 1.5 - flaky_rate * 3)

    # --- INDIVIDUAL (developer experience) ---
    # Zombie workflows and cascade failures waste developer attention
    zombies = _get_nested(analyzer_results, "zombie_scheduled", "summary", "zombie_workflows") or 0
    cascades = _get_nested(analyzer_results, "workflow_dependencies", "summary", "flaky_runs_detected") or 0
    indiv_score = max(0, 100 - zombies * 15 - cascades * 5 - failure_rate * 0.8)

    # --- SOCIAL (team impact, signal quality) ---
    # Inefficient triggers and external dep failures pollute team signal
    inefficient = _get_nested(analyzer_results, "inefficient_triggers", "summary", "inefficient_run_count") or 0
    ext_deps = _get_nested(analyzer_results, "external_deps", "summary", "external_dep_failures") or 0
    noise_rate = (inefficient + ext_deps) / max(total_runs, 1) * 100
    social_score = max(0, 100 - noise_rate * 5 - failure_rate * 0.5)

    dimensions = {
        "environmental": {
            "score": round(min(100, env_score), 1),
            "label": "Environmental",
            "icon": "\U0001f30d",
            "detail": f"{_fmt(carbon_grams)}g CO\u2082, {_fmt(energy_kwh * 1000)} Wh consumed",
        },
        "economic": {
            "score": round(min(100, econ_score), 1),
            "label": "Economic",
            "icon": "\U0001f4b0",
            "detail": f"${cost_usd:.2f} wasted, {_fmt(categorized_waste_minutes)} min unnecessary compute",
        },
        "technical": {
            "score": round(min(100, tech_score), 1),
            "label": "Technical",
            "icon": "\u2699\ufe0f",
            "detail": f"{failure_rate:.1f}% failure rate, {flaky} flaky failures",
        },
        "individual": {
            "score": round(min(100, indiv_score), 1),
            "label": "Individual",
            "icon": "\U0001f9d1\u200d\U0001f4bb",
            "detail": f"{zombies} zombie workflows, {cascades} cascade failures draining attention",
        },
        "social": {
            "score": round(min(100, social_score), 1),
            "label": "Social",
            "icon": "\U0001f465",
            "detail": f"{inefficient + ext_deps} noise failures polluting team CI signal",
        },
    }

    overall = round(
        sum(d["score"] for d in dimensions.values()) / len(dimensions), 1
    )

    return {
        "overall_score": overall,
        "dimensions": dimensions,
    }


def _get_nested(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _fmt(n):
    if n >= 100:
        return f"{int(n):,}"
    if n >= 10:
        return f"{n:.1f}"
    return f"{n:.2f}"
