"""
Impact comparisons.

Converts energy and CO2 into relatable equivalents

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

def _fmt(n):
    if n >= 100:
        return f"{int(n):,}"
    if n >= 10:
        return f"{n:.1f}"
    return f"{n:.2f}"
