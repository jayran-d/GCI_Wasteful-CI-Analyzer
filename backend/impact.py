"""
Convert estimated energy use and CO2 emissions into illustrative equivalents.

Sources:
    - Smartphone charge:
        EPA Greenhouse Gas Equivalencies Calculator - Calculations and References
        https://www.epa.gov/energy/greenhouse-gas-equivalencies-calculator-calculations-and-references

    - Passenger vehicle travel:
        EPA Greenhouse Gas Emissions from a Typical Passenger Vehicle
        https://www.epa.gov/greenvehicles/greenhouse-gas-emissions-typical-passenger-vehicle

    - LED bulb power range:
        U.S. Department of Energy, Purchasing Energy-Efficient Light Bulbs
        https://www.energy.gov/cmei/femp/purchasing-energy-efficient-light-bulbs
        median of 5.5–11 W

    - Streaming video:
        International Energy Agency, The carbon footprint of streaming video:
        fact-checking the headlines
        https://www.iea.org/commentaries/the-carbon-footprint-of-streaming-video-fact-checking-the-headlines

    - Gasoline Burnt:
        EPA Greenhouse Gas Equivalencies Calculator - Calculations and References
        https://www.epa.gov/energy/greenhouse-gas-equivalencies-calculator-calculations-and-references
        
    - Home Electricity:
        EPA Greenhouse Gas Equivalencies Calculator - Calculations and References
        https://www.epa.gov/energy/greenhouse-gas-equivalencies-calculator-calculations-and-references
        
Notes:
  - These are illustrative equivalents, not direct measurements.
  - Car distance is displayed in kilometers for readability, but based on an EPA
    grams-CO2-per-mile factor.
"""


def compute_impact(energy_kwh: float, carbon_grams: float) -> dict:
    """
    Convert estimated energy use and CO2 emissions into illustrative equivalents.

    Sources:
      - Smartphone charge: 12.4 g CO2 per charge
        (EPA Greenhouse Gas Equivalencies Calculator: Calculations and References)
      - Passenger vehicle travel: 400 g CO2 per mile
        (EPA Typical Passenger Vehicle emissions)
      - LED bulb: 8.25 W representative value
        (median of DOE example efficient LED bulb ratings: 5.5 W and 11 W)
      - Streaming video: 36 g CO2 per hour
        (IEA corrected central estimate)
      - Gasoline burned: 8,890 g CO2 per gallon
        (EPA Greenhouse Gas Equivalencies Calculator: Calculations and References)
      - Home electricity use: 4,798,000 g CO2 per home per year
        (EPA Greenhouse Gas Equivalencies Calculator FAQ)

    Notes:
      - These are illustrative equivalents, not direct measurements.
      - Car distance is displayed in kilometers for readability, but based on
        the EPA factor in g CO2 per mile.
    """
    energy_wh = energy_kwh * 1000
    energy_joules = energy_kwh * 3_600_000

    smartphone_charges = carbon_grams / 12.4
    car_miles = carbon_grams / 400.0
    car_km = car_miles * 1.60934
    led_bulb_hours = energy_wh / 8.25
    streaming_hours = carbon_grams / 36.0
    gasoline_gallons = carbon_grams / 8887.0
    home_electricity_years = carbon_grams / 4_798_000.0
    home_electricity_days = home_electricity_years * 365

    comparisons = []

    if smartphone_charges >= 0.1:
        comparisons.append({
            "value":
            round(smartphone_charges, 1),
            "unit":
            "smartphone charges",
            "text":
            f"Equivalent to charging a smartphone {_fmt(smartphone_charges)} times",
        })

    if led_bulb_hours >= 0.1:
        comparisons.append({
            "value":
            round(led_bulb_hours, 1),
            "unit":
            "hours of LED light",
            "text":
            f"Could power an LED bulb for {_fmt(led_bulb_hours)} hours",
        })

    if car_km >= 0.01:
        comparisons.append({
            "value":
            round(car_km, 2),
            "unit":
            "km driven",
            "text":
            f"Same CO₂ as driving a typical passenger car {_fmt(car_km)} km",
        })

    if streaming_hours >= 0.1:
        comparisons.append({
            "value":
            round(streaming_hours, 1),
            "unit":
            "hours of streaming",
            "text":
            f"Equivalent to streaming video for {_fmt(streaming_hours)} hours",
        })

    if gasoline_gallons >= 0.01:
        comparisons.append({
            "value":
            round(gasoline_gallons, 2),
            "unit":
            "gallons of gasoline",
            "text":
            f"Equivalent to burning {_fmt(gasoline_gallons)} gallons of gasoline",
        })

    if home_electricity_days >= 0.01:
        comparisons.append({
            "value":
            round(home_electricity_days, 2),
            "unit":
            "home-electricity days",
            "text":
            f"Equivalent to about {_fmt(home_electricity_days)} days of home electricity emissions",
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
